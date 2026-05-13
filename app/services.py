import json
from decimal import Decimal, InvalidOperation

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import (
    CashCut,
    Role,
    Tariff,
    User,
    VehicleRecord,
    WeeklyExitLog,
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


def get_vehicle_types(include_inactive=False):
    query = Tariff.query.order_by(Tariff.vehicle_type.asc())
    if not include_inactive:
        query = query.filter(Tariff.active.is_(True))
    return [tariff.vehicle_type for tariff in query.all()]


def get_tariffs(include_inactive=True):
    query = Tariff.query.order_by(Tariff.vehicle_type.asc())
    if not include_inactive:
        query = query.filter(Tariff.active.is_(True))
    return query.all()


def create_vehicle_record(form_data, user):
    physical_ticket_number = clean_ticket(form_data.get("ticket_number"))
    client_name = clean_text(form_data.get("client_name"), 120, "cliente")
    vehicle_type = clean_text(form_data.get("vehicle_type"), 50, "tipo de vehiculo")
    plate_number = clean_plate(form_data.get("plate_number"))
    stay_mode = _clean_stay_mode(form_data.get("stay_mode"))
    contracted_days = _clean_contracted_days(form_data.get("contracted_days"), stay_mode)
    service_wash, service_oil_change, service_oil_price = _clean_service_payload(form_data)
    notes = clean_optional_text(form_data.get("notes"), 500)

    if vehicle_type not in get_vehicle_types():
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
        service_wash=service_wash,
        service_oil_change=service_oil_change,
        service_oil_price=service_oil_price,
        notes=notes,
        entry_user=user,
    )
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
    db.session.commit()
    return record


def update_vehicle_record(record, form_data, user):
    record.physical_ticket_number = clean_ticket(form_data.get("ticket_number"))
    record.client_name = clean_text(form_data.get("client_name"), 120, "cliente")
    record.vehicle_type = clean_text(form_data.get("vehicle_type"), 50, "tipo de vehiculo")
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

    existing = VehicleRecord.query.filter(
        VehicleRecord.physical_ticket_number == record.physical_ticket_number,
        VehicleRecord.id != record.id,
        VehicleRecord.exit_at.is_(None),
    ).first()
    if existing:
        raise ValueError("Esa ficha fisica ya esta asignada a otro vehiculo dentro del estacionamiento.")

    if record.exit_at:
        pricing = calculate_charge(
            record.vehicle_type,
            record.entry_at,
            record.exit_at,
            stay_mode=record.stay_mode,
            contracted_days=record.contracted_days,
        )
        record.duration_seconds = pricing["duration_seconds"]
        record.applied_rate_label = pricing["rate_label"]
        record.total_amount = pricing["total"] + record.service_oil_price + (20 if record.service_wash else 0) + (40 if record.service_oil_change else 0)
        if record.services_total_amount:
            record.applied_rate_label = f"{record.applied_rate_label} | {record.services_label}"

    log_action(user, "vehicle_record_updated", "vehicle_record", record.id)
    db.session.commit()
    return record


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
    paid_at = utc_now()
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
    db.session.commit()
    return record


def register_weekly_exit(record, user):
    if record.is_hourly:
        raise ValueError("Solo los registros semanales permiten salidas parciales.")
    if record.exit_at:
        raise ValueError("Este registro semanal ya fue cerrado.")
    if record.status != "Dentro del estacionamiento":
        raise ValueError("Primero registra la entrada del dia antes de volver a sacar este vehiculo.")
    if not record.is_paid:
        raise ValueError("Primero registra el pago antes de registrar la salida semanal.")

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


def generate_cash_cut(cut_type, user):
    start, end = get_period_bounds("daily" if cut_type == "daily" else "weekly")
    paid_records = VehicleRecord.query.filter(
        VehicleRecord.paid_at.isnot(None),
        VehicleRecord.paid_at >= start,
        VehicleRecord.paid_at < end,
    ).all()
    pending_records = VehicleRecord.query.filter(
        VehicleRecord.exit_at.isnot(None),
        VehicleRecord.paid_at.is_(None),
        VehicleRecord.exit_at >= start,
        VehicleRecord.exit_at < end,
    ).all()

    total_income = Decimal("0.00")
    total_pending = Decimal("0.00")
    vehicle_types = get_vehicle_types(include_inactive=True)
    by_type = {vehicle_type: {"count": 0, "income": 0.0} for vehicle_type in vehicle_types}

    for record in paid_records:
        by_type.setdefault(record.vehicle_type, {"count": 0, "income": 0.0})
        by_type[record.vehicle_type]["count"] += 1
        total_income += Decimal(record.total_amount)
        by_type[record.vehicle_type]["income"] += float(record.total_amount)

    for record in pending_records:
        total_pending += Decimal(record.total_amount)

    cut = CashCut(
        cut_type=cut_type,
        period_start=start,
        period_end=end,
        generated_by=user,
        total_income=total_income,
        total_pending=total_pending,
        vehicles_served=len(paid_records) + len(pending_records),
        vehicles_paid=len(paid_records),
        breakdown_json=json.dumps(by_type),
    )
    db.session.add(cut)
    log_action(user, "cash_cut_created", "cash_cut", details={"cut_type": cut_type})
    db.session.commit()
    return cut


def dashboard_metrics():
    records = VehicleRecord.query.all()
    active_records = [
        record for record in records if record.status == "Dentro del estacionamiento"
    ]
    exited_records = [
        record
        for record in records
        if record.exit_at
    ]
    total_day = sum(
        float(record.total_amount)
        for record in records
        if record.is_paid
        and record.paid_at
        and ensure_utc(record.paid_at).date() == utc_now().date()
    )
    week_start, week_end = get_period_bounds("weekly")
    total_week = sum(
        float(record.total_amount)
        for record in records
        if record.is_paid
        and record.paid_at
        and week_start <= ensure_utc(record.paid_at) < week_end
    )
    pending = sum(
        float(record.total_amount)
        for record in records
        if record.exit_at and not record.is_paid
    )
    vehicle_count = {vehicle_type: 0 for vehicle_type in get_vehicle_types(include_inactive=True)}
    for record in active_records:
        vehicle_count[record.vehicle_type] = vehicle_count.get(record.vehicle_type, 0) + 1

    return {
        "inside_count": len(active_records),
        "exited_count": len(exited_records),
        "total_day": total_day,
        "total_week": total_week,
        "pending_total": pending,
        "vehicle_count": vehicle_count,
    }


def _clean_tariff_payload(form_data):
    vehicle_type = clean_text(form_data.get("vehicle_type"), 50, "tipo de vehiculo")
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
