from io import BytesIO
from datetime import date, datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

from flask import (
    abort,
    Blueprint,
    Response,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import joinedload, selectinload

from .decorators import role_required
from .extensions import db
from .models import AuditLog, CashCut, CashMovement, Role, Tariff, User, VehicleRecord, WeeklyExitLog, WeeklyPrepayment, log_action
from .pricing import calculate_charge, ensure_utc, format_duration, utc_now
from .services import (
    BILLING_SCHEMES,
    CASH_MOVEMENT_TYPES,
    PERIOD_UNITS,
    RECORD_STAY_MODES,
    STATUS_OPTIONS,
    activate_weekly_prepayment,
    cash_movement_totals,
    create_cash_movement,
    create_employee,
    create_tariff,
    create_vehicle_record,
    create_weekly_prepayment,
    dashboard_metrics,
    delete_cash_movement,
    delete_employee,
    delete_vehicle_record,
    employee_schedule_rows,
    generate_cash_cut,
    get_tariffs,
    get_vehicle_types,
    is_employee_within_schedule,
    register_exit,
    register_payment,
    register_weekly_entry,
    register_weekly_exit,
    reset_employee_password,
    update_employee,
    update_employee_schedule,
    update_tariff,
    update_vehicle_record,
)
from .tickets import (
    build_cash_cut_pdf,
    build_cash_cut_ticket_pdf,
    build_current_cut_pdf,
    build_ticket_pdf,
    build_ticket_qr_svg,
)
from .validators import clean_text

main_bp = Blueprint("main", __name__)

RECORD_DISPLAY_OPTIONS = (
    joinedload(VehicleRecord.entry_user),
    joinedload(VehicleRecord.exit_user),
    joinedload(VehicleRecord.payment_user),
    selectinload(VehicleRecord.weekly_exit_logs).joinedload(WeeklyExitLog.created_by),
)

SPANISH_WEEKDAYS = (
    "lunes",
    "martes",
    "miercoles",
    "jueves",
    "viernes",
    "sabado",
    "domingo",
)

SPANISH_MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)

CASH_MOVEMENT_DELETE_USERNAMES = {"empleado2", "empleado02"}
VEHICLE_RECORD_DELETE_USERNAMES = {"empleado02"}
VEHICLE_ENTRY_DATE_EDIT_USERNAMES = {"empleado02"}


def can_delete_cash_movements(user=None):
    target_user = user or current_user
    if not getattr(target_user, "is_authenticated", False):
        return False
    return bool(
        getattr(target_user, "is_admin", False)
        or getattr(target_user, "username", "") in CASH_MOVEMENT_DELETE_USERNAMES
    )


def can_delete_vehicle_records(user=None):
    target_user = user or current_user
    if not getattr(target_user, "is_authenticated", False):
        return False
    return bool(
        getattr(target_user, "is_admin", False)
        or getattr(target_user, "username", "") in VEHICLE_RECORD_DELETE_USERNAMES
    )


def can_edit_vehicle_entry_dates(user=None):
    target_user = user or current_user
    if not getattr(target_user, "is_authenticated", False):
        return False
    return getattr(target_user, "username", "") in VEHICLE_ENTRY_DATE_EDIT_USERNAMES


def parse_record_filters():
    requested_status = request.args.get("status")
    requested_date = request.args.get("date", "").strip()
    requested_date_field = request.args.get("date_field", "").strip().lower()
    requested_stay_mode = request.args.get("stay_mode", "").strip().lower()
    requested_sort_by = request.args.get("sort_by", "").strip().lower()
    requested_sort_dir = request.args.get("sort_dir", "").strip().lower()
    return {
        "search": request.args.get("search", "").strip(),
        "status": requested_status.strip() if requested_status is not None else "",
        "vehicle_type": request.args.get("vehicle_type", "").strip(),
        "stay_mode": requested_stay_mode if requested_stay_mode in {"hourly", "weekly"} else "",
        "date": requested_date,
        "date_field": requested_date_field if requested_date_field in {"entry", "exit", "payment"} else "",
        "sort_by": requested_sort_by if requested_sort_by in {"entry", "exit"} else "",
        "sort_dir": requested_sort_dir if requested_sort_dir in {"asc", "desc"} else "",
    }


def vehicle_record_display_query():
    return VehicleRecord.query.options(*RECORD_DISPLAY_OPTIONS)


def record_exited_between_filter(day_start, day_end):
    return or_(
        and_(
            VehicleRecord.exit_at.isnot(None),
            VehicleRecord.exit_at >= day_start,
            VehicleRecord.exit_at < day_end,
        ),
        VehicleRecord.weekly_exit_logs.any(
            and_(
                WeeklyExitLog.exited_at >= day_start,
                WeeklyExitLog.exited_at < day_end,
            )
        ),
    )


def record_activity_between_filter(day_start, day_end):
    return or_(
        VehicleRecord.exit_at.is_(None),
        and_(VehicleRecord.entry_at >= day_start, VehicleRecord.entry_at < day_end),
        and_(VehicleRecord.updated_at >= day_start, VehicleRecord.updated_at < day_end),
        and_(
            VehicleRecord.paid_at.isnot(None),
            VehicleRecord.paid_at >= day_start,
            VehicleRecord.paid_at < day_end,
        ),
        record_exited_between_filter(day_start, day_end),
    )


def record_has_exit_between(record, day_start, day_end):
    if record.exit_at:
        exit_moment = ensure_utc(record.exit_at)
        if day_start <= exit_moment < day_end:
            return True
    return any(
        day_start <= ensure_utc(exit_log.exited_at) < day_end
        for exit_log in record.weekly_exit_logs
        if exit_log.exited_at
    )


def record_has_activity_between(record, day_start, day_end):
    if record.exit_at is None:
        return True
    for moment in (record.entry_at, record.updated_at, record.paid_at, record.exit_at):
        if moment:
            target = ensure_utc(moment)
            if day_start <= target < day_end:
                return True
    return record_has_exit_between(record, day_start, day_end)


def enrich_records(records, reference_time=None):
    records = list(records)
    for record in records:
        enrich_record_display(record, reference_time=reference_time)
    attach_record_history(records)
    return records


def attach_record_history(records):
    record_ids = [record.id for record in records if record and record.id]
    if not record_ids:
        return

    audit_logs_by_record = defaultdict(list)
    audit_logs = (
        AuditLog.query.options(joinedload(AuditLog.user))
        .filter(
            AuditLog.entity_type == "vehicle_record",
            AuditLog.entity_id.in_(record_ids),
        )
        .order_by(AuditLog.created_at.asc())
        .all()
    )
    for audit_log in audit_logs:
        audit_logs_by_record[audit_log.entity_id].append(audit_log)

    for record in records:
        record.history_events = build_record_history_events(
            record,
            audit_logs_by_record.get(record.id, []),
        )


def build_record_history_events(record, audit_logs):
    events = [
        {
            "moment": record.entry_at,
            "event": "Registro de entrada",
            "status": "Dentro del estacionamiento",
            "user": record.entry_user.full_name if record.entry_user else "-",
        }
    ]
    if record.paid_at:
        events.append(
            {
                "moment": record.paid_at,
                "event": "Pago recibido",
                "status": "Pagado",
                "user": record.payment_user.full_name if record.payment_user else "-",
            }
        )
    for exit_log in record.weekly_exit_logs:
        events.append(
            {
                "moment": exit_log.exited_at,
                "event": f"Salida del dia {exit_log.day_number}",
                "status": "Salida registrada",
                "user": exit_log.created_by.full_name if exit_log.created_by else "-",
            }
        )
    if record.exit_at:
        events.append(
            {
                "moment": record.exit_at,
                "event": "Salida final",
                "status": record.status,
                "user": record.exit_user.full_name if record.exit_user else "-",
            }
        )

    audit_event_labels = {
        "weekly_entry_registered": ("Entrada del dia", "Dentro del estacionamiento"),
        "vehicle_record_updated": ("Edicion de registro", "Registro actualizado"),
    }
    for audit_log in audit_logs:
        event_info = audit_event_labels.get(audit_log.action)
        if not event_info:
            continue
        event, status = event_info
        events.append(
            {
                "moment": audit_log.created_at,
                "event": event,
                "status": status,
                "user": audit_log.user.full_name if audit_log.user else "-",
            }
        )

    return sorted(events, key=record_history_sort_key)


def record_history_sort_key(event):
    return ensure_utc(event["moment"]) or utc_now()


def build_records_query(filters):
    query = vehicle_record_display_query()
    search = filters["search"]
    if search:
        if search.isdigit():
            query = query.filter(
                or_(
                    VehicleRecord.physical_ticket_number == search,
                    (
                        VehicleRecord.physical_ticket_number.is_(None)
                        & (VehicleRecord.ticket_number == search)
                    ),
                )
            )
        else:
            like_value = f"%{search}%"
            query = query.filter(
                or_(
                    VehicleRecord.physical_ticket_number == search,
                    (
                        VehicleRecord.physical_ticket_number.is_(None)
                        & (VehicleRecord.ticket_number == search)
                    ),
                    VehicleRecord.client_name.ilike(like_value),
                    VehicleRecord.plate_number.ilike(like_value),
                    VehicleRecord.vehicle_type.ilike(like_value),
                )
            )
    if filters["status"]:
        status = filters["status"]
        if status == "Pagado":
            query = query.filter(VehicleRecord.paid_at.isnot(None))
        elif status == "Pendiente de pago":
            query = query.filter(
                VehicleRecord.paid_at.is_(None),
                VehicleRecord.exit_at.is_(None),
            )
        else:
            query = query.filter(VehicleRecord.status == status)
    if filters["vehicle_type"]:
        query = query.filter(VehicleRecord.vehicle_type == filters["vehicle_type"])
    if filters.get("stay_mode"):
        query = query.filter(VehicleRecord.stay_mode == filters["stay_mode"])
    if filters.get("date"):
        try:
            requested_day = datetime.fromisoformat(filters["date"]).date()
            day_start, day_end = get_local_day_bounds(requested_day)
            if filters.get("status") == "Dentro del estacionamiento":
                target_column = None
            elif not filters.get("date_field") and not filters.get("status"):
                query = query.filter(record_activity_between_filter(day_start, day_end))
                target_column = None
            else:
                date_field = filters.get("date_field") or ("payment" if filters.get("status") == "Pagado" else "entry")
                target_column = VehicleRecord.entry_at
                if date_field == "exit":
                    query = query.filter(record_exited_between_filter(day_start, day_end))
                    target_column = None
                elif date_field == "payment":
                    target_column = VehicleRecord.paid_at
            if target_column is not None:
                query = query.filter(
                    target_column.isnot(None),
                    target_column >= day_start,
                    target_column < day_end,
                )
        except ValueError:
            pass

    if filters.get("sort_by") == "entry":
        entry_order = VehicleRecord.entry_at.asc() if filters.get("sort_dir") == "asc" else VehicleRecord.entry_at.desc()
        return query.order_by(entry_order)
    if filters.get("sort_by") == "exit":
        exit_order = VehicleRecord.exit_at.asc() if filters.get("sort_dir") == "asc" else VehicleRecord.exit_at.desc()
        return query.order_by(VehicleRecord.exit_at.is_(None), exit_order, VehicleRecord.entry_at.desc())

    order_field = filters.get("date_field") or ("payment" if filters.get("status") == "Pagado" else "")
    if order_field == "payment":
        return query.order_by(VehicleRecord.paid_at.desc(), VehicleRecord.entry_at.desc())
    if order_field == "exit":
        return query.order_by(VehicleRecord.exit_at.desc(), VehicleRecord.entry_at.desc())
    if filters.get("date") and not filters.get("date_field") and not filters.get("status"):
        return query.order_by(VehicleRecord.updated_at.desc(), VehicleRecord.entry_at.desc())
    if filters.get("status") == "Pagado":
        return query.order_by(VehicleRecord.paid_at.desc(), VehicleRecord.entry_at.desc())
    if filters.get("status") == "Salida registrada":
        return query.order_by(VehicleRecord.exit_at.desc(), VehicleRecord.entry_at.desc())
    return query.order_by(VehicleRecord.entry_at.desc())


def build_sort_link_args(filters, sort_by):
    current_sort_by = filters.get("sort_by") or ""
    current_sort_dir = filters.get("sort_dir") or ""
    next_sort_dir = "asc" if current_sort_by == sort_by and current_sort_dir == "desc" else "desc"
    return {
        "search": filters.get("search", ""),
        "status": filters.get("status", ""),
        "vehicle_type": filters.get("vehicle_type", ""),
        "stay_mode": filters.get("stay_mode", ""),
        "date": filters.get("date", ""),
        "date_field": filters.get("date_field", ""),
        "sort_by": sort_by,
        "sort_dir": next_sort_dir,
    }


def natural_ticket_sort_key(ticket_number):
    ticket_text = (ticket_number or "").strip()
    digits = "".join(character for character in ticket_text if character.isdigit())
    if digits:
        return (0, int(digits), ticket_text.lower())
    return (1, ticket_text.lower())


def parse_ticket_slot_number(ticket_number):
    ticket_text = (ticket_number or "").strip()
    if not ticket_text:
        return None
    digits = "".join(character for character in ticket_text if character.isdigit())
    if not digits:
        return None
    slot_number = int(digits)
    if 1 <= slot_number <= 100:
        return slot_number
    return None


def ticket_board_status_theme(record):
    if not record:
        return "available"
    state = ticket_board_color_flags(record)[-1]
    if state == "weekly-out":
        return "weekly-out"
    return state


def ticket_board_color_flags(record):
    if not record:
        return ("available",)

    flags = ["weekly" if not record.is_hourly else "hourly"]

    if record.status == "Indeciso":
        flags.append("uncertain")
    elif (
        not record.is_hourly
        and record.vehicle_type == "Moto"
        and record.status == "Salida registrada"
    ):
        flags.append("weekly-out")
    elif record.status == "Dentro del estacionamiento":
        flags.append("inside")
    elif record.status in {"Salida registrada", "Pendiente de pago"}:
        flags.append("weekly-out" if not record.is_hourly else "inside")
    elif record.is_paid:
        flags.append("inside")
    else:
        flags.append("inside")
    return tuple(flags)


def ticket_board_color_label(color_flags):
    labels = {
        "available": "Disponible",
        "hourly": "Por hora",
        "weekly": "Por semana",
        "inside": "Adentro del estacionamiento",
        "uncertain": "Indeciso",
        "weekly-out": "Moto semanal salio",
    }
    return " + ".join(labels[color_flag] for color_flag in color_flags)


def ticket_board_color_style(color_flags):
    colors = {
        "available": ("#e9edf1", "#111a24"),
        "hourly": ("#b8f1eb", "#0f5f59"),
        "weekly": ("#8fbdff", "#1f68c8"),
        "inside": ("#9fe3ad", "#1f7a37"),
        "uncertain": ("#ff9b9b", "#b83232"),
        "weekly-out": ("#c6a4ff", "#7147c7"),
    }
    parts = []
    for index, color_flag in enumerate(color_flags[:4], start=1):
        light, dark = colors[color_flag]
        parts.append(f"--ticket-color-{index}: {light}")
        parts.append(f"--ticket-dark-color-{index}: {dark}")
    return "; ".join(parts)


def build_ticket_board():
    latest_by_ticket = {}
    for record in (
        vehicle_record_display_query()
        .filter(VehicleRecord.exit_at.is_(None))
        .order_by(VehicleRecord.entry_at.desc())
        .all()
    ):
        slot_number = parse_ticket_slot_number(record.display_ticket_number)
        if slot_number and slot_number not in latest_by_ticket:
            latest_by_ticket[slot_number] = record

    board_records = []
    for slot_number in range(1, 101):
        record = latest_by_ticket.get(slot_number)
        color_flags = ticket_board_color_flags(record)
        board_records.append(
            {
                "slot_number": slot_number,
                "slot_label": f"{slot_number:03d}",
                "record": record,
                "status_theme": ticket_board_status_theme(record),
                "color_signature": "-".join(color_flags),
                "color_label": ticket_board_color_label(color_flags),
                "color_count": len(color_flags),
                "color_style": ticket_board_color_style(color_flags),
            }
        )

    metrics = {
        "available": sum(1 for slot in board_records if not slot["record"]),
        "occupied": sum(1 for slot in board_records if slot["record"]),
        "inside": sum(
            1
            for slot in board_records
            if slot["record"] and slot["record"].status == "Dentro del estacionamiento"
        ),
        "weekly_inside": sum(
            1
            for slot in board_records
            if slot["record"]
            and not slot["record"].is_hourly
            and slot["record"].status == "Dentro del estacionamiento"
        ),
        "uncertain": sum(
            1
            for slot in board_records
            if slot["record"] and slot["record"].status == "Indeciso"
        ),
    }
    return board_records, metrics


def parse_dashboard_period():
    period = (request.args.get("period") or "today").strip().lower()
    if period not in {"today", "week", "month", "custom"}:
        period = "today"

    now_local = localize_datetime(utc_now())
    if period == "week":
        start_date = (now_local - timedelta(days=now_local.weekday())).date()
        end_date = start_date + timedelta(days=6)
    elif period == "month":
        start_date = date(now_local.year, now_local.month, 1)
        next_month = date(now_local.year + (1 if now_local.month == 12 else 0), 1 if now_local.month == 12 else now_local.month + 1, 1)
        end_date = next_month - timedelta(days=1)
    elif period == "custom":
        try:
            start_date = parse_cut_date(request.args.get("date_from")) or now_local.date()
            end_date = parse_cut_date(request.args.get("date_to")) or start_date
        except ValueError:
            start_date = now_local.date()
            end_date = start_date
        if end_date < start_date:
            start_date, end_date = end_date, start_date
    else:
        start_date = now_local.date()
        end_date = start_date

    start_at, _unused = get_local_day_bounds(start_date)
    _unused, end_at = get_local_day_bounds(end_date)
    return {
        "period": period,
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "start_at": start_at,
        "end_at": end_at,
        "label": f"{start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
    }


def dashboard_chart_data(period_filters):
    vehicle_types = get_vehicle_types(include_inactive=True)
    chart_rows = {
        vehicle_type: {"vehicle_type": vehicle_type, "entries": 0, "income": 0.0}
        for vehicle_type in vehicle_types
    }
    entry_counts = (
        db.session.query(VehicleRecord.vehicle_type, func.count(VehicleRecord.id))
        .filter(
            VehicleRecord.entry_at >= period_filters["start_at"],
            VehicleRecord.entry_at < period_filters["end_at"],
        )
        .group_by(VehicleRecord.vehicle_type)
        .all()
    )
    income_totals = (
        db.session.query(
            VehicleRecord.vehicle_type,
            func.coalesce(func.sum(VehicleRecord.total_amount), 0),
        )
        .filter(
            VehicleRecord.paid_at.isnot(None),
            VehicleRecord.paid_at >= period_filters["start_at"],
            VehicleRecord.paid_at < period_filters["end_at"],
        )
        .group_by(VehicleRecord.vehicle_type)
        .all()
    )
    for vehicle_type, count in entry_counts:
        chart_rows.setdefault(vehicle_type, {"vehicle_type": vehicle_type, "entries": 0, "income": 0.0})
        chart_rows[vehicle_type]["entries"] = int(count or 0)
    for vehicle_type, income in income_totals:
        chart_rows.setdefault(vehicle_type, {"vehicle_type": vehicle_type, "entries": 0, "income": 0.0})
        chart_rows[vehicle_type]["income"] = float(income or 0)

    rows = list(chart_rows.values())
    max_entries = max((row["entries"] for row in rows), default=0)
    max_income = max((row["income"] for row in rows), default=0)
    for row in rows:
        row["entries_percent"] = (row["entries"] / max_entries * 100) if max_entries else 0
        row["income_percent"] = (row["income"] / max_income * 100) if max_income else 0
    return rows


def record_matches_filters(record, filters, *, include_date=True):
    status = (filters.get("status") or "").strip()
    search = (filters.get("search") or "").strip().lower()
    raw_search = (filters.get("search") or "").strip()
    vehicle_type = (filters.get("vehicle_type") or "").strip()
    stay_mode = (filters.get("stay_mode") or "").strip()
    record_day = localize_datetime(record.entry_at).date().isoformat() if record.entry_at else ""

    if search:
        display_ticket = (record.display_ticket_number or "").strip()
        normalized_display_ticket = display_ticket.lstrip("0") or "0"
        normalized_search = raw_search.lstrip("0") or "0"
        ticket_match = display_ticket == raw_search or normalized_display_ticket == normalized_search
        if raw_search.isdigit():
            if not ticket_match:
                return False
        else:
            textual_haystack = " ".join(
                [
                    record.client_name or "",
                    record.plate_number or "",
                    record.vehicle_type or "",
                ]
            ).lower()
            text_match = search in textual_haystack
            if not ticket_match and not text_match:
                return False

    if vehicle_type and record.vehicle_type != vehicle_type:
        return False
    if stay_mode and record.stay_mode != stay_mode:
        return False

    if include_date and filters.get("date") and status != "Dentro del estacionamiento":
        try:
            requested_day = datetime.fromisoformat(filters["date"]).date()
            day_start, day_end = get_local_day_bounds(requested_day)
        except ValueError:
            return False
        if not filters.get("date_field") and not status:
            if not record_has_activity_between(record, day_start, day_end):
                return False
            target_moment = None
        else:
            date_field = filters.get("date_field") or ("payment" if status == "Pagado" else "entry")
            target_moment = record.entry_at
            if date_field == "exit":
                if not record_has_exit_between(record, day_start, day_end):
                    return False
                target_moment = None
            elif date_field == "payment":
                target_moment = record.paid_at

        target_day = localize_datetime(target_moment).date().isoformat() if target_moment else filters["date"]
        if filters["date"] != target_day:
            return False

    if status == "Pagado" and not record.is_paid:
        return False
    if status == "Pendiente de pago" and (record.is_paid or record.exit_at):
        return False
    if status and status not in {"Pagado", "Pendiente de pago"} and record.status != status:
        return False

    return True


def get_local_timezone():
    timezone_name = current_app.config.get("APP_TIMEZONE", "America/Mexico_City")
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo("America/Mexico_City")


def localize_datetime(moment):
    return ensure_utc(moment).astimezone(get_local_timezone())


def get_local_day_bounds(reference_day=None):
    timezone = get_local_timezone()
    if reference_day is None:
        local_reference = localize_datetime(utc_now())
        target_day = local_reference.date()
    else:
        target_day = reference_day
    local_start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone)
    local_end = local_start + timedelta(days=1)
    return ensure_utc(local_start), ensure_utc(local_end)


def get_local_date_range_bounds(start_date, end_date):
    if end_date < start_date:
        raise ValueError("La fecha final no puede ser anterior a la fecha inicial.")
    start, _unused = get_local_day_bounds(start_date)
    _unused, end = get_local_day_bounds(end_date)
    return start, end


def format_operating_date_label(moment):
    local_moment = localize_datetime(moment)
    weekday = SPANISH_WEEKDAYS[local_moment.weekday()]
    month = SPANISH_MONTHS[local_moment.month - 1]
    return f"{weekday}, {local_moment.day:02d} {month} {local_moment.year}".upper()


def format_operating_time_label(moment):
    local_moment = localize_datetime(moment)
    hour = local_moment.strftime("%I:%M")
    suffix = "a. m." if local_moment.hour < 12 else "p. m."
    return f"{hour} {suffix}"


def enrich_record_display(record, reference_time=None):
    target_time = reference_time or utc_now()
    if record.exit_at or record.is_paid:
        record.live_total_amount = float(record.total_amount or 0)
        record.live_rate_label = record.applied_rate_label or "-"
        return record

    pricing = calculate_charge(
        record.vehicle_type,
        record.entry_at,
        target_time,
        stay_mode=record.stay_mode,
        contracted_days=record.contracted_days,
    )
    record.live_total_amount = float(pricing["total"]) + float(record.services_total_amount or 0)
    record.live_rate_label = pricing["rate_label"]
    if record.services_total_amount:
        record.live_rate_label = f"{pricing['rate_label']} | {record.services_label}"
    return record


@main_bp.app_template_filter("money")
def money_filter(value):
    return f"${float(value):,.2f}"


@main_bp.app_template_filter("dt")
def datetime_filter(value):
    if not value:
        return "-"
    return localize_datetime(value).strftime("%Y-%m-%d %H:%M:%S")


@main_bp.app_template_filter("datetime_local")
def datetime_local_filter(value):
    if not value:
        return ""
    return localize_datetime(value).strftime("%Y-%m-%dT%H:%M")


def format_datetime_for_ticket(value):
    if not value:
        return "-"
    return localize_datetime(value).strftime("%d/%m/%Y %H:%M")


def format_cut_filename_date(value):
    return localize_datetime(value).strftime("%d-%m-%Y")


def cash_cut_file_basename(cut):
    start_local = localize_datetime(cut.period_start)
    end_local = localize_datetime(cut.period_end - timedelta(seconds=1))
    if cut.cut_type == "daily":
        return f"Corte de {format_cut_filename_date(cut.period_start)}"
    if cut.cut_type == "monthly":
        return f"Corte mensual de {start_local.strftime('%m-%Y')}"
    if cut.cut_type == "custom":
        return (
            f"Corte de {start_local.strftime('%d-%m-%Y')} - "
            f"{end_local.strftime('%d-%m-%Y')}"
        )

    weekday = SPANISH_WEEKDAYS[start_local.weekday()]
    return (
        f"Corte semanal del {weekday} "
        f"{start_local.strftime('%d-%m-%Y')} - {end_local.strftime('%d-%m-%Y')}"
    )


def cash_cut_type_label(cut_type):
    labels = {
        "daily": "Diario",
        "weekly": "Semanal",
        "monthly": "Mensual",
        "custom": "Fecha especifica",
    }
    return labels.get(cut_type, cut_type)


def current_cut_records():
    day_start, day_end = get_local_day_bounds()
    active_records = (
        vehicle_record_display_query()
        .filter(VehicleRecord.exit_at.is_(None))
        .order_by(VehicleRecord.entry_at.desc())
        .all()
    )
    paid_today_records = (
        vehicle_record_display_query()
        .filter(
            VehicleRecord.paid_at.isnot(None),
            VehicleRecord.paid_at >= day_start,
            VehicleRecord.paid_at < day_end,
        )
        .order_by(VehicleRecord.paid_at.desc(), VehicleRecord.entry_at.desc())
        .all()
    )
    return active_records, paid_today_records


def parse_cut_date(raw_value):
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Selecciona una fecha valida para el corte.") from exc


def require_cut_date(raw_value, field_label):
    parsed_date = parse_cut_date(raw_value)
    if not parsed_date:
        raise ValueError(f"Selecciona {field_label}.")
    return parsed_date


def parse_cut_month(raw_value):
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise ValueError("Selecciona un mes valido para el corte.") from exc


def require_cut_month(raw_value):
    parsed_month = parse_cut_month(raw_value)
    if not parsed_month:
        raise ValueError("Selecciona el mes del corte.")
    return parsed_month


def cut_records_for_period(cut):
    query = (
        vehicle_record_display_query()
        .filter(
            VehicleRecord.paid_at.isnot(None),
            VehicleRecord.paid_at >= cut.period_start,
            VehicleRecord.paid_at < cut.period_end,
            VehicleRecord.weekly_prepayment_id.is_(None),
        )
    )
    if cut.target_user_id:
        query = query.filter(VehicleRecord.payment_user_id == cut.target_user_id)
    paid_records = query.order_by(VehicleRecord.paid_at.asc(), VehicleRecord.entry_at.asc()).all()
    enrich_records(paid_records)
    return paid_records


@main_bp.app_context_processor
def inject_now():
    return {
        "now_utc": utc_now,
        "can_delete_cash_movements": can_delete_cash_movements(),
        "can_delete_vehicle_records": can_delete_vehicle_records(),
        "can_edit_vehicle_entry_dates": can_edit_vehicle_entry_dates(),
    }


@main_bp.route("/health")
def health():
    db.session.execute(db.text("SELECT 1"))
    return {"status": "ok"}, 200


@main_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter(db.func.lower(User.username) == username).first()
        if not user or not user.check_password(password):
            if user:
                user.register_failed_login()
                db.session.commit()
            flash("Usuario o contraseña inválidos.", "danger")
            return render_template("login.html")

        if user.is_locked():
            flash("Tu cuenta está temporalmente bloqueada por intentos fallidos.", "danger")
            return render_template("login.html")

        if not user.is_active_user:
            flash("Tu usuario está desactivado. Contacta al administrador.", "danger")
            return render_template("login.html")

        if not is_employee_within_schedule(user):
            flash("Tu cuenta solo puede usarse dentro del horario asignado.", "danger")
            return render_template("login.html")

        login_user(user)
        user.last_login_at = utc_now()
        user.reset_login_guard()
        log_action(user, "login", "session", details={"username": username})
        db.session.commit()
        return redirect(url_for("main.dashboard"))

    return render_template("login.html")


@main_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    log_action(current_user, "logout", "session")
    db.session.commit()
    logout_user()
    flash("Sesión cerrada correctamente.", "success")
    return redirect(url_for("main.login"))


@main_bp.route("/")
@login_required
def dashboard():
    now_local = localize_datetime(utc_now())

    def collector_rows_for_period(start, end):
        vehicle_totals = dict(
            db.session.query(
                VehicleRecord.payment_user_id,
                func.coalesce(func.sum(VehicleRecord.total_amount), 0),
            )
            .filter(
                VehicleRecord.paid_at.isnot(None),
                VehicleRecord.paid_at >= start,
                VehicleRecord.paid_at < end,
                VehicleRecord.payment_user_id.isnot(None),
                VehicleRecord.weekly_prepayment_id.is_(None),
            )
            .group_by(VehicleRecord.payment_user_id)
            .all()
        )
        movement_rows = (
            db.session.query(
                CashMovement.created_by_user_id,
                CashMovement.movement_type,
                func.coalesce(func.sum(CashMovement.amount), 0),
            )
            .filter(
                CashMovement.occurred_at >= start,
                CashMovement.occurred_at < end,
            )
            .group_by(CashMovement.created_by_user_id, CashMovement.movement_type)
            .all()
        )
        movement_totals = {}
        for user_id, movement_type, amount in movement_rows:
            movement_totals.setdefault(user_id, {"income": 0.0, "expense": 0.0})
            movement_totals[user_id][movement_type] = float(amount or 0)

        def row_for_user(user):
            vehicle_total = float(vehicle_totals.get(user.id, 0) or 0)
            movements = movement_totals.get(user.id, {})
            cash_income = float(movements.get("income", 0) or 0)
            cash_expense = float(movements.get("expense", 0) or 0)
            return {
                "user": user,
                "vehicle_total": vehicle_total,
                "cash_income": cash_income,
                "cash_expense": cash_expense,
                "total": vehicle_total + cash_income - cash_expense,
            }

        return [
            row_for_user(user)
            for user in User.query.order_by(User.full_name.asc()).all()
        ]

    if current_user.is_admin:
        day_start, day_end = get_local_day_bounds()
        metrics = dashboard_metrics()
        chart_filters = parse_dashboard_period()
        chart_rows = dashboard_chart_data(chart_filters)
        return render_template(
            "dashboard.html",
            metrics=metrics,
            collector_rows=collector_rows_for_period(day_start, day_end),
            chart_filters=chart_filters,
            chart_rows=chart_rows,
            cash_movement_types=CASH_MOVEMENT_TYPES,
            format_duration=format_duration,
            now_local=now_local,
            now_local_label=format_operating_date_label(now_local),
            now_time_label=format_operating_time_label(now_local),
            vehicle_types=get_vehicle_types(include_inactive=True),
            status_options=STATUS_OPTIONS,
        )

    today_view = request.args.get("view", "dentro").strip().lower() or "dentro"
    day_start, day_end = get_local_day_bounds()
    today_records = (
        vehicle_record_display_query()
        .filter(
            VehicleRecord.entry_at >= day_start,
            VehicleRecord.entry_at < day_end,
        )
        .order_by(VehicleRecord.entry_at.desc())
        .all()
    )
    current_inside_all_records = (
        vehicle_record_display_query()
        .filter(VehicleRecord.status == "Dentro del estacionamiento")
        .order_by(VehicleRecord.entry_at.desc())
        .all()
    )
    paid_today_records = (
        vehicle_record_display_query()
        .filter(
            VehicleRecord.paid_at.isnot(None),
            VehicleRecord.paid_at >= day_start,
            VehicleRecord.paid_at < day_end,
        )
        .order_by(VehicleRecord.paid_at.desc())
        .all()
    )
    day_movement_totals = cash_movement_totals(day_start, day_end)
    today_cash_movements = list(reversed(day_movement_totals["movements"]))[:6]
    exited_today_records = (
        vehicle_record_display_query()
        .filter(record_exited_between_filter(day_start, day_end))
        .order_by(VehicleRecord.exit_at.desc(), VehicleRecord.entry_at.desc())
        .all()
    )
    if today_view == "pagados":
        filtered_today_records = paid_today_records
    elif today_view == "no_pagados":
        filtered_today_records = [record for record in current_inside_all_records if not record.is_paid]
    elif today_view == "salidos":
        filtered_today_records = exited_today_records
    else:
        filtered_today_records = current_inside_all_records

    enrich_records(filtered_today_records)
    employee_table_records = (
        filtered_today_records if today_view == "dentro" else filtered_today_records[:10]
    )

    vehicle_income_today = sum(
        float(record.total_amount) for record in paid_today_records
        if not record.weekly_prepayment_id
    )
    user_paid_today_records = [
        record for record in paid_today_records
        if record.payment_user_id == current_user.id and not record.weekly_prepayment_id
    ]
    user_vehicle_income_today = sum(
        float(record.total_amount or 0) for record in user_paid_today_records
    )
    user_movement_totals = cash_movement_totals(
        day_start,
        day_end,
        target_user=current_user,
    )
    user_cash_income_today = float(user_movement_totals["income"] or 0)
    user_cash_expense_today = float(user_movement_totals["expenses"] or 0)
    user_total_day = (
        user_vehicle_income_today
        + user_cash_income_today
        - user_cash_expense_today
    )
    pending_records = [record for record in current_inside_all_records if not record.is_paid]
    pending_total = 0.0
    for record in pending_records:
        pricing = calculate_charge(
            record.vehicle_type,
            record.entry_at,
            utc_now(),
            stay_mode=record.stay_mode,
            contracted_days=record.contracted_days,
        )
        pending_total += float(pricing["total"]) + float(record.services_total_amount or 0)
    employee_metrics = {
        "today_count": len(today_records),
        "inside_count": VehicleRecord.query.filter(
            VehicleRecord.status == "Dentro del estacionamiento"
        ).count(),
        "uncertain_count": VehicleRecord.query.filter(
            VehicleRecord.status == "Indeciso",
            VehicleRecord.exit_at.is_(None),
        ).count(),
        "total_day": vehicle_income_today + float(day_movement_totals["net"] or 0),
        "vehicle_income_day": vehicle_income_today,
        "cash_income_day": float(day_movement_totals["income"] or 0),
        "cash_expense_day": float(day_movement_totals["expenses"] or 0),
        "paid_today_count": len(paid_today_records),
        "user_paid_today_count": len(user_paid_today_records),
        "user_vehicle_income_day": user_vehicle_income_today,
        "user_cash_income_day": user_cash_income_today,
        "user_cash_expense_day": user_cash_expense_today,
        "user_total_day": user_total_day,
        "unpaid_today_count": len(pending_records),
        "pending_count": len(pending_records),
        "pending_total": pending_total,
        "exited_today_count": len(exited_today_records),
    }

    return render_template(
        "dashboard.html",
        today_records=employee_table_records,
        today_cash_movements=today_cash_movements,
        collector_rows=collector_rows_for_period(day_start, day_end)
        if current_user.has_admin_access
        else [],
        cash_movement_types=CASH_MOVEMENT_TYPES,
        employee_metrics=employee_metrics,
        today_view=today_view,
        today_label=now_local.strftime("%d/%m/%Y"),
        format_duration=format_duration,
        now_local=now_local,
        now_local_label=format_operating_date_label(now_local),
        now_time_label=format_operating_time_label(now_local),
        vehicle_types=get_vehicle_types(include_inactive=True),
        status_options=STATUS_OPTIONS,
    )


@main_bp.route("/records")
@login_required
def records_page():
    filters = parse_record_filters()
    if not current_user.is_admin and not request.args:
        filters["date"] = localize_datetime(utc_now()).date().isoformat()
    records = build_records_query(filters).all()
    sort_links = {
        "entry": build_sort_link_args(filters, "entry"),
        "exit": build_sort_link_args(filters, "exit"),
    }
    pending_checkout_records = []
    if filters["status"] == "Dentro del estacionamiento":
        pending_checkout_records = (
            vehicle_record_display_query()
            .filter(
                VehicleRecord.status == "Dentro del estacionamiento",
                VehicleRecord.paid_at.isnot(None),
                VehicleRecord.exit_at.is_(None),
            )
            .order_by(VehicleRecord.paid_at.desc())
            .limit(8)
            .all()
        )
    enrich_records(records)
    enrich_records(pending_checkout_records)
    if not current_user.is_admin:
        show_current_records = not bool(request.args)
        current_records = (
            vehicle_record_display_query()
            .filter(VehicleRecord.exit_at.is_(None))
            .order_by(VehicleRecord.entry_at.desc())
            .all()
        ) if show_current_records else []
        if show_current_records:
            current_records = [
                record
                for record in current_records
                if record_matches_filters(record, filters, include_date=False)
            ]
        enrich_records(current_records)
        return render_template(
            "records.html",
            records=records,
            current_records=current_records,
            show_current_records=show_current_records,
            pending_checkout_records=pending_checkout_records,
            filters=filters,
            sort_links=sort_links,
            vehicle_types=get_vehicle_types(),
            status_options=STATUS_OPTIONS,
            format_duration=format_duration,
            today_filter_value=localize_datetime(utc_now()).date().isoformat(),
        )
    return render_template(
        "records.html",
        records=records,
        pending_checkout_records=pending_checkout_records,
        filters=filters,
        sort_links=sort_links,
        vehicle_types=get_vehicle_types(),
        status_options=STATUS_OPTIONS,
        format_duration=format_duration,
        today_filter_value=localize_datetime(utc_now()).date().isoformat(),
    )


@main_bp.route("/tickets-board")
@login_required
def tickets_board_page():
    board_records, board_metrics = build_ticket_board()
    enrich_records([slot["record"] for slot in board_records if slot["record"]])
    return render_template(
        "tickets_board.html",
        records=board_records,
        board_metrics=board_metrics,
        vehicle_types=get_vehicle_types(include_inactive=True),
        format_duration=format_duration,
    )


@main_bp.route("/operations")
@login_required
def operations_page():
    now = utc_now()
    vehicle_types = get_vehicle_types()
    return render_template(
        "operations.html",
        vehicle_types=vehicle_types,
        record_stay_modes=RECORD_STAY_MODES,
        weekly_prices={
            vehicle_type: calculate_charge(vehicle_type, now, now, "weekly", 6)["total"]
            for vehicle_type in vehicle_types
        },
        weekly_prepayments=WeeklyPrepayment.query.options(joinedload(WeeklyPrepayment.movement))
        .filter_by(activated_at=None).order_by(WeeklyPrepayment.id.desc()).all(),
    )


@main_bp.route("/weekly-prepayments/new", methods=["POST"])
@login_required
def create_weekly_prepayment_route():
    try:
        prepayment = create_weekly_prepayment(request.form, current_user)
        flash(f"Anticipo registrado por {money_filter(prepayment.movement.amount)}.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.operations_page", _anchor="weekly-prepayments"))


@main_bp.route("/weekly-prepayments/<int:prepayment_id>/activate", methods=["POST"])
@login_required
def activate_weekly_prepayment_route(prepayment_id):
    prepayment = db.get_or_404(WeeklyPrepayment, prepayment_id)
    try:
        record = activate_weekly_prepayment(prepayment, request.form.get("ticket_number"), current_user)
        flash("Entrada registrada. La vigencia semanal comienza ahora.", "success")
        return redirect(url_for("main.print_ticket", record_id=record.id))
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("main.operations_page", _anchor="weekly-prepayments"))


@main_bp.route("/tariffs")
@login_required
@role_required("admin")
def tariffs_page():
    return render_template(
        "tariffs.html",
        tariffs=get_tariffs(),
        billing_schemes=BILLING_SCHEMES,
        period_units=PERIOD_UNITS,
    )


@main_bp.route("/employees")
@login_required
@role_required("admin")
def employees_page():
    users = User.query.order_by(User.full_name.asc()).all()
    schedule_rows = {user.id: employee_schedule_rows(user) for user in users}
    return render_template("employees.html", users=users, schedule_rows=schedule_rows)


@main_bp.route("/cuts")
@login_required
def cuts_page():
    cuts = CashCut.query.order_by(CashCut.generated_at.desc()).limit(20).all()
    users = User.query.order_by(User.full_name.asc()).all()
    today = localize_datetime(utc_now()).date()
    return render_template(
        "cuts.html",
        cuts=cuts,
        users=users,
        today=today,
        current_month=today.strftime("%Y-%m"),
        cash_cut_type_label=cash_cut_type_label,
    )


@main_bp.route("/help")
@login_required
def help_page():
    return render_template("help.html")


@main_bp.route("/records/new", methods=["POST"])
@login_required
def create_record():
    try:
        record = create_vehicle_record(request.form, current_user)
        flash(
            "Entrada y pago semanal registrados correctamente."
            if record.is_paid else "Entrada registrada correctamente.",
            "success",
        )
        return redirect(url_for("main.print_ticket", record_id=record.id))
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.operations_page"))


@main_bp.route("/records/<int:record_id>/ticket")
@login_required
def print_ticket(record_id):
    record = vehicle_record_display_query().filter_by(id=record_id).first()
    if not record:
        flash("No se encontró el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))
    qr_target = url_for("main.print_ticket", record_id=record.id, _external=True)
    weekly_ends_at = (
        ensure_utc(record.entry_at) + timedelta(days=record.contracted_days)
        if not record.is_hourly and record.contracted_days else None
    )
    return render_template(
        "ticket.html", record=record, qr_target=qr_target, weekly_ends_at=weekly_ends_at
    )

@main_bp.route("/records/<int:record_id>/ticket/qr.svg")
@login_required
def ticket_qr_document(record_id):
    record = vehicle_record_display_query().filter_by(id=record_id).first()
    if not record:
        flash("No se encontrÃ³ el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))

    qr_target = url_for("main.print_ticket", record_id=record.id, _external=True)
    qr_svg = build_ticket_qr_svg(qr_target)
    return Response(qr_svg, mimetype="image/svg+xml")


@main_bp.route("/records/<int:record_id>/ticket/document")
@login_required
def ticket_document(record_id):
    record = vehicle_record_display_query().filter_by(id=record_id).first()
    if not record:
        flash("No se encontró el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))

    qr_target = url_for("main.print_ticket", record_id=record.id, _external=True)
    pdf_bytes = build_ticket_pdf(record, format_datetime_for_ticket, qr_value=qr_target)
    filename = f"ticket_{record.display_ticket_number}.pdf"
    as_attachment = request.args.get("download") == "1"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=as_attachment,
        download_name=filename,
    )


@main_bp.route("/reports/current-cut/document")
@login_required
def current_cut_document():
    active_records, paid_today_records = current_cut_records()
    enrich_records(active_records)
    enrich_records(paid_today_records)

    generated_at = utc_now()
    pdf_bytes = build_current_cut_pdf(
        active_records,
        paid_today_records,
        current_user,
        generated_at,
        format_datetime_for_ticket,
    )
    filename = f"corte_actual_{localize_datetime(generated_at).strftime('%Y%m%d_%H%M')}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=request.args.get("download") == "1",
        download_name=filename,
    )


@main_bp.route("/cash-movements/new", methods=["POST"])
@login_required
def create_cash_movement_route():
    try:
        movement = create_cash_movement(request.form, current_user)
        action_label = "gasto" if movement.movement_type == "expense" else "entrada de dinero"
        flash(f"Se registro el {action_label} correctamente.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(request.referrer or url_for("main.dashboard"))


@main_bp.route("/cash-movements/<int:movement_id>/delete", methods=["POST"])
@login_required
def delete_cash_movement_route(movement_id):
    if not can_delete_cash_movements():
        abort(403)

    movement = db.session.get(CashMovement, movement_id)
    if not movement:
        flash("No se encontro el movimiento de caja solicitado.", "danger")
        return redirect(request.referrer or url_for("main.dashboard"))

    try:
        delete_cash_movement(movement, current_user)
        flash("Movimiento de caja eliminado correctamente.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(request.referrer or url_for("main.dashboard"))


@main_bp.route("/records/<int:record_id>/exit", methods=["POST"])
@login_required
def register_vehicle_exit(record_id):
    record = db.session.get(VehicleRecord, record_id)
    if not record:
        flash("No se encontró el registro solicitado.", "danger")
        return redirect(url_for("main.dashboard"))

    try:
        register_exit(record, current_user)
        flash("Salida registrada y cobro calculado automáticamente.", "success")
    except ValueError as exc:
        flash(str(exc), "warning")
    return redirect(url_for("main.records_page"))


@main_bp.route("/records/<int:record_id>/weekly-exit", methods=["POST"])
@login_required
def register_vehicle_weekly_exit(record_id):
    record = db.session.get(VehicleRecord, record_id)
    if not record:
        flash("No se encontrÃ³ el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))

    try:
        register_weekly_exit(record, current_user)
        flash("Salida semanal registrada correctamente.", "success")
    except ValueError as exc:
        flash(str(exc), "warning")
    return redirect(url_for("main.records_page"))


@main_bp.route("/records/<int:record_id>/weekly-entry", methods=["POST"])
@login_required
def register_vehicle_weekly_entry(record_id):
    record = db.session.get(VehicleRecord, record_id)
    if not record:
        flash("No se encontró el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))

    try:
        register_weekly_entry(record, current_user)
        flash("Entrada del día registrada correctamente.", "success")
    except ValueError as exc:
        flash(str(exc), "warning")
    return redirect(url_for("main.records_page"))


@main_bp.route("/records/<int:record_id>/pay", methods=["POST"])
@login_required
def mark_paid(record_id):
    record = db.session.get(VehicleRecord, record_id)
    if not record:
        flash("No se encontró el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))

    try:
        register_payment(record, current_user)
        flash("Pago registrado correctamente.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.records_page"))


@main_bp.route("/records/<int:record_id>/uncertain", methods=["POST"])
@login_required
def mark_uncertain(record_id):
    record = db.session.get(VehicleRecord, record_id)
    if not record:
        flash("No se encontro el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))
    if record.exit_at:
        flash("No puedes marcar como indeciso un registro con salida final.", "warning")
        return redirect(request.referrer or url_for("main.records_page"))

    record.status = "Indeciso"
    log_action(current_user, "vehicle_record_marked_uncertain", "vehicle_record", record.id)
    db.session.commit()
    flash("Ficha marcada como indecisa para revision.", "success")
    return redirect(request.referrer or url_for("main.records_page"))


@main_bp.route("/records/<int:record_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin")
def edit_record(record_id):
    record = db.session.get(VehicleRecord, record_id)
    if not record:
        flash("No se encontró el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))

    if request.method == "POST":
        try:
            update_vehicle_record(
                record,
                request.form,
                current_user,
                allow_entry_at_change=can_edit_vehicle_entry_dates(),
            )
            flash("Registro actualizado correctamente.", "success")
            return redirect(url_for("main.records_page"))
        except ValueError as exc:
            flash(str(exc), "danger")

    return render_template(
        "record_edit.html",
        record=record,
        vehicle_types=get_vehicle_types(include_inactive=True),
        record_stay_modes=RECORD_STAY_MODES,
        status_options=STATUS_OPTIONS,
    )


@main_bp.route("/records/<int:record_id>/update", methods=["POST"])
@login_required
def update_record_from_modal(record_id):
    record = db.session.get(VehicleRecord, record_id)
    if not record:
        flash("No se encontrÃ³ el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))

    try:
        update_vehicle_record(
            record,
            request.form,
            current_user,
            allow_entry_at_change=can_edit_vehicle_entry_dates(),
        )
        flash("Registro actualizado correctamente.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.records_page"))


@main_bp.route("/tariffs/new", methods=["POST"])
@login_required
@role_required("admin")
def create_tariff_route():
    try:
        create_tariff(request.form, current_user)
        flash("Tarifa creada correctamente.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.tariffs_page"))


@main_bp.route("/tariffs/<int:tariff_id>/update", methods=["POST"])
@login_required
@role_required("admin")
def update_tariff_route(tariff_id):
    tariff = db.session.get(Tariff, tariff_id)
    if not tariff:
        flash("No se encontró la tarifa solicitada.", "danger")
        return redirect(url_for("main.tariffs_page"))

    try:
        update_tariff(tariff, request.form, current_user)
        flash("Tarifa actualizada correctamente.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.tariffs_page"))


@main_bp.route("/records/<int:record_id>/delete", methods=["POST"])
@login_required
def delete_record(record_id):
    if not can_delete_vehicle_records():
        abort(403)

    record = db.session.get(VehicleRecord, record_id)
    if not record:
        flash("No se encontró el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))

    delete_vehicle_record(record, current_user)
    flash("Registro eliminado correctamente.", "success")
    return redirect(url_for("main.records_page"))


@main_bp.route("/users/new", methods=["POST"])
@login_required
@role_required("admin")
def create_user():
    try:
        employee = create_employee(request.form, current_user)
        flash(
            f"Empleado creado correctamente: {employee.full_name} ({employee.username}).",
            "success",
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Employee creation failed")
        flash(f"No fue posible crear el empleado. Detalle: {exc}", "danger")
    return redirect(url_for("main.employees_page"))


@main_bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@role_required("admin")
def toggle_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("No se encontró el usuario solicitado.", "danger")
        return redirect(url_for("main.cuts_page"))
    if user.id == current_user.id:
        flash("No puedes desactivar tu propio usuario desde esta pantalla.", "danger")
        return redirect(url_for("main.employees_page"))

    user.is_active_user = not user.is_active_user
    log_action(
        current_user,
        "employee_status_changed",
        "user",
        user.id,
        {"active": user.is_active_user},
    )
    db.session.commit()
    flash("Estado del empleado actualizado.", "success")
    return redirect(url_for("main.employees_page"))


@main_bp.route("/users/<int:user_id>/password", methods=["POST"])
@login_required
@role_required("admin")
def reset_password(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("No se encontró el usuario solicitado.", "danger")
        return redirect(url_for("main.employees_page"))

    try:
        reset_employee_password(user, request.form.get("new_password", ""), current_user)
        flash("Contraseña actualizada correctamente.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.employees_page"))


@main_bp.route("/users/<int:user_id>/edit", methods=["POST"])
@login_required
@role_required("admin")
def edit_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("No se encontró el usuario solicitado.", "danger")
        return redirect(url_for("main.employees_page"))

    try:
        update_employee(user, request.form, current_user)
        flash(f"Empleado actualizado correctamente: {user.full_name} ({user.username}).", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.employees_page"))


@main_bp.route("/users/<int:user_id>/schedule", methods=["POST"])
@login_required
@role_required("admin")
def edit_user_schedule(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("No se encontrÃ³ el usuario solicitado.", "danger")
        return redirect(url_for("main.employees_page"))

    try:
        update_employee_schedule(user, request.form, current_user)
        flash(f"Horario actualizado correctamente para {user.full_name}.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.employees_page"))


@main_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("No se encontró el usuario solicitado.", "danger")
        return redirect(url_for("main.employees_page"))

    try:
        delete_employee(user, current_user)
        flash("Empleado eliminado correctamente.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.employees_page"))


@main_bp.route("/cuts/generate", methods=["POST"])
@login_required
def generate_cut():
    try:
        cut_type = clean_text(request.form.get("cut_type"), 20, "tipo de corte")
        if cut_type not in {"daily", "weekly", "monthly", "custom"}:
            raise ValueError("Tipo de corte inválido.")
        target_user = None
        target_user_id = (request.form.get("target_user_id") or "").strip()
        if target_user_id:
            if not target_user_id.isdigit():
                raise ValueError("Selecciona un usuario valido para el corte.")
            target_user = db.session.get(User, int(target_user_id))
            if not target_user:
                raise ValueError("Selecciona un usuario valido para el corte.")
        target_date = None
        period_start = None
        period_end = None
        if cut_type == "daily":
            target_date = parse_cut_date(request.form.get("cut_date"))
        elif cut_type == "weekly":
            if request.form.get("week_start_date") or request.form.get("week_end_date"):
                week_start = require_cut_date(request.form.get("week_start_date"), "la fecha de inicio")
                week_end = require_cut_date(request.form.get("week_end_date"), "la fecha de fin")
                period_start, period_end = get_local_date_range_bounds(week_start, week_end)
        elif cut_type == "monthly":
            target_date = require_cut_month(request.form.get("month_date"))
        elif cut_type == "custom":
            custom_start = require_cut_date(request.form.get("custom_start_date"), "la fecha de inicio")
            custom_end = require_cut_date(request.form.get("custom_end_date"), "la fecha de fin")
            period_start, period_end = get_local_date_range_bounds(custom_start, custom_end)
        cut = generate_cash_cut(
            cut_type,
            current_user,
            target_date=target_date,
            period_start=period_start,
            period_end=period_end,
            target_user=target_user,
        )
        flash("Corte generado correctamente.", "success")
        return redirect(url_for("main.cash_cut_detail", cut_id=cut.id))
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("main.cuts_page"))


@main_bp.route("/cuts/<int:cut_id>")
@login_required
def cash_cut_detail(cut_id):
    cut = db.session.get(CashCut, cut_id)
    if not cut:
        flash("No se encontró el corte solicitado.", "danger")
        return redirect(url_for("main.cuts_page"))
    paid_records = cut_records_for_period(cut)
    return render_template(
        "cut_detail.html",
        cut=cut,
        paid_records=paid_records,
        cash_cut_type_label=cash_cut_type_label,
        format_duration=format_duration,
    )


@main_bp.route("/cuts/<int:cut_id>/document")
@login_required
def cash_cut_document(cut_id):
    cut = db.session.get(CashCut, cut_id)
    if not cut:
        flash("No se encontrÃ³ el corte solicitado.", "danger")
        return redirect(url_for("main.cuts_page"))

    paid_records = cut_records_for_period(cut)
    pdf_bytes = build_cash_cut_pdf(
        cut,
        paid_records,
        format_datetime_for_ticket,
    )
    filename = f"{cash_cut_file_basename(cut)}.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,
    )


@main_bp.route("/cuts/<int:cut_id>/ticket")
@login_required
def cash_cut_ticket_document(cut_id):
    cut = db.session.get(CashCut, cut_id)
    if not cut:
        flash("No se encontro el corte solicitado.", "danger")
        return redirect(url_for("main.cuts_page"))

    paid_records = cut_records_for_period(cut)
    pdf_bytes = build_cash_cut_ticket_pdf(
        cut,
        paid_records,
        format_datetime_for_ticket,
    )
    filename = f"{cash_cut_file_basename(cut)} ticket.pdf"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,
    )


@main_bp.route("/cuts/<int:cut_id>/export")
@login_required
def export_cut(cut_id):
    cut = db.session.get(CashCut, cut_id)
    if not cut:
        flash("No se encontró el corte solicitado.", "danger")

        return redirect(url_for("main.cuts_page"))

    lines = [
        "tipo_corte,usuario_corte,periodo_inicio,periodo_fin,total_caja,vehiculos_cobrados,entradas_extra,gastos",
        f"{cut.cut_type},{cut.target_user.full_name if cut.target_user else 'Todos'},{cut.period_start.isoformat()},{cut.period_end.isoformat()},{cut.total_income},{cut.vehicles_paid},{cut.extra_income},{cut.total_expenses}",
        "",
        "tipo_vehiculo,cantidad,ingresos",
    ]
    for vehicle_type, values in cut.vehicle_breakdown.items():
        lines.append(f"{vehicle_type},{values['count']},{values['income']}")
    lines.extend(["", "tipo,descripcion,cantidad,usuario"])
    for movement in cut.movement_summary.get("items", []):
        lines.append(
            f"{movement['label']},{movement['description']},{movement['amount']},{movement['created_by']}"
        )

    response = make_response("\n".join(lines))
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    filename = f"{cash_cut_file_basename(cut)}.csv"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@main_bp.errorhandler(403)
def forbidden(_error):
    return render_template("403.html"), 403
