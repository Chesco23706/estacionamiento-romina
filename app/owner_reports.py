from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from .extensions import db
from .models import CashCut, User, VehicleRecord
from .owner_models import OwnerShiftAssignment, OwnerShiftReport
from .pricing import ensure_utc
from .routes import get_local_day_bounds, get_local_timezone, localize_datetime
from .services import cash_cut_payments, cash_movements_for_period


@dataclass(frozen=True)
class Shift:
    key: str
    label: str
    window_start: time
    window_end: time

    @property
    def window(self):
        if self.key == "sunday":
            return "Jornada completa · cierre a las 23:59"
        return f"{self.window_start:%H:%M}–{self.window_end:%H:%M}"


SHIFTS = (
    Shift("morning", "Turno mañana", time(14), time(14, 30)),
    Shift("night", "Turno tarde", time(21, 30), time(23, 59)),
)
SUNDAY_SHIFT = Shift("sunday", "Jornada del domingo", time.min, time(23, 59))
ALL_SHIFTS = (*SHIFTS, SUNDAY_SHIFT)


def scheduled_shifts(day):
    return (SUNDAY_SHIFT,) if day.weekday() == 6 else SHIFTS


def shift_bounds(day, shift):
    day_start, day_end = get_local_day_bounds(day)
    if shift == SUNDAY_SHIFT:
        return day_start, day_end
    morning_end = ensure_utc(
        datetime.combine(day, SHIFTS[0].window_end, tzinfo=get_local_timezone())
        + timedelta(minutes=1)
    )
    return (day_start, morning_end) if shift.key == "morning" else (morning_end, day_end)


def assignment_history(at):
    history = defaultdict(list)
    for assignment in (
        OwnerShiftAssignment.query.options(joinedload(OwnerShiftAssignment.user))
        .filter(OwnerShiftAssignment.effective_at <= at)
        .order_by(OwnerShiftAssignment.effective_at.desc(), OwnerShiftAssignment.id.desc())
    ):
        history[assignment.shift].append(assignment)
    return history


def weekly_assignments(day, at, history):
    assignments = {
        shift.key: next((
            entry for entry in history[shift.key] if ensure_utc(entry.effective_at) <= at
        ), None)
        for shift in SHIFTS
    }
    morning, night = (assignments[shift.key] for shift in SHIFTS)
    if morning and morning.user and night and night.user and morning.user_id != night.user_id:
        anchor = localize_datetime(max(ensure_utc(morning.effective_at), ensure_utc(night.effective_at))).date()
        anchor_monday = anchor - timedelta(days=anchor.weekday())
        if ((day - anchor_monday).days // 7) % 2:
            assignments["morning"], assignments["night"] = night, morning
    return assignments


def shift_employee(day, shift, now, history):
    _, end = shift_bounds(day, shift)
    at = min(now, end - timedelta(microseconds=1))
    assignments = weekly_assignments(day, at, history)
    assignment = assignments["morning" if shift == SUNDAY_SHIFT else shift.key]
    return assignment.user if assignment else None


@dataclass
class Totals:
    vehicles: Decimal = Decimal("0.00")
    other: Decimal = Decimal("0.00")
    advances: Decimal = Decimal("0.00")
    expenses: Decimal = Decimal("0.00")
    count: int = 0

    @property
    def gross(self):
        return self.vehicles + self.other + self.advances

    @property
    def net(self):
        return self.gross - self.expenses

    def record_payment(self, amount):
        self.vehicles += amount
        self.count += 1

    def record_movement(self, movement):
        if movement.movement_type == "expense":
            self.expenses += movement.amount
        elif movement.weekly_prepayment:
            self.advances += movement.amount
        else:
            self.other += movement.amount

    def snapshot(self):
        return {
            "vehicles": str(self.vehicles),
            "other": str(self.other),
            "advances": str(self.advances),
            "expenses": str(self.expenses),
            "gross": str(self.gross),
            "net": str(self.net),
            "count": self.count,
        }


def weekly_ledger(monday, now):
    days = {monday + timedelta(days=index): Totals() for index in range(7)}
    employees = defaultdict(Totals)
    total = Totals()
    start, _ = get_local_day_bounds(monday)
    end, _ = get_local_day_bounds(monday + timedelta(days=7))
    end = min(end, now)

    payments = cash_cut_payments(start, end).with_entities(
        VehicleRecord.paid_at, VehicleRecord.total_amount, VehicleRecord.payment_user_id,
    )
    for paid_at, amount, user_id in payments:
        day = localize_datetime(paid_at).date()
        for bucket in (total, days[day], employees[user_id]):
            bucket.record_payment(amount)

    for movement in cash_movements_for_period(start, end):
        day = localize_datetime(movement.occurred_at).date()
        for bucket in (total, days[day], employees[movement.created_by_user_id]):
            bucket.record_movement(movement)

    names = dict(
        db.session.query(User.id, User.full_name).filter(User.id.in_(employees)).all()
    ) if employees else {}
    employee_rows = [
        (names.get(user_id, "Sin usuario registrado"), totals)
        for user_id, totals in employees.items()
    ]
    employee_rows.sort(key=lambda row: (-row[1].gross, row[0]))
    return total, days, employee_rows


def shift_snapshot(start, end, employee):
    totals = Totals()
    payments = []
    movements = []
    records = cash_cut_payments(start, end, employee.id).order_by(
        VehicleRecord.paid_at, VehicleRecord.id,
    )
    for record in records:
        totals.record_payment(record.total_amount)
        payments.append({
            "ticket": record.display_ticket_number,
            "type": record.vehicle_type,
            "time": localize_datetime(record.paid_at).strftime("%H:%M"),
            "amount": str(record.total_amount),
        })
    for movement in cash_movements_for_period(start, end, target_user=employee):
        totals.record_movement(movement)
        movements.append({
            "label": movement.type_label,
            "description": movement.description,
            "time": localize_datetime(movement.occurred_at).strftime("%H:%M"),
            "amount": str(movement.signed_amount),
        })
    return {**totals.snapshot(), "payments": payments, "movements": movements}


def manual_cuts(first_day, last_day):
    start, _ = get_local_day_bounds(first_day)
    _, end = get_local_day_bounds(last_day)
    cuts = (
        CashCut.query.options(joinedload(CashCut.target_user), joinedload(CashCut.generated_by))
        .filter(CashCut.cut_type == "daily", CashCut.period_start >= start, CashCut.period_start < end)
        .order_by(CashCut.generated_at.desc(), CashCut.id.desc())
    )
    results = defaultdict(list)
    for cut in cuts:
        day = localize_datetime(cut.period_start).date()
        created = localize_datetime(cut.generated_at)
        shift = next((
            shift.label for shift in scheduled_shifts(day)
            if created.date() == day
            and shift.window_start <= created.time().replace(second=0, microsecond=0) <= shift.window_end
        ), "Fuera de la ventana")
        results[day].append((cut, created, shift))
    return results


def close_due_shifts(now):
    today = localize_datetime(now).date()
    first_day = today - timedelta(days=7)
    existing = {
        (day, shift) for day, shift in db.session.query(
            OwnerShiftReport.business_date, OwnerShiftReport.shift,
        ).filter(OwnerShiftReport.business_date >= first_day)
    }
    history = assignment_history(now)
    count = 0
    for offset in range(8):
        day = first_day + timedelta(days=offset)
        # A Sunday with a saved two-shift report predates the full-day schedule.
        if day.weekday() == 6 and any((day, shift.key) in existing for shift in SHIFTS):
            continue
        for shift in scheduled_shifts(day):
            start, end = shift_bounds(day, shift)
            if end > now or (day, shift.key) in existing:
                continue
            employee = shift_employee(day, shift, now, history)
            if not employee:
                continue
            report = OwnerShiftReport(
                business_date=day, shift=shift.key, employee_name=employee.full_name,
                closed_at=end, snapshot=shift_snapshot(start, end, employee),
            )
            try:
                with db.session.begin_nested():
                    db.session.add(report)
                    db.session.flush()
            except IntegrityError:
                if not OwnerShiftReport.query.filter_by(business_date=day, shift=shift.key).first():
                    raise
            else:
                count += 1
    db.session.commit()
    return count
