import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, load_only

from .extensions import db
from .models import Role, User, log_action
from .owner_models import OwnerPayMovement, OwnerShiftReport, OwnerWeeklyPay
from .owner_reports import ALL_SHIFTS, manual_cuts, scheduled_shifts
from .validators import clean_text


MOVEMENT_LABELS = {
    "expense": "Gasto a descontar",
    "reimbursement": "Reembolso de gasto",
    "discount": "Descuento",
    "loan": "Préstamo a descontar",
}
SHIFT_LABELS = {shift.key: shift.label for shift in ALL_SHIFTS}
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class PayEmployee:
    key: str
    name: str
    user: User | None
    pay: OwnerWeeklyPay | None


@dataclass(frozen=True)
class PayCut:
    day: date
    kind: str
    label: str
    cash_cut_id: int | None = None
    closed: bool = True

    @property
    def key(self):
        if self.kind == "manual":
            return f"manual:{self.cash_cut_id}"
        return f"{self.day.isoformat()}:{self.kind}"


def employee_key(pay):
    return str(pay.employee_id) if pay.employee_id is not None else f"record-{pay.id}"


def pay_employees(monday):
    records = OwnerWeeklyPay.query.filter_by(week_start=monday).all()
    by_user = {pay.employee_id: pay for pay in records if pay.employee_id is not None}
    users = (
        User.query.join(Role, User.role_id == Role.id)
        .filter(Role.name == "employee", User.is_active_user.is_(True))
        .order_by(User.full_name, User.id)
        .all()
    )
    employees = {str(user.id): PayEmployee(str(user.id), user.full_name, user, by_user.get(user.id)) for user in users}
    for pay in records:
        key = employee_key(pay)
        if key not in employees:
            employees[key] = PayEmployee(key, pay.employee_name, None, pay)
    return employees


def week_cut_options(monday):
    sunday = monday + timedelta(days=6)
    reports = {
        (report.business_date, report.shift): report
        for report in OwnerShiftReport.query.options(load_only(
            OwnerShiftReport.business_date, OwnerShiftReport.shift, OwnerShiftReport.employee_name,
        )).filter(OwnerShiftReport.business_date.between(monday, sunday))
    }
    manual = manual_cuts(monday, sunday)
    options = {}
    for index in range(7):
        day = monday + timedelta(days=index)
        shifts = scheduled_shifts(day)
        if day == sunday:
            shifts = tuple(shift for shift in ALL_SHIFTS if (day, shift.key) in reports) or shifts
        for shift in shifts:
            report = reports.get((day, shift.key))
            label = f"{shift.label} · {report.employee_name}" if report else shift.label
            cut = PayCut(day, shift.key, label, closed=bool(report))
            options[cut.key] = cut
        for cash_cut, _, _ in manual[day]:
            name = cash_cut.target_user.full_name if cash_cut.target_user else "Caja general"
            cut = PayCut(day, "manual", f"Corte manual #{cash_cut.id} · {name}", cash_cut.id)
            options[cut.key] = cut
    return options


def movement_cut_key(movement):
    if movement.cash_cut_id:
        return f"manual:{movement.cash_cut_id}"
    if movement.cut_day:
        return f"{movement.cut_day.isoformat()}:{movement.cut_kind}"
    return ""


def movement_cut_label(movement, cut_options):
    cut = cut_options.get(movement_cut_key(movement))
    if cut:
        return f"{cut.day:%d/%m/%Y} · {cut.label}"
    if movement.cut_day:
        return f"{movement.cut_day:%d/%m/%Y} · {SHIFT_LABELS.get(movement.cut_kind, 'Corte archivado')}"
    return "Sin corte asignado"


def pay_movements(pay):
    if pay is None:
        return []
    return OwnerPayMovement.query.filter_by(weekly_pay_id=pay.id).order_by(
        OwnerPayMovement.created_at.desc(), OwnerPayMovement.id.desc(),
    ).all()


def linked_pay_movements(day):
    return (
        OwnerPayMovement.query.options(joinedload(OwnerPayMovement.weekly_pay))
        .filter_by(cut_day=day, voided_at=None)
        .order_by(OwnerPayMovement.id)
        .all()
    )


def pay_totals(pay, movements):
    amounts = dict.fromkeys(MOVEMENT_LABELS, ZERO)
    for movement in movements:
        if movement.voided_at is None:
            amounts[movement.kind] += movement.amount
    salary = pay.salary if pay else None
    deductions = amounts["expense"] + amounts["discount"] + amounts["loan"]
    net = salary + amounts["reimbursement"] - deductions if salary is not None else None
    return {"salary": salary, "amounts": amounts, "deductions": deductions, "net": net}


def pay_amount(raw, *, allow_zero=False):
    if not re.fullmatch(r"[0-9]{1,8}(?:[.,][0-9]{1,2})?", raw):
        raise ValueError("Ingresa un importe válido con hasta dos decimales.")
    amount = Decimal(raw.replace(",", "."))
    if amount == 0 and not allow_zero:
        raise ValueError("El importe del movimiento debe ser mayor que cero.")
    return amount.quantize(Decimal("0.01"))


def get_or_create_pay(employee, monday):
    if employee.pay:
        return employee.pay
    pay = OwnerWeeklyPay(week_start=monday, employee_id=employee.user.id, employee_name=employee.name)
    try:
        with db.session.begin_nested():
            db.session.add(pay)
            db.session.flush()
    except IntegrityError:
        pay = OwnerWeeklyPay.query.filter_by(week_start=monday, employee_id=employee.user.id).one_or_none()
        if pay is None:
            raise
    return pay


def save_pay_change(employee, monday, form, actor, now):
    action = form.get("action")
    cut_key = form.get("cut", "")
    cut = None
    if action in {"add", "assign"} and cut_key:
        cut = week_cut_options(monday).get(cut_key)
        if cut is None:
            raise ValueError("Selecciona un corte de la semana del pago.")

    if action == "salary":
        salary = pay_amount(form.get("salary", ""), allow_zero=True)
        pay = get_or_create_pay(employee, monday)
        if pay.salary != salary:
            log_action(actor, "weekly_pay_salary_saved", "owner_weekly_pay", pay.id,
                       {"previous": str(pay.salary) if pay.salary is not None else None, "salary": str(salary)})
            pay.salary = salary
        return "Sueldo semanal guardado."

    if action == "add":
        kind = form.get("kind")
        key = form.get("entry_key", "")
        if kind not in MOVEMENT_LABELS or not re.fullmatch(r"[a-f0-9]{32}", key):
            raise ValueError("Elige un tipo de movimiento válido y vuelve a intentarlo.")
        amount = pay_amount(form.get("amount", ""))
        description = clean_text(form.get("description"), 255, "concepto")
        pay = get_or_create_pay(employee, monday)
        movement = OwnerPayMovement(
            weekly_pay_id=pay.id, entry_key=key, kind=kind, amount=amount, description=description,
            created_at=now, cut_day=cut.day if cut else None, cut_kind=cut.kind if cut else None,
            cash_cut_id=cut.cash_cut_id if cut else None,
        )
        try:
            with db.session.begin_nested():
                db.session.add(movement)
                db.session.flush()
        except IntegrityError:
            if not OwnerPayMovement.query.filter_by(entry_key=key, weekly_pay_id=pay.id).first():
                raise
        else:
            log_action(actor, "weekly_pay_movement_added", "owner_pay_movement", movement.id)
        return "Movimiento guardado."

    if action not in {"assign", "void"} or employee.pay is None:
        raise ValueError("Elige un movimiento del pago seleccionado.")
    movement_id = form.get("movement_id", type=int)
    movement = OwnerPayMovement.query.filter_by(
        id=movement_id, weekly_pay_id=employee.pay.id,
    ).with_for_update().one_or_none()
    if movement is None:
        raise ValueError("El movimiento no corresponde a este pago.")
    if action == "void":
        if movement.voided_at is None:
            movement.voided_at = now
            log_action(actor, "weekly_pay_movement_voided", "owner_pay_movement", movement.id)
        return "Movimiento anulado. Se conserva en el historial."
    if movement.voided_at is not None:
        raise ValueError("El movimiento está anulado.")

    previous = movement_cut_key(movement)
    movement.cut_day = cut.day if cut else None
    movement.cut_kind = cut.kind if cut else None
    movement.cash_cut_id = cut.cash_cut_id if cut else None
    if previous != cut_key:
        log_action(actor, "weekly_pay_cut_assigned", "owner_pay_movement", movement.id,
                   {"previous": previous, "cut": cut_key})
    return "Corte actualizado."
