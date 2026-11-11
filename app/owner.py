from datetime import date, timedelta
from functools import wraps
from hmac import compare_digest
from uuid import uuid4

from flask import (
    Blueprint, abort, current_app, flash, make_response, redirect, render_template,
    request, send_file, url_for,
)
from flask_login import current_user
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from .extensions import db
from .models import CashCut, User
from .owner_models import OWNER_TABLES, OwnerPushDevice, OwnerShiftAssignment, OwnerShiftReport, OwnerWeeklyPay
from .owner_payroll import (
    MOVEMENT_LABELS, employee_key, linked_pay_movements, movement_cut_key, movement_cut_label,
    pay_employees, pay_movements, pay_totals, save_pay_change, week_cut_options,
)
from .owner_payroll_pdf import build_weekly_pay_pdf
from .owner_push import delete_device, device_key, push_configured, read_subscription, send_due_notifications, send_push
from .owner_reports import (
    ALL_SHIFTS, SHIFTS, assignment_history, close_due_shifts, manual_cuts, scheduled_shifts,
    shift_bounds, shift_employee, weekly_assignments, weekly_ledger,
)
from .pricing import utc_now
from .routes import localize_datetime, login, logout


owner_bp = Blueprint("owner", __name__)
owner_bp.add_url_rule("/owner/login", view_func=login, methods=["GET", "POST"])
owner_bp.add_url_rule("/owner/logout", view_func=logout, methods=["POST"])
WEEKDAYS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
DAY_INITIALS = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def register_owner_tables():
    with db.engine.begin() as connection:
        present = set(inspect(connection).get_table_names())
        if "users" not in present or all(table.name in present for table in OWNER_TABLES):
            return
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(62470119)")
        db.metadata.create_all(bind=connection, tables=OWNER_TABLES)


def owner_required(view):
    @wraps(view)
    def allowed(*args, **kwargs):
        if not current_user.is_authenticated:
            if request.endpoint not in {"owner.dashboard", "owner.settings", "owner.manual_cut", "owner.payments"}:
                abort(401)
            target = request.full_path.rstrip("?")
            if request.args.get("fragment") == "1":
                target = url_for("owner.dashboard", day=request.args.get("day"), week=request.args.get("week"))
            return redirect(url_for("owner.login", next=target))
        if not current_user.is_owner:
            abort(403)
        return view(*args, **kwargs)
    return allowed


@owner_bp.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@owner_bp.app_template_filter("owner_short_date")
def short_date(day):
    return f"{day.day} {MONTHS[day.month - 1]}"


def requested_day(raw, fallback, today):
    try:
        day = date.fromisoformat(raw) if raw else fallback
    except ValueError:
        day = fallback
    return min(max(day, date(2000, 1, 1)), today)


@owner_bp.get("/owner")
@owner_required
def dashboard():
    now = utc_now()
    local_now = localize_datetime(now)
    today = local_now.date()
    requested = requested_day(request.args.get("week"), today, today)
    day = requested_day(request.args.get("day"), requested, today)
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    total, days, employees = weekly_ledger(monday, now)
    maximum = max(abs(bucket.net) for bucket in days.values()) or 1
    chart = [
        {
            "date": key, "name": DAY_INITIALS[key.weekday()],
            "height": round(100 * abs(bucket.net) / maximum),
            "total": bucket.net,
        }
        for key, bucket in days.items()
    ]

    reports = {
        (report.business_date, report.shift): report for report in OwnerShiftReport.query.filter(
            OwnerShiftReport.business_date.between(monday, sunday),
        )
    }
    manual = manual_cuts(monday, sunday)
    pay_links = linked_pay_movements(day)
    history = assignment_history(now)
    day_shifts = scheduled_shifts(day)
    if day == sunday:
        day_shifts = tuple(shift for shift in ALL_SHIFTS if (day, shift.key) in reports) or day_shifts
    shifts = []
    for shift in day_shifts:
        _, end = shift_bounds(day, shift)
        shifts.append({
            "shift": shift, "report": reports.get((day, shift.key)), "due": end <= now,
            "employee": shift_employee(day, shift, now, history),
            "pay_movements": [movement for movement in pay_links if movement.cut_kind == shift.key],
        })
    weekly_cuts = [
        (
            key, WEEKDAYS[key.weekday()],
            [(shift, reports[(key, shift.key)]) for shift in ALL_SHIFTS if (key, shift.key) in reports],
            manual[key],
        )
        for key in reversed(days) if manual[key] or any((key, shift.key) in reports for shift in ALL_SHIFTS)
    ]
    context = dict(
        today=today, day=day, day_name=WEEKDAYS[day.weekday()], monday=monday, sunday=sunday,
        previous=monday - timedelta(days=1), following=min(sunday + timedelta(days=7), today),
        current_week=monday <= today <= sunday, local_now=local_now, total=total, chart=chart,
        day_total=days[day], shifts=shifts, manual=manual[day], employees=employees, weekly_cuts=weekly_cuts,
        open_shift=request.args.get("shift"),
        pay_movement_labels=MOVEMENT_LABELS, pay_employee_key=employee_key,
    )
    template = "owner_dashboard_content.html" if request.args.get("fragment") == "1" else "owner_dashboard.html"
    return render_template(template, **context)


@owner_bp.get("/owner/cuts/<int:cut_id>")
@owner_required
def manual_cut(cut_id):
    cut = db.get_or_404(CashCut, cut_id)
    if cut.cut_type != "daily":
        abort(404)
    day = localize_datetime(cut.period_start).date()
    return render_template(
        "owner_manual_cut.html", cut=cut, day=day, day_name=WEEKDAYS[day.weekday()],
        created=localize_datetime(cut.generated_at),
        cut_pay_movements=[movement for movement in linked_pay_movements(day) if movement.cash_cut_id == cut.id],
        pay_movement_labels=MOVEMENT_LABELS, pay_employee_key=employee_key,
    )


@owner_bp.route("/owner/payments", methods=["GET", "POST"])
@owner_required
def payments():
    now = utc_now()
    today = localize_datetime(now).date()
    week = request.args.get("week")
    day = requested_day(week, today, today)
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    employees = pay_employees(monday)
    requested_employee = request.args.get("employee")
    employee = employees.get(requested_employee)

    if request.method == "POST":
        if week != monday.isoformat():
            abort(400)
        if employee is None:
            abort(404)
        try:
            message = save_pay_change(employee, monday, request.form, current_user, now)
            db.session.commit()
        except ValueError as error:
            db.session.rollback()
            flash(str(error), "danger")
        else:
            flash(message, "success")
        return redirect(url_for("owner.payments", week=monday, employee=employee.key))

    if employee is None:
        employee = next(iter(employees.values()), None)
    pay = employee.pay if employee else None
    movements = pay_movements(pay)
    return render_template(
        "owner_payments.html", today=today, monday=monday, sunday=sunday,
        previous=monday - timedelta(days=7), following=min(monday + timedelta(days=7), today),
        current_week=monday <= today <= sunday, employees=employees.values(), employee=employee,
        movements=movements, totals=pay_totals(pay, movements), movement_labels=MOVEMENT_LABELS,
        cut_options=week_cut_options(monday) if employee else {},
        cut_key=movement_cut_key, cut_label=movement_cut_label, entry_key=uuid4().hex,
    )


@owner_bp.get("/owner/payments/<int:pay_id>/pdf")
@owner_required
def payments_pdf(pay_id):
    pay = db.get_or_404(OwnerWeeklyPay, pay_id)
    movements = pay_movements(pay)
    options = week_cut_options(pay.week_start)
    pdf = build_weekly_pay_pdf(pay, movements, options)
    name = secure_filename(pay.employee_name) or "empleado"
    return send_file(
        pdf, mimetype="application/pdf", as_attachment=True,
        download_name=f"pago-{pay.week_start.isoformat()}-{name}.pdf",
    )


@owner_bp.route("/owner/settings", methods=["GET", "POST"])
@owner_required
def settings():
    now = utc_now()
    today = localize_datetime(now).date()
    monday = today - timedelta(days=today.weekday())
    users = User.query.filter(User.is_active_user.is_(True)).order_by(User.full_name).all()
    assignments = weekly_assignments(today, now, assignment_history(now))
    if request.method == "POST":
        valid_ids = {str(user.id): user.id for user in users}
        selections = {shift.key: request.form.get(shift.key, "") for shift in SHIFTS}
        if any(value and value not in valid_ids for value in selections.values()):
            abort(400)
        if selections["morning"] and selections["morning"] == selections["night"]:
            flash("Elige a un empleado distinto para cada turno.", "danger")
            return redirect(url_for("owner.settings"))
        selected_ids = {key: valid_ids.get(value) for key, value in selections.items()}
        current_ids = {key: assignment.user_id if assignment else None for key, assignment in assignments.items()}
        if selected_ids != current_ids:
            for key, user_id in selected_ids.items():
                db.session.add(OwnerShiftAssignment(shift=key, user_id=user_id, effective_at=now))
        db.session.commit()
        flash("Turnos guardados. Se aplicarán a los próximos cierres.", "success")
        return redirect(url_for("owner.settings"))

    return render_template(
        "owner_settings.html", shifts=SHIFTS, users=users, assignments=assignments,
        monday=monday, sunday=monday + timedelta(days=6),
        push_ready=push_configured(), public_key=current_app.config["OWNER_PUSH_PUBLIC_KEY"],
        scheduler_ready=bool(current_app.config["CRON_SECRET"]),
    )


@owner_bp.route("/owner/device", methods=["PUT", "DELETE"])
@owner_required
def device():
    try:
        subscription = read_subscription(request.get_json(silent=True))
    except ValueError:
        return {"error": "La suscripción del teléfono no es válida."}, 400
    key = device_key(subscription)
    existing = OwnerPushDevice.query.filter_by(endpoint_hash=key, user_id=current_user.id).first()
    if request.method == "DELETE":
        if existing:
            delete_device(existing)
        return "", 204
    if not push_configured():
        return {"error": "Los avisos aún no están configurados en el servidor."}, 503

    if not existing:
        db.session.add(OwnerPushDevice(user_id=current_user.id, endpoint_hash=key, subscription=subscription))
    else:
        existing.subscription = subscription
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if not OwnerPushDevice.query.filter_by(endpoint_hash=key, user_id=current_user.id).first():
            return {"error": "Este teléfono ya está vinculado a otra cuenta."}, 409
    return "", 204


@owner_bp.post("/owner/device/test")
@owner_required
def test_device():
    if not push_configured():
        abort(503)
    try:
        subscription = read_subscription(request.get_json(silent=True))
    except ValueError:
        return {"error": "Activa los avisos en este teléfono primero."}, 400
    device = OwnerPushDevice.query.filter_by(
        endpoint_hash=device_key(subscription), user_id=current_user.id,
    ).first()
    if not device:
        return {"error": "Activa los avisos en este teléfono primero."}, 404
    result = send_push(device, {
        "title": "Avisos de Romina activos",
        "body": "Este teléfono puede recibir los avisos de tus cortes.",
        "url": url_for("owner.dashboard"),
        "tag": "romina-test",
    })
    if result != "sent":
        return {"error": "No se pudo enviar el aviso. Revisa el permiso e inténtalo de nuevo."}, 503
    return "", 204


@owner_bp.get("/owner/jobs/close-shifts")
def close_shifts():
    secret = current_app.config["CRON_SECRET"]
    actual = request.headers.get("Authorization", "")
    if not secret or not compare_digest(actual.encode(), f"Bearer {secret}".encode()):
        abort(401)
    now = utc_now()
    closed = close_due_shifts(now)
    sent, failed = send_due_notifications(now)
    return {"closed": closed, "sent": sent, "failed": failed}, 503 if failed else 200


@owner_bp.get("/owner/manifest.webmanifest")
def manifest():
    manifest = {
        "id": "/owner",
        "name": "Romina · Propietario",
        "short_name": "Romina",
        "lang": "es-MX",
        "start_url": url_for("owner.dashboard"),
        "scope": "/owner",
        "display": "standalone",
        "background_color": "#f3f5f8",
        "theme_color": "#13223a",
        "icons": [
            {"src": url_for("static", filename=f"owner-{size}.png"), "sizes": f"{size}x{size}", "type": "image/png"}
            for size in (192, 512)
        ],
    }
    response = make_response(manifest)
    response.mimetype = "application/manifest+json"
    return response
