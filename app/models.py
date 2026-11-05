import json
import math
import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

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
    password_reference = db.Column(db.String(255))
    is_active_user = db.Column(db.Boolean, default=True, nullable=False)
    last_login_at = db.Column(db.DateTime(timezone=True))
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime(timezone=True))
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)

    role = db.relationship("Role")
    schedules = db.relationship(
        "EmployeeSchedule",
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="EmployeeSchedule.weekday",
    )

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

    @property
    def has_admin_access(self):
        return self.is_admin or self.username == "empleado02"

    def is_locked(self):
        return bool(self.locked_until and utc_now() < self.locked_until)

    def register_failed_login(self):
        self.failed_login_attempts += 1
        if self.failed_login_attempts >= 5:
            self.locked_until = utc_now() + timedelta(minutes=15)

    def reset_login_guard(self):
        self.failed_login_attempts = 0
        self.locked_until = None


class EmployeeSchedule(TimestampMixin, db.Model):
    __tablename__ = "employee_schedules"
    __table_args__ = (
        db.UniqueConstraint("user_id", "weekday", name="uq_employee_schedule_user_weekday"),
        db.Index("ix_employee_schedules_user_weekday", "user_id", "weekday"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    weekday = db.Column(db.Integer, nullable=False)
    start_time = db.Column(db.Time, nullable=False, default=time(0, 0))
    end_time = db.Column(db.Time, nullable=False, default=time(23, 59))
    is_enabled = db.Column(db.Boolean, default=False, nullable=False)

    user = db.relationship("User", back_populates="schedules")


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
    __table_args__ = (
        db.Index("ix_vehicle_records_status", "status"),
        db.Index("ix_vehicle_records_entry_at", "entry_at"),
        db.Index("ix_vehicle_records_exit_at", "exit_at"),
        db.Index("ix_vehicle_records_paid_at", "paid_at"),
        db.Index("ix_vehicle_records_physical_ticket_number", "physical_ticket_number"),
        db.Index("ix_vehicle_records_status_entry_at", "status", "entry_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(30), unique=True, nullable=False)
    physical_ticket_number = db.Column(db.String(30))
    client_name = db.Column(db.String(120), nullable=False)
    vehicle_type = db.Column(db.String(50), nullable=False)
    plate_number = db.Column(db.String(50), nullable=False)
    stay_mode = db.Column(db.String(20), default="hourly", nullable=False)
    contracted_days = db.Column(db.Integer)
    service_wash = db.Column(db.Boolean, default=False, nullable=False)
    service_oil_change = db.Column(db.Boolean, default=False, nullable=False)
    service_oil_price = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    entry_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    exit_at = db.Column(db.DateTime(timezone=True))
    duration_seconds = db.Column(db.Integer, default=0, nullable=False)
    applied_rate_label = db.Column(db.String(255))
    total_amount = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    status = db.Column(db.String(30), default="Dentro del estacionamiento", nullable=False)
    paid_at = db.Column(db.DateTime(timezone=True))
    notes = db.Column(db.Text)
    entry_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    exit_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    payment_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    weekly_prepayment_id = db.Column(db.Integer, db.ForeignKey("weekly_prepayments.id"), unique=True)

    entry_user = db.relationship("User", foreign_keys=[entry_user_id])
    exit_user = db.relationship("User", foreign_keys=[exit_user_id])
    payment_user = db.relationship("User", foreign_keys=[payment_user_id])
    weekly_exit_logs = db.relationship(
        "WeeklyExitLog",
        back_populates="record",
        cascade="all, delete-orphan",
        order_by="WeeklyExitLog.exited_at.desc()",
    )

    def close_record(self, user):
        if not self.is_paid:
            raise ValueError("Primero registra el pago para permitir la salida.")
        closed_at = utc_now()
        pricing = calculate_charge(
            self.vehicle_type,
            self.entry_at,
            closed_at,
            stay_mode=self.stay_mode,
            contracted_days=self.contracted_days,
        )
        self.exit_at = closed_at
        self.exit_user = user
        self.duration_seconds = pricing["duration_seconds"]
        if not self.total_amount:
            self.applied_rate_label = pricing["rate_label"]
            self.total_amount = pricing["total"]
        self.status = "Pagado"

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
        if self.exit_at:
            self.status = "Pagado"
        else:
            self.status = "Dentro del estacionamiento"

    @property
    def is_paid(self):
        return self.paid_at is not None or self.status == "Pagado"

    @property
    def payment_state_label(self):
        return "Pagado" if self.is_paid else "Pendiente"

    @property
    def payment_theme(self):
        return "paid" if self.is_paid else "checkout"

    @property
    def services_total_amount(self):
        oil_price = float(self.service_oil_price or 0)
        return (20.0 if self.service_wash else 0.0) + (40.0 if self.service_oil_change else 0.0) + oil_price

    @property
    def services_label(self):
        parts = []
        if self.service_wash:
            parts.append("Lavado de moto $20")
        if self.service_oil_change:
            parts.append(f"Cambio de aceite $40 + aceite ${float(self.service_oil_price or 0):,.2f}")
        return " | ".join(parts) if parts else "Sin servicios"

    @property
    def display_ticket_number(self):
        return self.physical_ticket_number or self.ticket_number

    @property
    def weekly_exit_count(self):
        return len(self.weekly_exit_logs)

    @property
    def weekly_entry_count(self):
        if self.is_hourly:
            return 0
        if self.weekly_exit_count == 0:
            return 0
        return self.weekly_exit_count if self.status == "Dentro del estacionamiento" else max(self.weekly_exit_count - 1, 0)

    @property
    def latest_weekly_exit_at(self):
        if not self.weekly_exit_logs:
            return None
        return self.weekly_exit_logs[0].exited_at

    @property
    def status_theme(self):
        if self.status == "Indeciso":
            return "uncertain"
        if self.status == "Pagado" and self.exit_at:
            return "paid"
        if self.status in {"Salida registrada", "Pendiente de pago"}:
            return "checkout"
        if self.is_paid:
            return "checkout"
        if not self.is_hourly and self.weekly_exit_count:
            return "weekly"
        return "inside"


db.Index(
    "ux_vehicle_records_active_physical_ticket",
    VehicleRecord.physical_ticket_number,
    unique=True,
    sqlite_where=db.text("exit_at IS NULL AND physical_ticket_number IS NOT NULL"),
    postgresql_where=db.text("exit_at IS NULL AND physical_ticket_number IS NOT NULL"),
).ddl_if(dialect=("sqlite", "postgresql"))


class WeeklyExitLog(TimestampMixin, db.Model):
    __tablename__ = "weekly_exit_logs"
    __table_args__ = (
        db.Index("ix_weekly_exit_logs_record_id_exited_at", "record_id", "exited_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    record_id = db.Column(db.Integer, db.ForeignKey("vehicle_records.id"), nullable=False)
    exited_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    day_number = db.Column(db.Integer, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    record = db.relationship("VehicleRecord", back_populates="weekly_exit_logs")
    created_by = db.relationship("User")


class CashCut(TimestampMixin, db.Model):
    __tablename__ = "cash_cuts"

    id = db.Column(db.Integer, primary_key=True)
    cut_type = db.Column(db.String(20), nullable=False)
    period_start = db.Column(db.DateTime(timezone=True), nullable=False)
    period_end = db.Column(db.DateTime(timezone=True), nullable=False)
    generated_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    generated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    target_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    total_income = db.Column(db.Numeric(10, 2), nullable=False)
    extra_income = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    total_expenses = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    net_total = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    total_pending = db.Column(db.Numeric(10, 2), nullable=False)
    vehicles_served = db.Column(db.Integer, nullable=False)
    vehicles_paid = db.Column(db.Integer, nullable=False)
    breakdown_json = db.Column(db.Text, nullable=False)

    generated_by = db.relationship("User", foreign_keys=[generated_by_user_id])
    target_user = db.relationship("User", foreign_keys=[target_user_id])

    @property
    def breakdown(self):
        return json.loads(self.breakdown_json)

    @property
    def vehicle_breakdown(self):
        return {
            key: value
            for key, value in self.breakdown.items()
            if not key.startswith("_")
        }

    @property
    def movement_summary(self):
        return self.breakdown.get(
            "_cash_movements",
            {
                "income": 0.0,
                "expense": 0.0,
                "items": [],
            },
        )


class CashMovement(TimestampMixin, db.Model):
    __tablename__ = "cash_movements"
    __table_args__ = (
        db.Index("ix_cash_movements_occurred_at", "occurred_at"),
        db.Index("ix_cash_movements_type_occurred_at", "movement_type", "occurred_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    movement_type = db.Column(db.String(20), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    created_by = db.relationship("User")
    weekly_prepayment = db.relationship("WeeklyPrepayment", back_populates="movement", uselist=False)

    @property
    def signed_amount(self):
        amount = float(self.amount or 0)
        return -amount if self.movement_type == "expense" else amount

    @property
    def type_label(self):
        if self.weekly_prepayment:
            return "Anticipo semanal"
        return "Gasto" if self.movement_type == "expense" else "Entrada"


class WeeklyPrepayment(db.Model):
    __tablename__ = "weekly_prepayments"

    id = db.Column(db.Integer, primary_key=True)
    client_name = db.Column(db.String(120), nullable=False)
    plate_number = db.Column(db.String(50), nullable=False)
    vehicle_type = db.Column(db.String(50), nullable=False)
    rate_label = db.Column(db.String(255), nullable=False)
    movement_id = db.Column(db.Integer, db.ForeignKey("cash_movements.id"), nullable=False, unique=True)
    activated_at = db.Column(db.DateTime(timezone=True))

    movement = db.relationship("CashMovement", back_populates="weekly_prepayment")


db.event.listen(
    WeeklyPrepayment.__table__,
    "after_create",
    db.DDL("ALTER TABLE weekly_prepayments ENABLE ROW LEVEL SECURITY").execute_if(dialect="postgresql"),
)
db.Index(
    "ux_weekly_prepayments_pending_plate",
    WeeklyPrepayment.plate_number,
    unique=True,
    sqlite_where=WeeklyPrepayment.activated_at.is_(None),
    postgresql_where=WeeklyPrepayment.activated_at.is_(None),
).ddl_if(dialect=("sqlite", "postgresql"))


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
                user.password_reference = password
                db.session.add(user)

    tariffs = [
        ("Moto", "daily", 10, "day", "Semana completa", 6, 40, "Se cobra $40 por semana completa de 6 dias."),
        ("Bicicleta", "daily", 10, "day", "Semana completa", 6, 40, "Se cobra $40 por semana completa de 6 dias."),
        (
            "Carrito callejero",
            "flat",
            10,
            "day",
            None,
            None,
            None,
            "Tarifa fija de $10 sin importar tiempo o dias.",
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

    normalize_weekly_six_day_pricing()

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
    if "password_reference" not in user_columns:
        connection.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN password_reference VARCHAR(255) NULL"
        )
    if "locked_until" not in user_columns:
        connection.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN locked_until TIMESTAMP NULL"
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
        if "service_wash" not in record_columns:
            connection.exec_driver_sql(
                "ALTER TABLE vehicle_records ADD COLUMN service_wash BOOLEAN NOT NULL DEFAULT FALSE"
            )
        if "service_oil_change" not in record_columns:
            connection.exec_driver_sql(
                "ALTER TABLE vehicle_records ADD COLUMN service_oil_change BOOLEAN NOT NULL DEFAULT FALSE"
            )
        if "service_oil_price" not in record_columns:
            connection.exec_driver_sql(
                "ALTER TABLE vehicle_records ADD COLUMN service_oil_price NUMERIC(10, 2) NOT NULL DEFAULT 0"
            )
        if "paid_at" not in record_columns:
            connection.exec_driver_sql(
                "ALTER TABLE vehicle_records ADD COLUMN paid_at TIMESTAMP NULL"
            )
        if "payment_user_id" not in record_columns:
            connection.exec_driver_sql(
                "ALTER TABLE vehicle_records ADD COLUMN payment_user_id INTEGER NULL"
            )
    if "cash_cuts" in table_names:
        cut_columns = {column["name"] for column in inspector.get_columns("cash_cuts")}
        if "extra_income" not in cut_columns:
            connection.exec_driver_sql(
                "ALTER TABLE cash_cuts ADD COLUMN extra_income NUMERIC(10, 2) NOT NULL DEFAULT 0"
            )
        if "total_expenses" not in cut_columns:
            connection.exec_driver_sql(
                "ALTER TABLE cash_cuts ADD COLUMN total_expenses NUMERIC(10, 2) NOT NULL DEFAULT 0"
            )
        if "net_total" not in cut_columns:
            connection.exec_driver_sql(
                "ALTER TABLE cash_cuts ADD COLUMN net_total NUMERIC(10, 2) NOT NULL DEFAULT 0"
            )
            connection.exec_driver_sql(
                "UPDATE cash_cuts SET net_total = total_income WHERE net_total = 0"
            )
        if "target_user_id" not in cut_columns:
            connection.exec_driver_sql(
                "ALTER TABLE cash_cuts ADD COLUMN target_user_id INTEGER NULL"
            )
    if "cash_movements" not in table_names:
        CashMovement.__table__.create(bind=db.engine, checkfirst=True)
    if "weekly_exit_logs" not in table_names:
        WeeklyExitLog.__table__.create(bind=db.engine, checkfirst=True)
    if "employee_schedules" not in table_names:
        EmployeeSchedule.__table__.create(bind=db.engine, checkfirst=True)
    _ensure_runtime_indexes()
    if "vehicle_records" in table_names and (
        "weekly_prepayments" not in table_names or "weekly_prepayment_id" not in record_columns
    ):
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(62470118)")
        WeeklyPrepayment.__table__.create(bind=connection, checkfirst=True)
        current_columns = {column["name"] for column in inspect(connection).get_columns("vehicle_records")}
        if "weekly_prepayment_id" not in current_columns:
            connection.exec_driver_sql(
                "ALTER TABLE vehicle_records ADD COLUMN weekly_prepayment_id INTEGER REFERENCES weekly_prepayments(id)"
            )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_vehicle_records_weekly_prepayment ON vehicle_records (weekly_prepayment_id)"
        )
    normalize_weekly_six_day_pricing()
    db.session.commit()


def _ensure_runtime_indexes():
    for index in VehicleRecord.__table__.indexes:
        index.create(bind=db.engine, checkfirst=True)
    for index in WeeklyExitLog.__table__.indexes:
        index.create(bind=db.engine, checkfirst=True)
    for index in CashMovement.__table__.indexes:
        index.create(bind=db.engine, checkfirst=True)
    for index in EmployeeSchedule.__table__.indexes:
        index.create(bind=db.engine, checkfirst=True)


def normalize_weekly_six_day_pricing():
    legacy_vehicle_aliases = {
        "moto por semana": "Moto",
        "motocicleta por semana": "Moto",
        "bicicleta por semana": "Bicicleta",
        "carrito por semana": "Carrito callejero",
    }
    weekly_vehicle_types = {
        "Moto": (10, "Semana completa", 40),
        "Bicicleta": (10, "Semana completa", 40),
    }
    flat_vehicle_types = {
        "Carrito callejero": (10, "Tarifa fija de $10 sin importar tiempo o dias."),
    }

    for vehicle_type, (rate_amount, offer_label, offer_price) in weekly_vehicle_types.items():
        tariff = Tariff.query.filter_by(vehicle_type=vehicle_type).first()
        if not tariff:
            continue
        tariff.billing_scheme = "daily"
        tariff.period_unit = "day"
        tariff.rate_amount = rate_amount
        tariff.offer_label = offer_label
        tariff.offer_trigger_units = 6
        tariff.offer_price = offer_price
        tariff.notes = "Se cobra $40 por semana completa de 6 dias."

    for vehicle_type, (rate_amount, notes) in flat_vehicle_types.items():
        tariff = Tariff.query.filter_by(vehicle_type=vehicle_type).first()
        if not tariff:
            continue
        tariff.billing_scheme = "flat"
        tariff.period_unit = "day"
        tariff.rate_amount = rate_amount
        tariff.min_charge_units = 1
        tariff.offer_label = None
        tariff.offer_trigger_units = None
        tariff.offer_price = None
        tariff.notes = notes

    for legacy_name, canonical_name in legacy_vehicle_aliases.items():
        legacy_tariff = Tariff.query.filter(
            db.func.lower(Tariff.vehicle_type) == legacy_name
        ).first()
        if legacy_tariff:
            canonical_tariff = Tariff.query.filter_by(vehicle_type=canonical_name).first()
            if canonical_tariff:
                legacy_tariff.active = False

    weekly_records = VehicleRecord.query.filter_by(stay_mode="weekly", weekly_prepayment_id=None).all()
    for record in weekly_records:
        normalized_vehicle_type = legacy_vehicle_aliases.get((record.vehicle_type or "").strip().lower())
        if normalized_vehicle_type:
            record.vehicle_type = normalized_vehicle_type
        if record.vehicle_type not in weekly_vehicle_types and record.vehicle_type not in flat_vehicle_types:
            continue
        if record.vehicle_type in weekly_vehicle_types:
            record.contracted_days = 6
        pricing = calculate_charge(
            record.vehicle_type,
            record.entry_at,
            record.exit_at or record.paid_at or utc_now(),
            stay_mode=record.stay_mode,
            contracted_days=record.contracted_days,
        )
        record.duration_seconds = pricing["duration_seconds"]
        record.applied_rate_label = pricing["rate_label"]
        record.total_amount = pricing["total"] + record.service_oil_price + (20 if record.service_wash else 0) + (40 if record.service_oil_change else 0)
        if record.services_total_amount:
            record.applied_rate_label = f"{record.applied_rate_label} | {record.services_label}"


def generate_internal_ticket_code():
    return f"REC-{uuid.uuid4().hex[:12].upper()}"


def get_period_bounds(cut_type, reference_date=None):
    timezone_name = current_app.config.get("APP_TIMEZONE", "America/Mexico_City")
    try:
        local_timezone = ZoneInfo(timezone_name)
    except Exception:
        local_timezone = ZoneInfo("America/Mexico_City")

    now = utc_now().astimezone(local_timezone)
    if reference_date:
        now = datetime(
            reference_date.year,
            reference_date.month,
            reference_date.day,
            tzinfo=local_timezone,
        )
    if cut_type == "daily":
        local_start = datetime(now.year, now.month, now.day, tzinfo=local_timezone)
        start = local_start.astimezone(timezone.utc)
        end = start + timedelta(days=1)
        return start, end
    if cut_type == "monthly":
        local_start = datetime(now.year, now.month, 1, tzinfo=local_timezone)
        if now.month == 12:
            local_end = datetime(now.year + 1, 1, 1, tzinfo=local_timezone)
        else:
            local_end = datetime(now.year, now.month + 1, 1, tzinfo=local_timezone)
        return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)

    weekday = now.weekday()
    local_start = datetime(now.year, now.month, now.day, tzinfo=local_timezone) - timedelta(
        days=weekday
    )
    start = local_start.astimezone(timezone.utc)
    end = start + timedelta(days=7)
    return start, end
