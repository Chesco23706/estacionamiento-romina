import json
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import (
    CashCut,
    CashMovement,
    EmployeeSchedule,
    Role,
    Tariff,
    User,
    VehicleRecord,
    WeeklyExitLog,
    WeeklyPrepayment,
    generate_internal_ticket_code,
    get_period_bounds,
    log_action,
)
from .pricing import calculate_charge, ensure_utc, utc_now
from .validators import (
    clean_optional_text,
    clean_plate,
    clean_text,
    clean_ticket,
    validate_password_strength,
)

STATUS_OPTIONS = (
    "Dentro del estacionamiento",
    "Indeciso",
    "Salida registrada",
    "Pagado",
    "Pendiente de pago",
)
RECORD_STAY_MODES = (
    ("hourly", "Por hora"),
    ("weekly", "Por semana"),
)
BILLING_SCHEMES = (
    ("hourly", "Por hora"),
    ("daily", "Por dia"),
    ("flat", "Tarifa fija"),
)
PERIOD_UNITS = (
    ("hour", "Hora"),
    ("day", "Dia"),
)
CASH_MOVEMENT_TYPES = (
    ("income", "Entrada de dinero"),
    ("expense", "Gasto"),
)
WEEKDAY_OPTIONS = (
    (0, "Lunes"),
    (1, "Martes"),
    (2, "Miercoles"),
    (3, "Jueves"),
    (4, "Viernes"),
    (5, "Sabado"),
    (6, "Domingo"),
)

LEGACY_VEHICLE_TYPE_ALIASES = {
    "moto por semana": "Moto",
    "motocicleta por semana": "Moto",
    "bicicleta por semana": "Bicicleta",
    "carrito por semana": "Carrito callejero",
}


def normalize_vehicle_type(raw_value):
    normalized = clean_text(raw_value, 50, "tipo de vehiculo")
    return LEGACY_VEHICLE_TYPE_ALIASES.get(normalized.lower(), normalized)


def get_vehicle_types(include_inactive=False):
    query = Tariff.query.order_by(Tariff.vehicle_type.asc())
    if not include_inactive:
        query = query.filter(Tariff.active.is_(True))
    visible_vehicle_types = []
    for tariff in query.all():
        normalized_type = LEGACY_VEHICLE_TYPE_ALIASES.get(tariff.vehicle_type.lower(), tariff.vehicle_type)
        if normalized_type not in visible_vehicle_types:
            visible_vehicle_types.append(normalized_type)
    return visible_vehicle_types


def get_tariffs(include_inactive=True):
    query = Tariff.query.order_by(Tariff.vehicle_type.asc())
    if not include_inactive:
        query = query.filter(Tariff.active.is_(True))
    return query.all()


def create_vehicle_record(form_data, user, prepayment=None):
    physical_ticket_number = clean_ticket(form_data.get("ticket_number"))
    client_name = clean_text(form_data.get("client_name"), 120, "cliente")
    vehicle_type = normalize_vehicle_type(form_data.get("vehicle_type"))
    plate_number = clean_plate(form_data.get("plate_number"))
    stay_mode = _clean_stay_mode(form_data.get("stay_mode"))
    contracted_days = _clean_contracted_days(form_data.get("contracted_days"), stay_mode)
    weekly_prepaid = form_data.get("weekly_prepaid") == "on"
    if weekly_prepaid and stay_mode != "weekly":
        raise ValueError("El pago por adelantado requiere la modalidad semanal.")
    service_wash, service_oil_change, service_oil_price = _clean_service_payload(form_data)
    notes = clean_optional_text(form_data.get("notes"), 500)

    if prepayment is None and vehicle_type not in get_vehicle_types():
        raise ValueError("El tipo de vehiculo no es valido o no esta activo.")

    active_ticket = VehicleRecord.query.filter(
        VehicleRecord.physical_ticket_number == physical_ticket_number,
        VehicleRecord.exit_at.is_(None),
    ).first()
    if active_ticket:
        raise ValueError("Esa ficha fisica ya esta asignada a un vehiculo dentro del estacionamiento.")

    record = VehicleRecord(
        ticket_number=generate_internal_ticket_code(),
        physical_ticket_number=physical_ticket_number,
        client_name=client_name,
        vehicle_type=vehicle_type,
        plate_number=plate_number,
        stay_mode=stay_mode,
        contracted_days=contracted_days,
        entry_at=utc_now(),
        service_wash=service_wash,
        service_oil_change=service_oil_change,
        service_oil_price=service_oil_price,
        notes=notes,
        entry_user=user,
    )
    try:
        if prepayment is not None:
            claimed = WeeklyPrepayment.query.filter(
                WeeklyPrepayment.id == prepayment.id,
                WeeklyPrepayment.activated_at.is_(None),
            ).update({"activated_at": record.entry_at})
            if claimed != 1:
                raise ValueError("Este anticipo ya fue utilizado.")
            record.weekly_prepayment_id = prepayment.id
            record.paid_at = prepayment.movement.occurred_at
            record.payment_user_id = prepayment.movement.created_by_user_id
            record.total_amount = prepayment.movement.amount
            record.applied_rate_label = prepayment.rate_label
        db.session.add(record)
        db.session.flush()
        log_action(
            user,
            "vehicle_entry_created",
            "vehicle_record",
            record.id,
            {
                "ticket_number": record.ticket_number,
                "physical_ticket_number": physical_ticket_number,
                "vehicle_type": vehicle_type,
                "stay_mode": stay_mode,
                "contracted_days": contracted_days,
                "service_wash": service_wash,
                "service_oil_change": service_oil_change,
                "service_oil_price": float(service_oil_price),
            },
        )
        if weekly_prepaid:
            _apply_vehicle_payment(record, user, record.entry_at)
        if prepayment is not None:
            log_action(user, "weekly_prepayment_activated", "vehicle_record", record.id)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ValueError("Esa ficha fisica ya esta asignada a un vehiculo dentro del estacionamiento.") from exc
    except ValueError:
        db.session.rollback()
        raise
    return record


def create_weekly_prepayment(form_data, user):
    client_name = clean_text(form_data.get("client_name"), 120, "cliente")
    plate_number = clean_plate(form_data.get("plate_number"))
    vehicle_type = normalize_vehicle_type(form_data.get("vehicle_type"))
    if vehicle_type not in get_vehicle_types():
        raise ValueError("El tipo de vehiculo no es valido o no esta activo.")

    paid_at = utc_now()
    days = _clean_contracted_days(None, "weekly")
    pricing = calculate_charge(vehicle_type, paid_at, paid_at, "weekly", days)
    movement = CashMovement(
        movement_type="income",
        description=f"Anticipo semanal: {client_name} ({plate_number})",
        amount=pricing["total"],
        occurred_at=paid_at,
        created_by=user,
    )
    prepayment = WeeklyPrepayment(
        client_name=client_name, plate_number=plate_number, vehicle_type=vehicle_type,
        rate_label=pricing["rate_label"], movement=movement,
    )
    db.session.add(prepayment)
    try:
        db.session.flush()
        log_action(user, "weekly_prepayment_created", "cash_movement", movement.id)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ValueError("Ya hay un anticipo pendiente para esa placa.") from exc
    return prepayment


def activate_weekly_prepayment(prepayment, ticket_number, user):
    return create_vehicle_record(
        {
            "ticket_number": ticket_number,
            "client_name": prepayment.client_name,
            "plate_number": prepayment.plate_number,
            "vehicle_type": prepayment.vehicle_type,
            "stay_mode": "weekly",
        },
        user,
        prepayment=prepayment,
    )


def update_vehicle_record(record, form_data, user, allow_entry_at_change=False):
    original_entry_at = record.entry_at
    if record.weekly_prepayment_id and (
        _clean_stay_mode(form_data.get("stay_mode")) != record.stay_mode
        or normalize_vehicle_type(form_data.get("vehicle_type")) != record.vehicle_type
        or _clean_service_payload(form_data) != (
            record.service_wash, record.service_oil_change, record.service_oil_price
        )
    ):
        raise ValueError("El tipo, la modalidad y los servicios del anticipo ya cobrado no pueden cambiarse.")
    record.physical_ticket_number = clean_ticket(form_data.get("ticket_number"))
    record.client_name = clean_text(form_data.get("client_name"), 120, "cliente")
    record.vehicle_type = normalize_vehicle_type(form_data.get("vehicle_type"))
    record.plate_number = clean_plate(form_data.get("plate_number"))
    record.stay_mode = _clean_stay_mode(form_data.get("stay_mode"))
    record.contracted_days = _clean_contracted_days(
        form_data.get("contracted_days"),
        record.stay_mode,
    )
    (
        record.service_wash,
        record.service_oil_change,
        record.service_oil_price,
    ) = _clean_service_payload(form_data)
    record.status = clean_text(form_data.get("status"), 30, "estado")
    record.notes = clean_optional_text(form_data.get("notes"), 500)

    if record.vehicle_type not in get_vehicle_types(include_inactive=True):
        raise ValueError("El tipo de vehiculo no es valido.")
    if record.status not in STATUS_OPTIONS:
        raise ValueError("El estado seleccionado no es valido.")

    if allow_entry_at_change and "entry_at_local" in form_data:
        record.entry_at = _clean_entry_at_local(form_data.get("entry_at_local"))
        if record.exit_at and record.entry_at > ensure_utc(record.exit_at):
            raise ValueError("La fecha de entrada no puede ser posterior a la salida.")
        if record.paid_at and not record.weekly_prepayment_id and record.entry_at > ensure_utc(record.paid_at):
            raise ValueError("La fecha de entrada no puede ser posterior al pago.")

    existing = VehicleRecord.query.filter(
        VehicleRecord.physical_ticket_number == record.physical_ticket_number,
        VehicleRecord.id != record.id,
        VehicleRecord.exit_at.is_(None),
    ).first()
    if existing:
        raise ValueError("Esa ficha fisica ya esta asignada a otro vehiculo dentro del estacionamiento.")

    if (record.exit_at or record.paid_at) and not record.weekly_prepayment_id:
        charge_until = record.exit_at or record.paid_at
        pricing = calculate_charge(
            record.vehicle_type,
            record.entry_at,
            charge_until,
            stay_mode=record.stay_mode,
            contracted_days=record.contracted_days,
        )
        record.duration_seconds = pricing["duration_seconds"]
        record.applied_rate_label = pricing["rate_label"]
        record.total_amount = pricing["total"] + record.service_oil_price + (20 if record.service_wash else 0) + (40 if record.service_oil_change else 0)
        if record.services_total_amount:
            record.applied_rate_label = f"{record.applied_rate_label} | {record.services_label}"

    log_action(
        user,
        "vehicle_record_updated",
        "vehicle_record",
        record.id,
        {"entry_at_changed": True} if record.entry_at != original_entry_at else None,
    )
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ValueError("Esa ficha fisica ya esta asignada a otro vehiculo dentro del estacionamiento.") from exc
    return record


def _clean_entry_at_local(raw_value):
    value = (raw_value or "").strip()
    if not value:
        raise ValueError("Selecciona la fecha y hora de entrada.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Selecciona una fecha de entrada valida.") from exc

    if parsed.tzinfo is None:
        timezone_name = current_app.config.get("APP_TIMEZONE", "America/Mexico_City")
        try:
            local_timezone = ZoneInfo(timezone_name)
        except Exception:
            local_timezone = ZoneInfo("America/Mexico_City")
        parsed = parsed.replace(tzinfo=local_timezone)
    return ensure_utc(parsed)


def register_exit(record, user):
    if record.exit_at:
        raise ValueError("La salida ya fue registrada previamente.")
    if not record.is_paid:
        raise ValueError("Primero registra el pago antes de marcar la salida.")
    record.close_record(user)
    log_action(
        user,
        "vehicle_exit_registered",
        "vehicle_record",
        record.id,
        {"total_amount": float(record.total_amount)},
    )
    db.session.commit()
    return record


def register_payment(record, user):
    if record.is_paid:
        raise ValueError("Ese registro ya fue marcado como pagado.")
    _apply_vehicle_payment(record, user, utc_now())
    db.session.commit()
    return record


def _apply_vehicle_payment(record, user, paid_at):
    pricing = calculate_charge(
        record.vehicle_type,
        record.entry_at,
        paid_at,
        stay_mode=record.stay_mode,
        contracted_days=record.contracted_days,
    )
    record.duration_seconds = pricing["duration_seconds"]
    record.applied_rate_label = pricing["rate_label"]
    record.total_amount = pricing["total"] + record.service_oil_price + (20 if record.service_wash else 0) + (40 if record.service_oil_change else 0)
    if record.services_total_amount:
        record.applied_rate_label = f"{record.applied_rate_label} | {record.services_label}"
    record.paid_at = paid_at
    record.payment_user = user
    record.mark_paid()
    log_action(user, "vehicle_payment_registered", "vehicle_record", record.id)


def register_weekly_exit(record, user):
    if record.is_hourly:
        raise ValueError("Solo los registros semanales permiten salidas parciales.")
    if record.exit_at:
        raise ValueError("Este registro semanal ya fue cerrado.")
    if record.status != "Dentro del estacionamiento":
        raise ValueError("Primero registra la entrada del dia antes de volver a sacar este vehiculo.")

    day_number = record.consumed_day_units()
    weekly_exit = WeeklyExitLog(
        record=record,
        exited_at=utc_now(),
        day_number=day_number,
        created_by=user,
    )
    db.session.add(weekly_exit)
    record.status = "Salida registrada"
    log_action(
        user,
        "weekly_exit_registered",
        "vehicle_record",
        record.id,
        {"day_number": day_number},
    )
    db.session.commit()
    return weekly_exit


def register_weekly_entry(record, user):
    if record.is_hourly:
        raise ValueError("Solo los registros semanales permiten entrada del dia.")
    if record.exit_at:
        raise ValueError("Este registro semanal ya fue cerrado.")
    if record.status == "Dentro del estacionamiento":
        raise ValueError("Este vehiculo ya esta marcado dentro del estacionamiento.")
    if record.weekly_exit_count == 0:
        raise ValueError("Primero registra una salida del dia antes de usar esta opcion.")

    record.status = "Dentro del estacionamiento"
    log_action(
        user,
        "weekly_entry_registered",
        "vehicle_record",
        record.id,
    )
    db.session.commit()
    return record


def delete_vehicle_record(record, user):
    log_action(
        user,
        "vehicle_record_deleted",
        "vehicle_record",
        record.id,
        {"ticket_number": record.ticket_number},
    )
    db.session.delete(record)
    db.session.commit()


def create_employee(form_data, user):
    full_name = clean_text(form_data.get("full_name"), 120, "nombre completo")
    username = clean_text(form_data.get("username"), 50, "usuario").lower()
    password = validate_password_strength(form_data.get("password", ""))

    if User.query.filter(func.lower(User.username) == username).first():
        raise ValueError("Ese nombre de usuario ya existe.")

    employee_role = _ensure_employee_role()
    new_user = User(full_name=full_name, username=username, role=employee_role)
    new_user.set_password(password)
    new_user.password_reference = password
    try:
        db.session.add(new_user)
        db.session.flush()
        log_action(user, "employee_created", "user", new_user.id, {"username": username})
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ValueError(
            "No se pudo crear el empleado porque el usuario ya existe o la base rechazo el registro."
        ) from exc
    return new_user


def reset_employee_password(target_user, new_password, actor):
    validated_password = validate_password_strength(new_password)
    target_user.set_password(validated_password)
    target_user.password_reference = validated_password
    log_action(actor, "employee_password_reset", "user", target_user.id)
    db.session.commit()


def update_employee(target_user, form_data, actor):
    full_name = clean_text(form_data.get("full_name"), 120, "nombre completo")
    username = clean_text(form_data.get("username"), 50, "usuario").lower()

    existing = User.query.filter(
        func.lower(User.username) == username,
        User.id != target_user.id,
    ).first()
    if existing:
        raise ValueError("Ese nombre de usuario ya existe.")

    target_user.full_name = full_name
    target_user.username = username
    log_action(
        actor,
        "employee_updated",
        "user",
        target_user.id,
        {"username": username},
    )
    db.session.commit()
    return target_user


def update_employee_schedule(target_user, form_data, actor):
    if target_user.is_admin:
        raise ValueError("Los administradores no requieren horario de acceso.")

    schedules_by_day = {schedule.weekday: schedule for schedule in target_user.schedules}
    for weekday, _label in WEEKDAY_OPTIONS:
        enabled = form_data.get(f"enabled_{weekday}") == "on"
        start_time = _clean_schedule_time(form_data.get(f"start_{weekday}"), f"inicio del dia {weekday}")
        end_time = _clean_schedule_time(form_data.get(f"end_{weekday}"), f"fin del dia {weekday}")
        if enabled and start_time >= end_time:
            raise ValueError("La hora de inicio debe ser anterior a la hora final.")

        schedule = schedules_by_day.get(weekday)
        if not schedule:
            schedule = EmployeeSchedule(user=target_user, weekday=weekday)
            db.session.add(schedule)
        schedule.is_enabled = enabled
        schedule.start_time = start_time
        schedule.end_time = end_time

    log_action(actor, "employee_schedule_updated", "user", target_user.id)
    db.session.commit()
    return target_user


def employee_schedule_rows(target_user):
    existing = {schedule.weekday: schedule for schedule in target_user.schedules}
    rows = []
    for weekday, label in WEEKDAY_OPTIONS:
        schedule = existing.get(weekday)
        rows.append(
            {
                "weekday": weekday,
                "label": label,
                "enabled": bool(schedule and schedule.is_enabled),
                "start": _format_schedule_time(schedule.start_time if schedule else time(8, 0)),
                "end": _format_schedule_time(schedule.end_time if schedule else time(18, 0)),
            }
        )
    return rows


def is_employee_within_schedule(target_user, moment=None):
    if target_user.is_admin:
        return True
    schedules = list(target_user.schedules)
    if not schedules or not any(schedule.is_enabled for schedule in schedules):
        return True

    local_moment = _localize_schedule_moment(moment or utc_now())
    active_schedule = next(
        (
            schedule
            for schedule in schedules
            if schedule.weekday == local_moment.weekday() and schedule.is_enabled
        ),
        None,
    )
    if not active_schedule:
        return False
    local_time = local_moment.time().replace(second=0, microsecond=0)
    return active_schedule.start_time <= local_time <= active_schedule.end_time


def _clean_schedule_time(raw_value, field_label):
    value = (raw_value or "").strip()
    if not value:
        raise ValueError(f"Selecciona la hora de {field_label}.")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Selecciona una hora valida para {field_label}.") from exc
    return parsed.replace(second=0, microsecond=0)


def _format_schedule_time(value):
    return value.strftime("%H:%M") if value else ""


def _localize_schedule_moment(moment):
    timezone_name = current_app.config.get("APP_TIMEZONE", "America/Mexico_City")
    try:
        local_timezone = ZoneInfo(timezone_name)
    except Exception:
        local_timezone = ZoneInfo("America/Mexico_City")
    return ensure_utc(moment).astimezone(local_timezone)


def delete_employee(target_user, actor):
    if target_user.id == actor.id:
        raise ValueError("No puedes eliminar tu propio usuario.")
    if target_user.is_admin:
        raise ValueError("No se permite eliminar administradores desde esta pantalla.")

    log_action(
        actor,
        "employee_deleted",
        "user",
        target_user.id,
        {"username": target_user.username},
    )
    db.session.delete(target_user)
    db.session.commit()


def create_tariff(form_data, user):
    tariff = Tariff(**_clean_tariff_payload(form_data))
    if Tariff.query.filter(func.lower(Tariff.vehicle_type) == tariff.vehicle_type.lower()).first():
        raise ValueError("Ese tipo de vehiculo ya existe.")

    db.session.add(tariff)
    db.session.flush()
    log_action(user, "tariff_created", "tariff", tariff.id, {"vehicle_type": tariff.vehicle_type})
    db.session.commit()
    return tariff


def update_tariff(tariff, form_data, user):
    payload = _clean_tariff_payload(form_data)
    existing = Tariff.query.filter(
        func.lower(Tariff.vehicle_type) == payload["vehicle_type"].lower(),
        Tariff.id != tariff.id,
    ).first()
    if existing:
        raise ValueError("Ese tipo de vehiculo ya existe.")

    for key, value in payload.items():
        setattr(tariff, key, value)

    log_action(user, "tariff_updated", "tariff", tariff.id, {"vehicle_type": tariff.vehicle_type})
    db.session.commit()
    return tariff


def create_cash_movement(form_data, user):
    movement_type = clean_text(form_data.get("movement_type"), 20, "tipo de movimiento").lower()
    if movement_type not in {value for value, _label in CASH_MOVEMENT_TYPES}:
        raise ValueError("El tipo de movimiento de caja no es valido.")

    movement = CashMovement(
        movement_type=movement_type,
        description=clean_text(form_data.get("description"), 255, "descripcion"),
        amount=_clean_decimal(form_data.get("amount"), "cantidad"),
        created_by=user,
    )
    db.session.add(movement)
    db.session.flush()
    log_action(
        user,
        "cash_movement_created",
        "cash_movement",
        movement.id,
        {
            "movement_type": movement_type,
            "amount": float(movement.amount),
            "description": movement.description,
        },
    )
    db.session.commit()
    return movement


def delete_cash_movement(movement, user):
    if WeeklyPrepayment.query.filter_by(movement_id=movement.id).first():
        raise ValueError("El anticipo semanal esta vinculado a una reserva.")
    log_action(
        user,
        "cash_movement_deleted",
        "cash_movement",
        movement.id,
        {
            "movement_type": movement.movement_type,
            "amount": float(movement.amount),
            "description": movement.description,
        },
    )
    db.session.delete(movement)
    db.session.commit()


def cash_movements_for_period(start, end, target_user=None):
    query = CashMovement.query.filter(
        CashMovement.occurred_at >= start,
        CashMovement.occurred_at < end,
    )
    if target_user:
        query = query.filter(CashMovement.created_by_user_id == target_user.id)
    return query.order_by(CashMovement.occurred_at.asc(), CashMovement.id.asc()).all()


def cash_movement_totals(start, end, target_user=None):
    movements = cash_movements_for_period(start, end, target_user=target_user)
    income = sum(
        Decimal(movement.amount or 0)
        for movement in movements
        if movement.movement_type == "income"
    )
    expenses = sum(
        Decimal(movement.amount or 0)
        for movement in movements
        if movement.movement_type == "expense"
    )
    return {
        "movements": movements,
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
    }


def generate_cash_cut(
    cut_type,
    user,
    target_date=None,
    period_start=None,
    period_end=None,
    target_user=None,
):
    if period_start and period_end:
        start, end = period_start, period_end
    else:
        start, end = get_period_bounds(
            cut_type,
            reference_date=target_date,
        )
    paid_records_query = VehicleRecord.query.filter(
        VehicleRecord.weekly_prepayment_id.is_(None),
        VehicleRecord.paid_at.isnot(None),
        VehicleRecord.paid_at >= start,
        VehicleRecord.paid_at < end,
    )
    if target_user:
        paid_records_query = paid_records_query.filter(
            VehicleRecord.payment_user_id == target_user.id
        )
    paid_records = paid_records_query.all()

    vehicle_income = Decimal("0.00")
    vehicle_types = get_vehicle_types(include_inactive=True)
    by_type = {vehicle_type: {"count": 0, "income": 0.0} for vehicle_type in vehicle_types}

    for record in paid_records:
        by_type.setdefault(record.vehicle_type, {"count": 0, "income": 0.0})
        by_type[record.vehicle_type]["count"] += 1
        vehicle_income += Decimal(record.total_amount)
        by_type[record.vehicle_type]["income"] += float(record.total_amount)

    movement_totals = cash_movement_totals(start, end, target_user=target_user)
    extra_income = movement_totals["income"]
    total_expenses = movement_totals["expenses"]
    net_total = vehicle_income + extra_income - total_expenses
    by_type["_cash_movements"] = _cash_movement_breakdown(movement_totals["movements"])

    cut = CashCut(
        cut_type=cut_type,
        period_start=start,
        period_end=end,
        generated_by=user,
        target_user=target_user,
        total_income=net_total,
        extra_income=extra_income,
        total_expenses=total_expenses,
        net_total=net_total,
        total_pending=Decimal("0.00"),
        vehicles_served=len(paid_records),
        vehicles_paid=len(paid_records),
        breakdown_json=json.dumps(by_type),
    )
    db.session.add(cut)
    log_action(
        user,
        "cash_cut_created",
        "cash_cut",
        details={
            "cut_type": cut_type,
            "target_user_id": target_user.id if target_user else None,
        },
    )
    db.session.commit()
    return cut


def dashboard_metrics():
    day_start, day_end = get_period_bounds("daily")
    week_start, week_end = get_period_bounds("weekly")

    entries_today = VehicleRecord.query.filter(
        VehicleRecord.entry_at >= day_start,
        VehicleRecord.entry_at < day_end,
    ).count()
    inside_count = VehicleRecord.query.filter_by(status="Dentro del estacionamiento").count()
    exited_count = VehicleRecord.query.filter(VehicleRecord.exit_at.isnot(None)).count()
    vehicle_total_day = db.session.scalar(
        db.select(func.coalesce(func.sum(VehicleRecord.total_amount), 0)).where(
            VehicleRecord.weekly_prepayment_id.is_(None),
            VehicleRecord.paid_at.isnot(None),
            VehicleRecord.paid_at >= day_start,
            VehicleRecord.paid_at < day_end,
        )
    )
    vehicle_total_week = db.session.scalar(
        db.select(func.coalesce(func.sum(VehicleRecord.total_amount), 0)).where(
            VehicleRecord.weekly_prepayment_id.is_(None),
            VehicleRecord.paid_at.isnot(None),
            VehicleRecord.paid_at >= week_start,
            VehicleRecord.paid_at < week_end,
        )
    )
    pending_records_today = VehicleRecord.query.filter(
        VehicleRecord.entry_at >= day_start,
        VehicleRecord.entry_at < day_end,
        VehicleRecord.exit_at.is_(None),
        VehicleRecord.paid_at.is_(None),
        VehicleRecord.status != "Pagado",
    ).all()
    pending = Decimal("0.00")
    for record in pending_records_today:
        pricing = calculate_charge(
            record.vehicle_type,
            record.entry_at,
            utc_now(),
            stay_mode=record.stay_mode,
            contracted_days=record.contracted_days,
        )
        pending += (
            Decimal(pricing["total"])
            + Decimal(str(record.services_total_amount or 0))
        )

    vehicle_count = {vehicle_type: 0 for vehicle_type in get_vehicle_types(include_inactive=True)}
    active_counts = (
        db.session.query(VehicleRecord.vehicle_type, func.count(VehicleRecord.id))
        .filter_by(status="Dentro del estacionamiento")
        .group_by(VehicleRecord.vehicle_type)
        .all()
    )
    for vehicle_type, count in active_counts:
        vehicle_count[vehicle_type] = count

    day_movements = cash_movement_totals(day_start, day_end)
    week_movements = cash_movement_totals(week_start, week_end)
    total_day = Decimal(vehicle_total_day or 0) + day_movements["net"]
    total_week = Decimal(vehicle_total_week or 0) + week_movements["net"]

    return {
        "entries_today": entries_today,
        "inside_count": inside_count,
        "exited_count": exited_count,
        "total_day": float(total_day or 0),
        "total_week": float(total_week or 0),
        "vehicle_total_day": float(vehicle_total_day or 0),
        "cash_income_day": float(day_movements["income"] or 0),
        "cash_expense_day": float(day_movements["expenses"] or 0),
        "vehicle_total_week": float(vehicle_total_week or 0),
        "cash_income_week": float(week_movements["income"] or 0),
        "cash_expense_week": float(week_movements["expenses"] or 0),
        "pending_count": len(pending_records_today),
        "pending_total": float(pending or 0),
        "vehicle_count": vehicle_count,
    }


def _cash_movement_breakdown(movements):
    income = Decimal("0.00")
    expenses = Decimal("0.00")
    items = []
    for movement in movements:
        amount = Decimal(movement.amount or 0)
        if movement.movement_type == "expense":
            expenses += amount
        else:
            income += amount
        items.append(
            {
                "type": movement.movement_type,
                "label": movement.type_label,
                "description": movement.description,
                "amount": float(amount),
                "occurred_at": movement.occurred_at.isoformat()
                if movement.occurred_at
                else "",
                "created_by": movement.created_by.full_name if movement.created_by else "-",
            }
        )
    return {
        "income": float(income),
        "expense": float(expenses),
        "items": items,
    }


def _clean_tariff_payload(form_data):
    vehicle_type = normalize_vehicle_type(form_data.get("vehicle_type"))
    billing_scheme = clean_text(form_data.get("billing_scheme"), 30, "esquema de cobro").lower()
    if billing_scheme not in {scheme for scheme, _label in BILLING_SCHEMES}:
        raise ValueError("El esquema de cobro no es valido.")

    if billing_scheme == "flat":
        period_unit = "day"
    else:
        period_unit = clean_text(form_data.get("period_unit"), 20, "unidad de periodo").lower()
        if period_unit not in {unit for unit, _label in PERIOD_UNITS}:
            raise ValueError("La unidad de periodo no es valida.")

    rate_amount = _clean_decimal(form_data.get("rate_amount"), "tarifa base")
    min_charge_units = _clean_positive_int(
        form_data.get("min_charge_units") or "1",
        "cobro minimo",
    )
    offer_label = clean_optional_text(form_data.get("offer_label"), 120) or None
    offer_trigger_units = form_data.get("offer_trigger_units", "").strip()
    offer_price = form_data.get("offer_price", "").strip()

    if bool(offer_trigger_units) != bool(offer_price):
        raise ValueError("La oferta debe incluir unidades y precio promocional.")

    normalized_offer_units = None
    normalized_offer_price = None
    if offer_trigger_units:
        normalized_offer_units = _clean_positive_int(offer_trigger_units, "unidades de oferta")
        normalized_offer_price = _clean_decimal(offer_price, "precio promocional")
        if normalized_offer_units <= min_charge_units:
            raise ValueError("La oferta debe activarse por encima del cobro minimo.")

    return {
        "vehicle_type": vehicle_type,
        "billing_scheme": billing_scheme,
        "rate_amount": rate_amount,
        "period_unit": period_unit,
        "min_charge_units": min_charge_units,
        "offer_label": offer_label,
        "offer_trigger_units": normalized_offer_units,
        "offer_price": normalized_offer_price,
        "notes": clean_optional_text(form_data.get("notes"), 255) or None,
        "active": form_data.get("active") == "on",
    }


def _clean_decimal(raw_value, field_name):
    normalized = str(raw_value or "").strip().replace(",", "")
    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"El campo {field_name} debe ser numerico.") from exc
    if value <= 0:
        raise ValueError(f"El campo {field_name} debe ser mayor a cero.")
    return value.quantize(Decimal("0.01"))


def _clean_positive_int(raw_value, field_name):
    normalized = str(raw_value or "").strip()
    if not normalized.isdigit():
        raise ValueError(f"El campo {field_name} debe ser un entero positivo.")
    value = int(normalized)
    if value <= 0:
        raise ValueError(f"El campo {field_name} debe ser mayor a cero.")
    return value


def _clean_stay_mode(raw_value):
    stay_mode = clean_text(raw_value, 20, "modalidad").lower()
    if stay_mode not in {mode for mode, _label in RECORD_STAY_MODES}:
        raise ValueError("La modalidad seleccionada no es valida.")
    return stay_mode


def _clean_contracted_days(raw_value, stay_mode):
    if stay_mode != "weekly":
        return None
    return 6


def _clean_service_payload(form_data):
    service_wash = form_data.get("service_wash") == "on"
    service_oil_change = form_data.get("service_oil_change") == "on"
    raw_oil_price = (form_data.get("service_oil_price") or "").strip()
    if not service_oil_change:
        return service_wash, False, Decimal("0.00")
    oil_price = _clean_decimal(raw_oil_price or "0", "precio de aceite")
    return service_wash, True, oil_price


def _ensure_employee_role():
    employee_role = Role.query.filter_by(name="employee").first()
    if employee_role:
        return employee_role

    employee_role = Role(
        name="employee",
        description="Empleado de operacion",
    )
    db.session.add(employee_role)
    db.session.flush()
    return employee_role
