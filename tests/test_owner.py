from base64 import urlsafe_b64encode
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app import create_app
from app.extensions import db
from app.models import CashMovement, User, VehicleRecord, WeeklyPrepayment
from app.owner_models import OwnerPushDelivery, OwnerPushDevice, OwnerShiftAssignment, OwnerShiftReport
from app.owner_reports import weekly_ledger
from app.pricing import ensure_utc
from config import TestingConfig


class OwnerTestingConfig(TestingConfig):
    CRON_SECRET = "local-test-secret"
    OWNER_MOBILE_USERNAME = "admin"
    OWNER_PUSH_PUBLIC_KEY = "test-public"
    OWNER_PUSH_PRIVATE_KEY = "test-private"
    OWNER_PUSH_SUBJECT = "mailto:qa@example.com"


def local_time(day, hour, minute, second=0):
    return ensure_utc(datetime(
        2026, 11, day, hour, minute, second, tzinfo=ZoneInfo("America/Mexico_City"),
    ))


def owner_login(client, target="/owner", remember=True):
    return client.post("/owner/login", query_string={"next": target}, data={
        "username": "admin", "password": "AdminRomina2026!",
        "remember_phone": "on" if remember else "",
    })


def subscription():
    public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint,
    )
    return {
        "endpoint": "https://fcm.googleapis.com/fcm/send/qa",
        "keys": {
            "p256dh": urlsafe_b64encode(public).decode().rstrip("="),
            "auth": urlsafe_b64encode(b"0123456789abcdef").decode().rstrip("="),
        },
    }


def test_owner_login_and_api_access():
    app = create_app(OwnerTestingConfig)
    client = app.test_client()
    url = "/owner?day=2026-11-08&shift=morning"
    assert client.get(url).location.startswith("/owner/login?next=")
    assert client.get("/owner/manifest.webmanifest").json["start_url"] == "/owner"
    assert client.put("/owner/device", json=subscription()).status_code == 401

    client.post("/login", data={"username": "empleado1", "password": "EmpleadoUno2026!"})
    assert client.get("/owner").status_code == 403
    assert client.get("/owner/settings").status_code == 403
    assert client.get("/owner/cuts/1").status_code == 403
    client.post("/logout")

    response = owner_login(client, url)
    assert response.location == url
    assert any(header.startswith("remember_token=") for header in response.headers.getlist("Set-Cookie"))
    client.delete_cookie(app.config["SESSION_COOKIE_NAME"])
    assert client.get(url).headers["Cache-Control"] == "no-store"
    assert client.get(url).status_code == 200
    assert client.put("/owner/device", json={
        **subscription(), "endpoint": "https://fcm.googleapis.com.example.net/qa",
    }).status_code == 400
    client.post("/logout")
    assert owner_login(client, "//example.com").location == "/owner"


def test_owner_closes_both_shifts_and_counts_weekly_advance_once(monkeypatch):
    app = create_app(OwnerTestingConfig)
    client = app.test_client()
    now = local_time(7, 8, 0)
    monkeypatch.setattr("app.owner.utc_now", lambda: now)
    with app.app_context():
        morning = User.query.filter_by(username="empleado1").one()
        night = User.query.filter_by(username="empleado2").one()
        morning_id, night_id = morning.id, night.id

        def payment(ticket, amount, paid, employee, **fields):
            return VehicleRecord(
                ticket_number=ticket, physical_ticket_number=ticket, client_name="QA",
                vehicle_type="Moto", plate_number=ticket, total_amount=amount,
                paid_at=paid, payment_user=employee, entry_user=morning, **fields,
            )

        advance = WeeklyPrepayment(
            client_name="QA", plate_number="QA-PRE", vehicle_type="Moto", rate_label="Semanal",
            movement=CashMovement(
                movement_type="income", description="Anticipo", amount=40,
                occurred_at=local_time(7, 11, 0), created_by=morning,
            ),
        )
        db.session.add(advance)
        db.session.flush()
        db.session.add_all([
            payment("QA-01", 100, local_time(7, 14, 30, 59), morning),
            payment("QA-02", 85, local_time(7, 14, 31), night),
            payment("QA-03", 40, local_time(7, 23, 59, 59), night),
            payment("QA-04", 45, local_time(8, 0, 0), night),
            payment("QA-05", 40, local_time(7, 13, 30), morning, weekly_prepayment_id=advance.id),
            CashMovement(
                movement_type="expense", description="Insumos", amount=15,
                occurred_at=local_time(7, 14, 30, 20), created_by=morning,
            ),
        ])
        db.session.commit()

    owner_login(client)
    assert client.post("/owner/settings", data={"morning": morning_id, "night": night_id}).status_code == 302
    path = "/owner/jobs/close-shifts"
    headers = {"Authorization": "Bearer local-test-secret"}
    assert client.get(path).status_code == 401
    now = local_time(7, 14, 30, 59)
    assert client.get(path, headers=headers).json["closed"] == 0
    now += timedelta(seconds=1)
    assert client.get(path, headers=headers).json["closed"] == 1
    assert client.get(path, headers=headers).json["closed"] == 0
    now = local_time(8, 0, 0)
    assert client.get(path, headers=headers).json["closed"] == 1

    with app.app_context():
        reports = {report.shift: report for report in OwnerShiftReport.query.all()}
        morning_report = reports["morning"].snapshot
        assert morning_report["net"] == "125.00"
        assert morning_report["advances"] == "40.00"
        assert [item["ticket"] for item in morning_report["payments"]] == ["QA-01"]
        assert [item["ticket"] for item in reports["night"].snapshot["payments"]] == ["QA-02", "QA-03"]
        assert str(reports["night"].business_date) == "2026-11-07"
        total, days, employees = weekly_ledger(reports["night"].business_date - timedelta(days=5), now)
        assert total.net == days[reports["night"].business_date].net == Decimal("250")
        assert len(employees) == 2

    response = client.get("/owner?day=2026-11-07&shift=night")
    assert response.status_code == 200
    assert 'id="detail-night" open' in response.text
    assert "QA-03" in response.text


def test_owner_rotates_monday_and_closes_the_full_sunday_for_morning_employee(monkeypatch):
    app = create_app(OwnerTestingConfig)
    client = app.test_client()
    now = local_time(8, 9, 0)
    monkeypatch.setattr("app.owner.utc_now", lambda: now)
    with app.app_context():
        first = User.query.filter_by(username="empleado1").one()
        second = User.query.filter_by(username="empleado2").one()
        first_id, second_id = first.id, second.id
        first_name, second_name = first.full_name, second.full_name
        db.session.add_all([
            CashMovement(
                movement_type="income", description="Entrada de domingo", amount=85,
                occurred_at=local_time(8, 10, 0), created_by=first,
            ),
            CashMovement(
                movement_type="expense", description="Gasto del domingo", amount=15,
                occurred_at=local_time(8, 23, 59, 59), created_by=first,
            ),
            CashMovement(
                movement_type="income", description="Otra caja", amount=25,
                occurred_at=local_time(8, 18, 0), created_by=second,
            ),
            CashMovement(
                movement_type="income", description="Entrada del lunes", amount=30,
                occurred_at=local_time(9, 0, 0), created_by=first,
            ),
        ])
        db.session.commit()

    owner_login(client)
    client.post("/owner/settings", data={"morning": first_id, "night": second_id})
    path = "/owner/jobs/close-shifts"
    headers = {"Authorization": "Bearer local-test-secret"}
    now = local_time(8, 23, 59, 59)
    assert client.get(path, headers=headers).json["closed"] == 0
    now = local_time(9, 0, 0)
    assert client.get(path, headers=headers).json["closed"] == 1
    assert client.get(path, headers=headers).json["closed"] == 0

    with app.app_context():
        sunday_report = OwnerShiftReport.query.one()
        assert sunday_report.shift == "sunday"
        assert sunday_report.employee_name == first_name
        assert sunday_report.snapshot["net"] == "70.00"
        assert [item["description"] for item in sunday_report.snapshot["movements"]] == [
            "Entrada de domingo", "Gasto del domingo",
        ]
    sunday = client.get("/owner?day=2026-11-08&shift=sunday").text
    assert 'id="detail-sunday" open' in sunday
    assert 'id="shift-morning"' not in sunday and 'id="shift-night"' not in sunday
    assert 'id="weekly-cuts" open' in sunday
    assert "Jornada del domingo" in sunday

    # Saving the displayed Monday assignments keeps the original rotation.
    client.post("/owner/settings", data={"morning": second_id, "night": first_id})
    with app.app_context():
        assert OwnerShiftAssignment.query.count() == 2

    now = local_time(10, 0, 0)
    client.get(path, headers=headers)
    with app.app_context():
        monday_reports = {
            report.shift: report.employee_name
            for report in OwnerShiftReport.query.filter_by(business_date=local_time(9, 12, 0).date())
        }
        assert monday_reports == {"morning": second_name, "night": first_name}

    now = local_time(16, 0, 0)
    client.get(path, headers=headers)
    with app.app_context():
        sundays = OwnerShiftReport.query.filter_by(shift="sunday").order_by(OwnerShiftReport.business_date)
        assert [report.employee_name for report in sundays] == [first_name, second_name]
    assert f'value="{first_id}" selected' in client.get("/owner/settings").text.split(
        'id="setting-morning"', 1,
    )[1].split("</select>", 1)[0]


def test_owner_push_retries_and_does_not_send_twice(monkeypatch):
    app = create_app(OwnerTestingConfig)
    client = app.test_client()
    before_close = local_time(7, 14, 0)
    now = before_close
    monkeypatch.setattr("app.owner.utc_now", lambda: now)
    owner_login(client)
    phone = subscription()
    assert client.put("/owner/device", json=phone).status_code == 204
    with app.app_context():
        OwnerPushDevice.query.one().created_at = before_close
        db.session.commit()
        employee_id = User.query.filter_by(username="empleado1").one().id
    client.post("/owner/settings", data={"morning": employee_id})
    now = local_time(7, 14, 35)
    sent = []

    def send(**kwargs):
        sent.append(kwargs["data"])
        return SimpleNamespace(status_code=502 if len(sent) == 1 else 201)

    monkeypatch.setattr("app.owner_push.webpush", send)
    path = "/owner/jobs/close-shifts"
    headers = {"Authorization": "Bearer local-test-secret"}
    assert client.get(path, headers=headers).status_code == 503
    assert client.get(path, headers=headers).json == {"closed": 0, "sent": 1, "failed": 0}
    assert client.get(path, headers=headers).json["sent"] == 0
    assert len(sent) == 2
    assert "shift=morning" in sent[1]
    with app.app_context():
        assert OwnerPushDelivery.query.one().sent_at is not None
    assert client.delete("/owner/device", json=phone).status_code == 204
    with app.app_context():
        assert OwnerPushDelivery.query.count() == 0
