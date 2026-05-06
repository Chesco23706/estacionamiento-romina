import json
import math
import uuid
from datetime import datetime, timedelta, timezone

from flask import current_app
from flask_login import UserMixin
from sqlalchemy import inspect
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager
from .pricing import calculate_charge, ensure_utc, utc_now


class TimestampMixin:
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Role(TimestampMixin, db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(30), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=False)


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active_user = db.Column(db.Boolean, default=True, nullable=False)
    last_login_at = db.Column(db.DateTime(timezone=True))
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime(timezone=True))
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)

    role = db.relationship("Role")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return self.is_active_user

    @property
    def is_admin(self):
        return self.role and self.role.name == "admin"

    def is_locked(self):
        return bool(self.locked_until and utc_now() < self.locked_until)

    def register_failed_login(self):
        self.failed_login_attempts += 1
        if self.failed_login_attempts >= 5:
            self.locked_until = utc_now() + timedelta(minutes=15)

    def reset_login_guard(self):
        self.failed_login_attempts = 0
        self.locked_until = None


class Tariff(TimestampMixin, db.Model):
    __tablename__ = "tariffs"

    id = db.Column(db.Integer, primary_key=True)
    vehicle_type = db.Column(db.String(50), unique=True, nullable=False)
    billing_scheme = db.Column(db.String(30), nullable=False)
    rate_amount = db.Column(db.Numeric(10, 2), nullable=False)
    period_unit = db.Column(db.String(20), nullable=False)
    min_charge_units = db.Column(db.Integer, default=1, nullable=False)
    offer_label = db.Column(db.String(120))
    offer_trigger_units = db.Column(db.Integer)
    offer_price = db.Column(db.Numeric(10, 2))
    notes = db.Column(db.String(255))
    active = db.Column(db.Boolean, default=True, nullable=False)


class VehicleRecord(TimestampMixin, db.Model):
    __tablename__ = "vehicle_records"

    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(30), unique=True, nullable=False)
    physical_ticket_number = db.Column(db.String(30))
    client_name = db.Column(db.String(120), nullable=False)
    vehicle_type = db.Column(db.String(50), nullable=False)
    plate_number = db.Column(db.String(50), nullable=False)
    stay_mode = db.Column(db.String(20), default="hourly", nullable=False)
    contracted_days = db.Column(db.Integer)
    entry_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    exit_at = db.Column(db.DateTime(timezone=True))
    duration_seconds = db.Column(db.Integer, default=0, nullable=False)
    applied_rate_label = db.Column(db.String(255))
    total_amount = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    status = db.Column(db.String(30), default="Dentro del estacionamiento", nullable=False)
    notes = db.Column(db.Text)
    entry_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    exit_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    entry_user = db.relationship("User", foreign_keys=[entry_user_id])
    exit_user = db.relationship("User", foreign_keys=[exit_user_id])

    def close_record(self, user):
        pricing = calculate_charge(
            self.vehicle_type,
            self.entry_at,
            utc_now(),
            stay_mode=self.stay_mode,
            contracted_days=self.contracted_days,
        )
        self.exit_at = utc_now()
        self.exit_user = user
        self.duration_seconds = pricing["duration_seconds"]
        self.applied_rate_label = pricing["rate_label"]
        self.total_amount = pricing["total"]
        self.status = "Pendiente de pago"

    @property
    def is_hourly(self):
        return self.stay_mode != "weekly"

    @property
    def mode_label(self):
        return "Por hora" if self.is_hourly else "Por semana"

    def consumed_day_units(self, reference_time=None):
        if self.is_hourly:
            return None
        target_time = self.exit_at or reference_time or utc_now()
        total_seconds = max(
            0,
            int((ensure_utc(target_time) - ensure_utc(self.entry_at)).total_seconds()),
        )
        return max(1, math.ceil(total_seconds / 86400)) if total_seconds or self.entry_at else 1

    def remaining_day_units(self, reference_time=None):
        if self.is_hourly or not self.contracted_days:
            return None
        consumed = self.consumed_day_units(reference_time=reference_time)
        return max(self.contracted_days - consumed, 0)

    def mark_paid(self):
        if self.status in {"Pendiente de pago", "Salida registrada"}:
            self.status = "Pagado"

    @property
    def display_ticket_number(self):
        return self.physical_ticket_number or self.ticket_number


class CashCut(TimestampMixin, db.Model):
    __tablename__ = "cash_cuts"

    id = db.Column(db.Integer, primary_key=True)
    cut_type = db.Column(db.String(20), nullable=False)
    period_start = db.Column(db.DateTime(timezone=True), nullable=False)
    period_end = db.Column(db.DateTime(timezone=True), nullable=False)
    generated_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    generated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    total_income = db.Column(db.Numeric(10, 2), nullable=False)
    total_pending = db.Column(db.Numeric(10, 2), nullable=False)
    vehicles_served = db.Column(db.Integer, nullable=False)
    vehicles_paid = db.Column(db.Integer, nullable=False)
    breakdown_json = db.Column(db.Text, nullable=False)

    generated_by = db.relationship("User")

    @property
    def breakdown(self):
        return json.loads(self.breakdown_json)


class AuditLog(TimestampMixin, db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(50), nullable=False)
    entity_type = db.Column(db.String(50), nullable=False)
    entity_id = db.Column(db.Integer)
    details_json = db.Column(db.Text, default="{}")

    user = db.relationship("User")


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def log_action(user, action, entity_type, entity_id=None, details=None):
    entry = AuditLog(
        user_id=getattr(user, "id", None),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details_json=json.dumps(details or {}, ensure_ascii=True),
    )
    db.session.add(entry)


def seed_defaults():
    if not Role.query.filter_by(name="admin").first():
        db.session.add(Role(name="admin", description="Administrador del sistema"))
    if not Role.query.filter_by(name="employee").first():
        db.session.add(Role(name="employee", description="Empleado de operación"))
    db.session.commit()

    admin_role = Role.query.filter_by(name="admin").first()
    employee_role = Role.query.filter_by(name="employee").first()

    bootstrap_admin_user()

    if current_app.config.get("SEED_DEMO_USERS", False):
        default_users = [
            ("Empleado 1", "empleado1", "EmpleadoUno2026!", employee_role),
            ("Empleado 2", "empleado2", "EmpleadoDos2026!", employee_role),
        ]
        for full_name, username, password, role in default_users:
            if not User.query.filter_by(username=username).first():
                user = User(full_name=full_name, username=username, role=role)
                user.set_password(password)
                db.session.add(user)

    tariffs = [
        ("Moto", "daily", 10, "day", "Semana completa", 7, 40, "Se cobra $40 por semana completa."),
        ("Bicicleta", "daily", 10, "day", "Semana completa", 7, 40, "Se cobra $40 por semana completa."),
        (
            "Carrito callejero",
            "daily",
            10,
            "day",
            "Semana completa",
            7,
            40,
            "Se cobra $40 por semana completa.",
        ),
        ("Automóvil", "hourly", 20, "hour", None, None, None, "Se cobra por hora o fracción."),
    ]
    for vehicle_type, scheme, amount, unit, offer_label, offer_trigger_units, offer_price, notes in tariffs:
        tariff = Tariff.query.filter_by(vehicle_type=vehicle_type).first()
        if not tariff:
            db.session.add(
                Tariff(
                    vehicle_type=vehicle_type,
                    billing_scheme=scheme,
                    rate_amount=amount,
                    period_unit=unit,
                    offer_label=offer_label,
                    offer_trigger_units=offer_trigger_units,
                    offer_price=offer_price,
                    notes=notes,
                )
            )
            continue

        if tariff.billing_scheme == "daily_or_weekly":
            tariff.billing_scheme = "daily"
        if not tariff.offer_label:
            tariff.offer_label = offer_label
        if not tariff.offer_trigger_units:
            tariff.offer_trigger_units = offer_trigger_units
        if not tariff.offer_price:
            tariff.offer_price = offer_price

    db.session.commit()


def bootstrap_admin_user():
    admin_role = Role.query.filter_by(name="admin").first()
    username = current_app.config.get("ADMIN_BOOTSTRAP_USERNAME", "admin")
    password = current_app.config.get("ADMIN_BOOTSTRAP_PASSWORD")
    full_name = current_app.config.get("ADMIN_BOOTSTRAP_NAME", "Administrador General")

    if not password:
        return

    existing_admin = User.query.filter_by(username=username).first()
    if existing_admin:
        return

    user = User(full_name=full_name, username=username, role=admin_role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()


def apply_runtime_migrations():
    inspector = inspect(db.engine)
    table_names = inspector.get_table_names()
    if "users" not in table_names:
        return

    user_columns = {column["name"] for column in inspector.get_columns("users")}
    connection = db.session.connection()
    if "failed_login_attempts" not in user_columns:
        connection.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "locked_until" not in user_columns:
        connection.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN locked_until DATETIME NULL"
        )

    if "tariffs" in table_names:
        tariff_columns = {column["name"] for column in inspector.get_columns("tariffs")}
        if "offer_label" not in tariff_columns:
            connection.exec_driver_sql(
                "ALTER TABLE tariffs ADD COLUMN offer_label VARCHAR(120) NULL"
            )
        if "offer_trigger_units" not in tariff_columns:
            connection.exec_driver_sql(
                "ALTER TABLE tariffs ADD COLUMN offer_trigger_units INTEGER NULL"
            )
        if "offer_price" not in tariff_columns:
            connection.exec_driver_sql(
                "ALTER TABLE tariffs ADD COLUMN offer_price NUMERIC(10, 2) NULL"
            )
    if "vehicle_records" in table_names:
        record_columns = {
            column["name"] for column in inspector.get_columns("vehicle_records")
        }
        if "physical_ticket_number" not in record_columns:
            connection.exec_driver_sql(
                "ALTER TABLE vehicle_records ADD COLUMN physical_ticket_number VARCHAR(30) NULL"
            )
            connection.exec_driver_sql(
                "UPDATE vehicle_records SET physical_ticket_number = ticket_number WHERE physical_ticket_number IS NULL"
            )
        if "stay_mode" not in record_columns:
            connection.exec_driver_sql(
                "ALTER TABLE vehicle_records ADD COLUMN stay_mode VARCHAR(20) NOT NULL DEFAULT 'hourly'"
            )
        if "contracted_days" not in record_columns:
            connection.exec_driver_sql(
                "ALTER TABLE vehicle_records ADD COLUMN contracted_days INTEGER NULL"
            )
    db.session.commit()


def generate_internal_ticket_code():
    return f"REC-{uuid.uuid4().hex[:12].upper()}"


def get_period_bounds(cut_type):
    now = utc_now()
    if cut_type == "daily":
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        return start, end

    weekday = now.weekday()
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(
        days=weekday
    )
    end = start + timedelta(days=7)
    return start, end
