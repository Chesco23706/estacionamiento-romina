import base64
import hashlib
import json
import re
from datetime import timedelta
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from flask import current_app, url_for
from py_vapid import VapidException
from pywebpush import WebPushException, webpush
from requests import RequestException, Session
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import User
from .owner_models import OwnerPushDelivery, OwnerPushDevice, OwnerShiftReport
from .owner_reports import ALL_SHIFTS


class PushSession(Session):
    def get_redirect_target(self, response):
        return None


def push_configured():
    return all(current_app.config[key] for key in (
        "OWNER_PUSH_PUBLIC_KEY", "OWNER_PUSH_PRIVATE_KEY", "OWNER_PUSH_SUBJECT",
    ))


def device_key(subscription):
    return hashlib.sha256(subscription["endpoint"].encode()).hexdigest()


def read_subscription(value):
    if not isinstance(value, dict) or not isinstance(value.get("keys"), dict):
        raise ValueError
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or not re.fullmatch(r"https://[^\s\\#]{1,2048}", endpoint):
        raise ValueError
    url = urlsplit(endpoint)
    host = url.hostname or ""
    trusted_host = host in {
        "fcm.googleapis.com", "android.googleapis.com", "updates.push.services.mozilla.com",
        "web.push.apple.com",
    } or host.endswith((".push.apple.com", ".notify.windows.com", ".push.services.mozilla.com"))
    if not trusted_host or url.netloc != host or not url.path.startswith("/"):
        raise ValueError

    keys = {}
    for name, length in (("p256dh", 65), ("auth", 16)):
        key = value["keys"].get(name)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,90}", key):
            raise ValueError
        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
        if len(raw) != length:
            raise ValueError
        if name == "p256dh":
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        keys[name] = key
    return {"endpoint": endpoint, "keys": keys}


def delete_device(device):
    OwnerPushDelivery.query.filter_by(device_id=device.id).delete()
    db.session.delete(device)
    db.session.commit()


def send_push(device, payload):
    try:
        with PushSession() as session:
            response = webpush(
                subscription_info=device.subscription,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=current_app.config["OWNER_PUSH_PRIVATE_KEY"],
                vapid_claims={"sub": current_app.config["OWNER_PUSH_SUBJECT"]},
                ttl=12 * 60 * 60,
                timeout=5,
                requests_session=session,
            )
        if 200 <= response.status_code < 300:
            return "sent"
    except WebPushException as error:
        if error.response is not None and error.response.status_code in (404, 410):
            delete_device(device)
            return "expired"
    except (RequestException, VapidException, ValueError):
        pass
    current_app.logger.warning("Falló el aviso Web Push del propietario.")
    return "retry"


def claim_delivery(report_id, device_id, now):
    delivery = OwnerPushDelivery.query.filter_by(report_id=report_id, device_id=device_id)
    if delivery.first():
        renewed = delivery.filter(
            OwnerPushDelivery.sent_at.is_(None),
            OwnerPushDelivery.claimed_at < now - timedelta(minutes=2),
        ).update({"claimed_at": now})
        db.session.commit()
        return bool(renewed)
    db.session.add(OwnerPushDelivery(report_id=report_id, device_id=device_id, claimed_at=now))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if delivery.first() or not db.session.get(OwnerPushDevice, device_id):
            return False
        raise
    return True


def send_due_notifications(now):
    if not push_configured():
        return 0, 0
    owner = User.query.filter_by(username=current_app.config["OWNER_MOBILE_USERNAME"]).first()
    if not owner or not owner.is_owner:
        return 0, 0
    if not OwnerPushDevice.query.filter_by(user_id=owner.id).first():
        return 0, 0
    labels = {shift.key: shift.label for shift in ALL_SHIFTS}
    reports = OwnerShiftReport.query.filter(
        OwnerShiftReport.closed_at >= now - timedelta(hours=24), OwnerShiftReport.closed_at <= now,
    ).all()
    sent = failed = 0
    for report in reports:
        device_ids = [
            device_id for (device_id,) in db.session.query(OwnerPushDevice.id).filter(
                OwnerPushDevice.user_id == owner.id, OwnerPushDevice.created_at <= report.closed_at,
            )
        ]
        for device_id in device_ids:
            if not claim_delivery(report.id, device_id, now):
                continue
            device = db.session.get(OwnerPushDevice, device_id)
            if not device:
                continue
            result = send_push(device, {
                "title": "Corte de Romina disponible",
                "body": f"{labels[report.shift]} · {report.business_date:%d/%m/%Y}",
                "url": url_for(
                    "owner.dashboard", day=report.business_date, shift=report.shift,
                    _anchor=f"shift-{report.shift}",
                ),
                "tag": f"romina-cut-{report.id}",
            })
            if result == "expired":
                continue
            delivery = db.session.get(OwnerPushDelivery, (report.id, device_id))
            if delivery:
                if result == "sent":
                    delivery.sent_at = now
                else:
                    db.session.delete(delivery)
                db.session.commit()
            sent += result == "sent"
            failed += result == "retry"
    return sent, failed
