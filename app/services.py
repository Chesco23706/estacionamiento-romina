import json
from decimal import Decimal

from sqlalchemy import func

from .extensions import db
from .models import CashCut, Role, User, VehicleRecord, get_period_bounds, log_action
from .pricing import calculate_charge, ensure_utc, utc_now
from .validators import (
    clean_optional_text,
    clean_plate,
    clean_text,
    clean_ticket,
    validate_password_strength,
)

VEHICLE_TYPES = ("Moto", "Automóvil", "Bicicleta", "Carrito callejero")
STATUS_OPTIONS = (
    "Dentro del estacionamiento",
    "Salida registrada",
    "Pagado",
    "Pendiente de pago",
)


def create_vehicle_record(form_data, user):
    ticket_number = clean_ticket(form_data.get("ticket_number"))
    client_name = clean_text(form_data.get("client_name"), 120, "cliente")
    vehicle_type = clean_text(form_data.get("vehicle_type"), 50, "tipo de vehículo")
    plate_number = clean_plate(form_data.get("plate_number"))
    notes = clean_optional_text(form_data.get("notes"), 500)

    if vehicle_type not in VEHICLE_TYPES:
        raise ValueError("El tipo de vehículo no es válido.")

    if VehicleRecord.query.filter_by(ticket_number=ticket_number).first():
        raise ValueError("El número de ticket ya existe.")

    record = VehicleRecord(
        ticket_number=ticket_number,
        client_name=client_name,
        vehicle_type=vehicle_type,
        plate_number=plate_number,
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
        {"ticket_number": ticket_number, "vehicle_type": vehicle_type},
    )
    db.session.commit()
    return record


def update_vehicle_record(record, form_data, user):
    record.ticket_number = clean_ticket(form_data.get("ticket_number"))
    record.client_name = clean_text(form_data.get("client_name"), 120, "cliente")
    record.vehicle_type = clean_text(form_data.get("vehicle_type"), 50, "tipo de vehículo")
    record.plate_number = clean_plate(form_data.get("plate_number"))
    record.status = clean_text(form_data.get("status"), 30, "estado")
    record.notes = clean_optional_text(form_data.get("notes"), 500)

    if record.vehicle_type not in VEHICLE_TYPES:
        raise ValueError("El tipo de vehículo no es válido.")
    if record.status not in STATUS_OPTIONS:
        raise ValueError("El estado seleccionado no es válido.")

    existing = VehicleRecord.query.filter(
        VehicleRecord.ticket_number == record.ticket_number,
        VehicleRecord.id != record.id,
    ).first()
    if existing:
        raise ValueError("Ese número de ticket ya está asignado a otro registro.")

    if record.exit_at:
        pricing = calculate_charge(record.vehicle_type, record.entry_at, record.exit_at)
        record.duration_seconds = pricing["duration_seconds"]
        record.applied_rate_label = pricing["rate_label"]
        record.total_amount = pricing["total"]

    log_action(user, "vehicle_record_updated", "vehicle_record", record.id)
    db.session.commit()
    return record


def register_exit(record, user):
    if record.exit_at:
        raise ValueError("La salida ya fue registrada previamente.")
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
    if not record.exit_at:
        raise ValueError("Primero debes registrar la salida del vehículo.")
    record.mark_paid()
    log_action(user, "vehicle_payment_registered", "vehicle_record", record.id)
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

    employee_role = Role.query.filter_by(name="employee").first()
    new_user = User(full_name=full_name, username=username, role=employee_role)
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.flush()
    log_action(user, "employee_created", "user", new_user.id, {"username": username})
    db.session.commit()
    return new_user


def reset_employee_password(target_user, new_password, actor):
    target_user.set_password(validate_password_strength(new_password))
    log_action(actor, "employee_password_reset", "user", target_user.id)
    db.session.commit()


def generate_cash_cut(cut_type, user):
    start, end = get_period_bounds("daily" if cut_type == "daily" else "weekly")
    records = VehicleRecord.query.filter(
        VehicleRecord.exit_at.isnot(None),
        VehicleRecord.exit_at >= start,
        VehicleRecord.exit_at < end,
    ).all()

    total_income = Decimal("0.00")
    total_pending = Decimal("0.00")
    by_type = {vehicle_type: {"count": 0, "income": 0.0} for vehicle_type in VEHICLE_TYPES}

    for record in records:
        by_type[record.vehicle_type]["count"] += 1
        if record.status == "Pagado":
            total_income += Decimal(record.total_amount)
            by_type[record.vehicle_type]["income"] += float(record.total_amount)
        elif record.status == "Pendiente de pago":
            total_pending += Decimal(record.total_amount)

    cut = CashCut(
        cut_type=cut_type,
        period_start=start,
        period_end=end,
        generated_by=user,
        total_income=total_income,
        total_pending=total_pending,
        vehicles_served=len(records),
        vehicles_paid=sum(1 for record in records if record.status == "Pagado"),
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
        if record.status in {"Pendiente de pago", "Pagado", "Salida registrada"}
    ]
    total_day = sum(
        float(record.total_amount)
        for record in records
        if record.status == "Pagado"
        and record.exit_at
        and ensure_utc(record.exit_at).date() == utc_now().date()
    )
    week_start, week_end = get_period_bounds("weekly")
    total_week = sum(
        float(record.total_amount)
        for record in records
        if record.status == "Pagado"
        and record.exit_at
        and week_start <= ensure_utc(record.exit_at) < week_end
    )
    pending = sum(
        float(record.total_amount)
        for record in records
        if record.status == "Pendiente de pago"
    )
    vehicle_count = {vehicle_type: 0 for vehicle_type in VEHICLE_TYPES}
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
