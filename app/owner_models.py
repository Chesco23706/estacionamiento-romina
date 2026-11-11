from .extensions import db
from .pricing import utc_now


class OwnerShiftAssignment(db.Model):
    __tablename__ = "owner_shift_assignments"

    id = db.Column(db.Integer, primary_key=True)
    shift = db.Column(db.String(10), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    effective_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    user = db.relationship("User")

    __table_args__ = (db.Index("ix_owner_shift_effective", "shift", "effective_at"),)


class OwnerShiftReport(db.Model):
    __tablename__ = "owner_shift_reports"

    id = db.Column(db.Integer, primary_key=True)
    business_date = db.Column(db.Date, nullable=False)
    shift = db.Column(db.String(10), nullable=False)
    employee_name = db.Column(db.String(120), nullable=False)
    closed_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    snapshot = db.Column(db.JSON, nullable=False)

    __table_args__ = (db.UniqueConstraint("business_date", "shift", name="uq_owner_shift_date"),)


class OwnerPushDevice(db.Model):
    __tablename__ = "owner_push_devices"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    endpoint_hash = db.Column(db.String(64), unique=True, nullable=False)
    subscription = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class OwnerPushDelivery(db.Model):
    __tablename__ = "owner_push_deliveries"

    report_id = db.Column(db.Integer, db.ForeignKey("owner_shift_reports.id"), primary_key=True)
    device_id = db.Column(
        db.Integer, db.ForeignKey("owner_push_devices.id", ondelete="CASCADE"), primary_key=True, index=True,
    )
    claimed_at = db.Column(db.DateTime(timezone=True), nullable=False)
    sent_at = db.Column(db.DateTime(timezone=True))


OWNER_TABLES = (
    OwnerShiftAssignment.__table__, OwnerShiftReport.__table__,
    OwnerPushDevice.__table__, OwnerPushDelivery.__table__,
)

for table in OWNER_TABLES:
    db.event.listen(
        table, "after_create",
        db.DDL("ALTER TABLE %(fullname)s ENABLE ROW LEVEL SECURITY").execute_if(dialect="postgresql"),
    )
