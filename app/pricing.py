import math
from datetime import datetime, timezone


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


def calculate_charge(vehicle_type, entry_at, exit_at=None, stay_mode="hourly", contracted_days=None):
    from .models import Tariff

    tariff = Tariff.query.filter_by(vehicle_type=vehicle_type, active=True).first()
    if not tariff:
        tariff = Tariff.query.filter_by(vehicle_type=vehicle_type).first()
    if not tariff:
        raise ValueError("No existe una tarifa configurada para este tipo de vehiculo.")

    total_seconds = seconds_between(entry_at, exit_at)
    scheme = tariff.billing_scheme

    if scheme == "flat":
        return {
            "duration_seconds": total_seconds,
            "duration_label": format_duration(total_seconds),
            "billing_units": 1,
            "total": tariff.rate_amount,
            "rate_label": f"Tarifa fija ${float(tariff.rate_amount):,.2f}",
        }

    use_day_units = stay_mode == "weekly"
    divisor = 86400 if use_day_units else (3600 if tariff.period_unit == "hour" else 86400)
    unit_label = "dia" if use_day_units or tariff.period_unit == "day" else "hora"
    raw_units = math.ceil(total_seconds / divisor) if total_seconds else 0
    minimum_units = int(tariff.min_charge_units or 1)
    if use_day_units and contracted_days:
        minimum_units = max(minimum_units, int(contracted_days))
    billable_units = max(minimum_units, raw_units, 1)

    total = tariff.rate_amount * billable_units
    rate_parts = [
        f"${float(tariff.rate_amount):,.2f} por {unit_label} x {billable_units}"
    ]

    if tariff.offer_trigger_units and tariff.offer_price and billable_units >= tariff.offer_trigger_units:
        bundles, remainder = divmod(billable_units, tariff.offer_trigger_units)
        if bundles:
            total = (tariff.offer_price * bundles) + (tariff.rate_amount * remainder)
            bundle_label = tariff.offer_label or "Oferta"
            rate_parts = [
                f"{bundle_label}: ${float(tariff.offer_price):,.2f} x {bundles}"
            ]
            if remainder:
                rate_parts.append(
                    f"${float(tariff.rate_amount):,.2f} por {unit_label} x {remainder}"
                )

    return {
        "duration_seconds": total_seconds,
        "duration_label": format_duration(total_seconds),
        "billing_units": billable_units,
        "total": total,
        "rate_label": " + ".join(rate_parts),
    }
