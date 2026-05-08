from io import BytesIO

from flask import (
    Blueprint,
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
from sqlalchemy import or_

from .decorators import role_required
from .extensions import db
from .models import CashCut, Role, Tariff, User, VehicleRecord, log_action
from .pricing import calculate_charge, ensure_utc, format_duration, utc_now
from .services import (
    BILLING_SCHEMES,
    PERIOD_UNITS,
    RECORD_STAY_MODES,
    STATUS_OPTIONS,
    create_employee,
    create_tariff,
    create_vehicle_record,
    dashboard_metrics,
    delete_employee,
    delete_vehicle_record,
    generate_cash_cut,
    get_tariffs,
    get_vehicle_types,
    register_exit,
    register_payment,
    register_weekly_exit,
    reset_employee_password,
    update_employee,
    update_tariff,
    update_vehicle_record,
)
from .tickets import build_ticket_pdf
from .validators import clean_text

main_bp = Blueprint("main", __name__)


def parse_record_filters():
    requested_status = request.args.get("status")
    return {
        "search": request.args.get("search", "").strip(),
        "status": "Dentro del estacionamiento"
        if requested_status is None
        else requested_status.strip(),
        "vehicle_type": request.args.get("vehicle_type", "").strip(),
    }


def build_records_query(filters):
    query = VehicleRecord.query.order_by(VehicleRecord.entry_at.desc())
    search = filters["search"]
    if search:
        like_value = f"%{search}%"
        query = query.filter(
            or_(
                VehicleRecord.ticket_number.ilike(like_value),
                VehicleRecord.physical_ticket_number.ilike(like_value),
                VehicleRecord.client_name.ilike(like_value),
                VehicleRecord.plate_number.ilike(like_value),
                VehicleRecord.vehicle_type.ilike(like_value),
            )
        )
    if filters["status"]:
        query = query.filter(VehicleRecord.status == filters["status"])
    if filters["vehicle_type"]:
        query = query.filter(VehicleRecord.vehicle_type == filters["vehicle_type"])
    return query


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


def build_ticket_board():
    latest_by_ticket = {}
    for record in VehicleRecord.query.order_by(VehicleRecord.entry_at.desc()).all():
        slot_number = parse_ticket_slot_number(record.display_ticket_number)
        if slot_number and slot_number not in latest_by_ticket:
            latest_by_ticket[slot_number] = record

    board_records = []
    for slot_number in range(1, 101):
        record = latest_by_ticket.get(slot_number)
        board_records.append(
            {
                "slot_number": slot_number,
                "slot_label": f"{slot_number:03d}",
                "record": record,
                "status_theme": record.status_theme if record else "available",
            }
        )

    metrics = {
        "available": sum(1 for slot in board_records if slot["status_theme"] == "available"),
        "inside": sum(1 for slot in board_records if slot["status_theme"] == "inside"),
        "weekly": sum(1 for slot in board_records if slot["status_theme"] == "weekly"),
        "checkout": sum(1 for slot in board_records if slot["status_theme"] == "checkout"),
        "paid": sum(1 for slot in board_records if slot["status_theme"] == "paid"),
    }
    return board_records, metrics


def enrich_record_display(record, reference_time=None):
    target_time = reference_time or utc_now()
    if record.exit_at:
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
    return ensure_utc(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_datetime_for_ticket(value):
    if not value:
        return "-"
    return ensure_utc(value).astimezone().strftime("%d/%m/%Y %H:%M")


@main_bp.app_context_processor
def inject_now():
    return {"now_utc": utc_now}


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
    records = VehicleRecord.query.order_by(VehicleRecord.entry_at.desc()).limit(8).all()
    active_records = (
        VehicleRecord.query.filter(VehicleRecord.status == "Dentro del estacionamiento")
        .order_by(VehicleRecord.entry_at.desc())
        .limit(8)
        .all()
    )
    pending_checkout_records = (
        VehicleRecord.query.filter(VehicleRecord.status == "Salida registrada")
        .order_by(VehicleRecord.exit_at.desc())
        .limit(6)
        .all()
    )
    for record in records:
        enrich_record_display(record)
    for record in active_records:
        enrich_record_display(record)
    for record in pending_checkout_records:
        enrich_record_display(record)
    metrics = dashboard_metrics()
    return render_template(
        "dashboard.html",
        records=records,
        active_records=active_records,
        pending_checkout_records=pending_checkout_records,
        metrics=metrics,
        format_duration=format_duration,
        now_local=utc_now().astimezone(),
    )


@main_bp.route("/records")
@login_required
def records_page():
    filters = parse_record_filters()
    records = build_records_query(filters).all()
    pending_checkout_records = []
    if filters["status"] == "Dentro del estacionamiento":
        pending_checkout_records = (
            VehicleRecord.query.filter(VehicleRecord.status == "Salida registrada")
            .order_by(VehicleRecord.exit_at.desc())
            .limit(8)
            .all()
        )
    for record in records:
        enrich_record_display(record)
    for record in pending_checkout_records:
        enrich_record_display(record)
    return render_template(
        "records.html",
        records=records,
        pending_checkout_records=pending_checkout_records,
        filters=filters,
        vehicle_types=get_vehicle_types(),
        status_options=STATUS_OPTIONS,
        format_duration=format_duration,
    )


@main_bp.route("/tickets-board")
@login_required
def tickets_board_page():
    board_records, board_metrics = build_ticket_board()
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
    return render_template(
        "operations.html",
        vehicle_types=get_vehicle_types(),
        record_stay_modes=RECORD_STAY_MODES,
    )


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
    cuts = CashCut.query.order_by(CashCut.generated_at.desc()).limit(10).all()
    return render_template("employees.html", users=users, cuts=cuts)


@main_bp.route("/help")
@login_required
def help_page():
    return render_template("help.html")


@main_bp.route("/records/new", methods=["POST"])
@login_required
def create_record():
    try:
        record = create_vehicle_record(request.form, current_user)
        flash("Entrada registrada correctamente.", "success")
        return redirect(url_for("main.print_ticket", record_id=record.id))
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("main.operations_page"))


@main_bp.route("/records/<int:record_id>/ticket")
@login_required
def print_ticket(record_id):
    record = db.session.get(VehicleRecord, record_id)
    if not record:
        flash("No se encontró el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))
    return render_template("ticket.html", record=record)


@main_bp.route("/records/<int:record_id>/ticket/document")
@login_required
def ticket_document(record_id):
    record = db.session.get(VehicleRecord, record_id)
    if not record:
        flash("No se encontró el registro solicitado.", "danger")
        return redirect(url_for("main.records_page"))

    pdf_bytes = build_ticket_pdf(record, format_datetime_for_ticket)
    filename = f"ticket_{record.display_ticket_number}.pdf"
    as_attachment = request.args.get("download") == "1"
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=as_attachment,
        download_name=filename,
    )


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
            update_vehicle_record(record, request.form, current_user)
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
        update_vehicle_record(record, request.form, current_user)
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
@role_required("admin")
def delete_record(record_id):
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
        return redirect(url_for("main.employees_page"))
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
@role_required("admin")
def generate_cut():
    try:
        cut_type = clean_text(request.form.get("cut_type"), 20, "tipo de corte")
        if cut_type not in {"daily", "weekly"}:
            raise ValueError("Tipo de corte inválido.")
        cut = generate_cash_cut(cut_type, current_user)
        flash("Corte generado correctamente.", "success")
        return redirect(url_for("main.employees_page"))
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("main.employees_page"))


@main_bp.route("/cuts/<int:cut_id>")
@login_required
@role_required("admin")
def cash_cut_detail(cut_id):
    cut = db.session.get(CashCut, cut_id)
    if not cut:
        flash("No se encontró el corte solicitado.", "danger")
        return redirect(url_for("main.employees_page"))
    return render_template("cut_detail.html", cut=cut)


@main_bp.route("/cuts/<int:cut_id>/export")
@login_required
@role_required("admin")
def export_cut(cut_id):
    cut = db.session.get(CashCut, cut_id)
    if not cut:
        flash("No se encontró el corte solicitado.", "danger")
        return redirect(url_for("main.employees_page"))

    lines = [
        "tipo_corte,periodo_inicio,periodo_fin,total_ingresos,total_pendiente,vehiculos_atendidos,vehiculos_pagados",
        f"{cut.cut_type},{cut.period_start.isoformat()},{cut.period_end.isoformat()},{cut.total_income},{cut.total_pending},{cut.vehicles_served},{cut.vehicles_paid}",
        "",
        "tipo_vehiculo,cantidad,ingresos",
    ]
    for vehicle_type, values in cut.breakdown.items():
        lines.append(f"{vehicle_type},{values['count']},{values['income']}")

    response = make_response("\n".join(lines))
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename=cut_{cut.id}.csv"
    return response


@main_bp.errorhandler(403)
def forbidden(_error):
    return render_template("403.html"), 403
