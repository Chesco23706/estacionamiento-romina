import math
from datetime import datetime, timezone


DAY_BASED_TYPES = {"Moto", "Bicicleta", "Carrito callejero"}
CAR_TYPE = "Automóvil"


def utc_now():
    return datetime.now(timezone.utc)


def ensure_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def seconds_between(start_at, end_at=None):
    normalized_start = ensure_utc(start_at)
    normalized_end = ensure_utc(end_at or utc_now())
    return max(0, int((normalized_end - normalized_start).total_seconds()))


def format_duration(total_seconds):
    hours, remainder = divmod(max(0, int(total_seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def calculate_charge(vehicle_type, entry_at, exit_at=None):
    total_seconds = seconds_between(entry_at, exit_at)

    if vehicle_type == CAR_TYPE:
        billable_hours = max(1, math.ceil(total_seconds / 3600))
        total = billable_hours * 20
        label = f"$20 por hora x {billable_hours} hora(s)"
        return {
            "duration_seconds": total_seconds,
            "duration_label": format_duration(total_seconds),
            "billing_units": billable_hours,
            "total": total,
            "rate_label": label,
        }

    billable_days = max(1, math.ceil(total_seconds / 86400))
    full_weeks, extra_days = divmod(billable_days, 7)
    total = (full_weeks * 40) + (extra_days * 10)

    parts = []
    if full_weeks:
        parts.append(f"$40 por semana x {full_weeks}")
    if extra_days:
        parts.append(f"$10 por día x {extra_days}")
    if not parts:
        parts.append("$10 mínimo por día")

    return {
        "duration_seconds": total_seconds,
        "duration_label": format_duration(total_seconds),
        "billing_units": billable_days,
        "total": total,
        "rate_label": " + ".join(parts),
    }
