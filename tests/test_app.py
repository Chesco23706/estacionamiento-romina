from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from app import create_app
from app.extensions import db
from app.models import CashCut, CashMovement, EmployeeSchedule, Role, Tariff, User, VehicleRecord, WeeklyPrepayment, get_period_bounds
from app.pricing import utc_now
from app.routes import build_record_history_events, build_ticket_board
from app.services import dashboard_metrics
from config import TestingConfig


def build_client():
    app = create_app(TestingConfig)
    return app, app.test_client()


def login(client, username="admin", password="AdminRomina2026!"):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def test_admin_login_and_vehicle_flow():
    app, client = build_client()

    response = login(client)
    assert response.status_code == 200

    create_response = client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-01",
            "client_name": "Cliente QA",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "ABC-123",
            "service_wash": "on",
            "service_oil_change": "on",
            "service_oil_price": "55.50",
            "notes": "Registro de prueba",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert "Abrir PDF" in create_response.get_data(as_text=True)

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-01").first()
        assert record is not None
        assert record.ticket_number.startswith("REC-")
        assert record.stay_mode == "weekly"
        assert record.contracted_days == 6
        assert record.service_wash is True
        assert record.service_oil_change is True
        record_id = record.id

    pay_response = client.post(f"/records/{record_id}/pay", follow_redirects=True)
    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.is_paid is True
        assert updated.exit_at is None

    exit_response = client.post(f"/records/{record_id}/exit", follow_redirects=True)
    cut_response = client.post(
        "/cuts/generate", data={"cut_type": "daily"}, follow_redirects=True
    )

    assert exit_response.status_code == 200
    assert pay_response.status_code == 200
    assert cut_response.status_code == 200

    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.status == "Pagado"
        assert float(updated.total_amount) >= 115.5
        assert CashCut.query.count() == 1


def test_weekly_prepayment_from_entry_form_waits_for_vehicle_arrival(monkeypatch):
    app, client = build_client()
    paid = datetime(2026, 11, 7, 10, tzinfo=ZoneInfo("America/Mexico_City"))
    monkeypatch.setattr("app.services.utc_now", lambda: paid.astimezone(timezone.utc))
    login(client, "empleado1", "EmpleadoUno2026!")

    payload = {
        "ticket_number": "PREPAGO-01",
        "client_name": "Cliente prepago",
        "vehicle_type": "Moto",
        "plate_number": "PRE-01",
        "stay_mode": "weekly",
        "weekly_prepaid": "on",
    }
    response = client.post("/records/new", data=payload, follow_redirects=True)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Pendientes de entrada (1)" in body
    assert "Cliente prepago" in body
    assert "Pendiente de entrada" in body
    client.post("/records/new", data=payload)

    with app.app_context():
        assert VehicleRecord.query.count() == 0
        prepayment = WeeklyPrepayment.query.one()
        assert prepayment.activated_at is None
        assert prepayment.movement.type_label == "Anticipo semanal"
        assert prepayment.movement.amount == 40
        employee_id = prepayment.movement.created_by_user_id

    response = client.post("/cuts/generate", data={
        "cut_type": "daily", "cut_date": "2026-11-07", "target_user_id": employee_id,
    })
    assert response.status_code == 302
    with app.app_context():
        cut = CashCut.query.one()
        assert cut.vehicles_paid == 0
        assert cut.extra_income == 40
        assert cut.total_income == 40


def test_weekly_advance_payment_is_collected_saturday_and_activated_monday(monkeypatch):
    app, client = build_client()
    local_tz = ZoneInfo("America/Mexico_City")
    paid = datetime(2026, 11, 7, 10, tzinfo=local_tz)
    arrival = datetime(2026, 11, 9, 8, tzinfo=local_tz)
    monkeypatch.setattr("app.services.utc_now", lambda: paid.astimezone(timezone.utc))
    login(client, "empleado1", "EmpleadoUno2026!")
    payload = {"client_name": "Cliente reserva", "vehicle_type": "Moto", "plate_number": "RES-01"}
    for _ in range(2):
        assert client.post("/weekly-prepayments/new", data=payload).status_code == 302

    with app.app_context():
        prepayment = WeeklyPrepayment.query.one()
        assert VehicleRecord.query.count() == 0
        assert CashMovement.query.count() == 1
        assert prepayment.movement.amount == 40
        assert prepayment.movement.type_label == "Anticipo semanal"
        employee_id = prepayment.movement.created_by_user_id
        prepayment_id = prepayment.id

    def cut_on(day):
        return client.post("/cuts/generate", data={
            "cut_type": "daily", "cut_date": day, "target_user_id": employee_id,
        }, follow_redirects=True)

    assert "Anticipo semanal" in cut_on("2026-11-07").get_data(as_text=True)
    monkeypatch.setattr("app.services.utc_now", lambda: arrival.astimezone(timezone.utc))
    client.post("/logout")
    login(client, "empleado2", "EmpleadoDos2026!")
    activate_url = f"/weekly-prepayments/{prepayment_id}/activate"
    response = client.post(activate_url, data={"ticket_number": "015"}, follow_redirects=True)
    assert "Vigencia semanal: 2026-11-09 08:00:00 al 2026-11-15 08:00:00" in response.get_data(as_text=True)
    client.post(activate_url, data={"ticket_number": "016"})
    cut_on("2026-11-07")
    cut_on("2026-11-09")

    with app.app_context():
        record = VehicleRecord.query.one()
        assert record.is_paid
        assert record.payment_user.username == "empleado1"
        assert record.entry_user.username == "empleado2"
        assert CashMovement.query.count() == 1
        cuts = CashCut.query.order_by(CashCut.id).all()
        assert [cut.total_income for cut in cuts] == [40, 40, 0]
        assert [cut.extra_income for cut in cuts] == [40, 40, 0]


def test_daily_cut_uses_payment_day_not_entry_day():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-CORTE-01",
            "client_name": "Cliente Corte",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "COR-001",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-CORTE-01").first()
        yesterday = utc_now() - timedelta(days=1)
        record.entry_at = yesterday
        db.session.commit()
        record_id = record.id

    client.post(f"/records/{record_id}/pay", follow_redirects=True)
    cut_response = client.post(
        "/cuts/generate", data={"cut_type": "daily"}, follow_redirects=True
    )

    assert cut_response.status_code == 200

    with app.app_context():
        cut = CashCut.query.order_by(CashCut.id.desc()).first()
        assert cut is not None
        assert float(cut.total_income) > 0
        assert cut.vehicles_paid >= 1


def test_admin_can_generate_dated_cut_and_download_report_pdf():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-CORTE-FECHA",
            "client_name": "Cliente Corte Fecha",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "FEC-001",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-CORTE-FECHA").first()
        target_local_date = datetime(2026, 1, 15, tzinfo=ZoneInfo("America/Mexico_City"))
        paid_at = target_local_date.replace(hour=14).astimezone(timezone.utc)
        record.entry_at = target_local_date.replace(hour=9).astimezone(timezone.utc)
        record.paid_at = paid_at
        record.total_amount = 75
        record.status = "Dentro del estacionamiento"
        db.session.commit()

    cut_response = client.post(
        "/cuts/generate",
        data={"cut_type": "daily", "cut_date": "2026-01-15"},
        follow_redirects=True,
    )
    body = cut_response.get_data(as_text=True)

    assert cut_response.status_code == 200
    assert "Cliente Corte Fecha" in body
    assert "Vehiculos cobrados" in body
    assert "Informe PDF" in body
    assert "$75.00" in body

    with app.app_context():
        cut = CashCut.query.order_by(CashCut.id.desc()).first()
        assert cut is not None
        assert cut.vehicles_paid == 1
        assert float(cut.total_income) == 75

    pdf_response = client.get(f"/cuts/{cut.id}/document")

    assert pdf_response.status_code == 200
    assert pdf_response.mimetype == "application/pdf"
    assert pdf_response.data.startswith(b"%PDF")
    assert "Corte de 15-01-2026.pdf" in pdf_response.headers["Content-Disposition"]

    ticket_response = client.get(f"/cuts/{cut.id}/ticket")
    assert ticket_response.status_code == 200
    assert "Corte de 15-01-2026 ticket.pdf" in ticket_response.headers["Content-Disposition"]

    csv_response = client.get(f"/cuts/{cut.id}/export")
    assert csv_response.status_code == 200
    assert 'filename="Corte de 15-01-2026.csv"' in csv_response.headers["Content-Disposition"]


def test_daily_and_weekly_cuts_exclude_unpaid_weekly_records():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SEMANAL-SIN-PAGO",
            "client_name": "Cliente Semanal Sin Pago",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "SSP-001",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(client_name="Cliente Semanal Sin Pago").first()
        record.entry_at = utc_now()
        record.total_amount = 320
        record.paid_at = None
        db.session.commit()

    daily_response = client.post(
        "/cuts/generate",
        data={"cut_type": "daily", "cut_date": utc_now().date().isoformat()},
        follow_redirects=True,
    )
    daily_body = daily_response.get_data(as_text=True)

    assert daily_response.status_code == 200
    assert "Cliente Semanal Sin Pago" not in daily_body
    assert "$320.00" not in daily_body

    with app.app_context():
        daily_cut = CashCut.query.order_by(CashCut.id.desc()).first()
        assert float(daily_cut.total_income) == 0
        assert daily_cut.vehicles_paid == 0
        assert daily_cut.vehicles_served == 0

    weekly_response = client.post(
        "/cuts/generate",
        data={"cut_type": "weekly"},
        follow_redirects=True,
    )
    weekly_body = weekly_response.get_data(as_text=True)

    assert weekly_response.status_code == 200
    assert "Cliente Semanal Sin Pago" not in weekly_body
    assert "$320.00" not in weekly_body

    with app.app_context():
        weekly_cut = CashCut.query.order_by(CashCut.id.desc()).first()
        assert float(weekly_cut.total_income) == 0
        assert weekly_cut.vehicles_paid == 0
        assert weekly_cut.vehicles_served == 0


def test_weekly_cut_period_runs_monday_to_sunday():
    app, _client = build_client()
    with app.app_context():
        start, end = get_period_bounds("weekly", reference_date=datetime(2026, 6, 25).date())

    local_tz = ZoneInfo("America/Mexico_City")
    local_start = start.astimezone(local_tz)
    local_end = end.astimezone(local_tz)

    assert local_start.weekday() == 0
    assert local_start.hour == 0
    assert local_start.minute == 0
    assert (local_end - timedelta(seconds=1)).weekday() == 6
    assert local_end.weekday() == 0
    assert (local_end - local_start).days == 7


def test_weekly_cut_download_filename_uses_start_day_and_date_range():
    app, client = build_client()
    login(client)

    local_tz = ZoneInfo("America/Mexico_City")
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        local_start = datetime(2026, 6, 22, tzinfo=local_tz)
        local_end = datetime(2026, 6, 29, tzinfo=local_tz)
        cut = CashCut(
            cut_type="weekly",
            period_start=local_start.astimezone(timezone.utc),
            period_end=local_end.astimezone(timezone.utc),
            generated_by=admin,
            total_income=0,
            extra_income=0,
            total_expenses=0,
            net_total=0,
            total_pending=0,
            vehicles_served=0,
            vehicles_paid=0,
            breakdown_json="{}",
        )
        db.session.add(cut)
        db.session.commit()
        cut_id = cut.id

    pdf_response = client.get(f"/cuts/{cut_id}/document")
    expected = "Corte semanal del lunes 22-06-2026 - 28-06-2026.pdf"
    assert pdf_response.status_code == 200
    assert expected in pdf_response.headers["Content-Disposition"]


def test_cuts_page_shows_detailed_period_options():
    _app, client = build_client()
    login(client, "empleado1", "EmpleadoUno2026!")

    response = client.get("/cuts")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Tipo de corte" in body
    assert '<option value="daily">Diario</option>' in body
    assert '<option value="weekly">Semana</option>' in body
    assert '<option value="monthly">Mensual</option>' in body
    assert "Fecha especifica" in body
    assert 'name="week_start_date"' in body
    assert 'name="week_end_date"' in body
    assert 'name="month_date"' in body
    assert 'name="custom_start_date"' in body
    assert 'name="custom_end_date"' in body


def test_monthly_cut_uses_selected_month_period_and_filename():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-CORTE-MES",
            "client_name": "Cliente Corte Mes",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "MES-001",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-CORTE-MES").first()
        target_local_date = datetime(2026, 2, 15, tzinfo=ZoneInfo("America/Mexico_City"))
        record.entry_at = target_local_date.replace(hour=9).astimezone(timezone.utc)
        record.paid_at = target_local_date.replace(hour=12).astimezone(timezone.utc)
        record.total_amount = 90
        db.session.commit()

    cut_response = client.post(
        "/cuts/generate",
        data={"cut_type": "monthly", "month_date": "2026-02"},
        follow_redirects=True,
    )
    body = cut_response.get_data(as_text=True)

    assert cut_response.status_code == 200
    assert "Corte mensual" in body
    assert "Cliente Corte Mes" in body
    assert "$90.00" in body

    with app.app_context():
        cut = CashCut.query.order_by(CashCut.id.desc()).first()
        assert cut.cut_type == "monthly"
        local_start = cut.period_start.astimezone(ZoneInfo("America/Mexico_City"))
        local_end = cut.period_end.astimezone(ZoneInfo("America/Mexico_City"))
        assert local_start.year == 2026
        assert local_start.month == 2
        assert local_start.day == 1
        assert local_end.month == 3
        assert local_end.day == 1
        cut_id = cut.id

    pdf_response = client.get(f"/cuts/{cut_id}/document")
    assert pdf_response.status_code == 200
    assert "Corte mensual de 02-2026.pdf" in pdf_response.headers["Content-Disposition"]


def test_weekly_cut_can_use_explicit_start_and_end_dates():
    app, client = build_client()
    login(client)

    response = client.post(
        "/cuts/generate",
        data={
            "cut_type": "weekly",
            "week_start_date": "2026-03-02",
            "week_end_date": "2026-03-08",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        cut = CashCut.query.order_by(CashCut.id.desc()).first()
        local_start = cut.period_start.astimezone(ZoneInfo("America/Mexico_City"))
        local_end = cut.period_end.astimezone(ZoneInfo("America/Mexico_City"))
        assert cut.cut_type == "weekly"
        assert local_start.date().isoformat() == "2026-03-02"
        assert local_end.date().isoformat() == "2026-03-09"
        cut_id = cut.id

    pdf_response = client.get(f"/cuts/{cut_id}/document")
    assert "Corte semanal del lunes 02-03-2026 - 08-03-2026.pdf" in pdf_response.headers["Content-Disposition"]


def test_custom_cut_can_use_explicit_date_range():
    app, client = build_client()
    login(client)

    response = client.post(
        "/cuts/generate",
        data={
            "cut_type": "custom",
            "custom_start_date": "2026-04-10",
            "custom_end_date": "2026-04-12",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Corte fecha especifica" in response.get_data(as_text=True)
    with app.app_context():
        cut = CashCut.query.order_by(CashCut.id.desc()).first()
        local_start = cut.period_start.astimezone(ZoneInfo("America/Mexico_City"))
        local_end = cut.period_end.astimezone(ZoneInfo("America/Mexico_City"))
        assert cut.cut_type == "custom"
        assert local_start.date().isoformat() == "2026-04-10"
        assert local_end.date().isoformat() == "2026-04-13"
        cut_id = cut.id

    pdf_response = client.get(f"/cuts/{cut_id}/document")
    assert "Corte de 10-04-2026 - 12-04-2026.pdf" in pdf_response.headers["Content-Disposition"]


def test_employee_dashboard_counts_income_by_payment_day():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-PAGO-HOY",
            "client_name": "Cliente Pago Hoy",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "PAG-777",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-PAGO-HOY").first()
        record.entry_at = utc_now() - timedelta(days=1)
        db.session.commit()
        record_id = record.id

    client.post(f"/records/{record_id}/pay", follow_redirects=True)
    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    response = client.get("/")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "$10.00" in body


def test_employee_can_register_cash_movements_and_generate_daily_cut_ticket():
    app, client = build_client()
    login(client, "empleado1", "EmpleadoUno2026!")

    income_response = client.post(
        "/cash-movements/new",
        data={
            "movement_type": "income",
            "description": "Entrada inicial",
            "amount": "100",
        },
        follow_redirects=True,
    )
    expense_response = client.post(
        "/cash-movements/new",
        data={
            "movement_type": "expense",
            "description": "Compra de papel",
            "amount": "25",
        },
        follow_redirects=True,
    )
    dashboard_response = client.get("/")
    dashboard_body = dashboard_response.get_data(as_text=True)

    assert income_response.status_code == 200
    assert expense_response.status_code == 200
    assert dashboard_response.status_code == 200
    assert "Total caja hoy" in dashboard_body
    assert 'data-open-dialog="cash-expense-dialog"' in dashboard_body
    assert 'data-open-dialog="cash-income-dialog"' in dashboard_body
    assert 'data-open-dialog="cash-movements-dialog"' in dashboard_body
    assert 'id="cash-expense-dialog"' in dashboard_body
    assert 'id="cash-income-dialog"' in dashboard_body
    assert 'id="cash-movements-dialog"' in dashboard_body
    assert "$75.00" in dashboard_body
    assert "Entrada inicial" in dashboard_body
    assert "Compra de papel" in dashboard_body

    cut_response = client.post(
        "/cuts/generate",
        data={"cut_type": "daily"},
        follow_redirects=True,
    )
    cut_body = cut_response.get_data(as_text=True)

    assert cut_response.status_code == 200
    assert "Total caja" in cut_body
    assert "Otros ingresos" in cut_body
    assert "Gastos" in cut_body
    assert "Entrada inicial" in cut_body
    assert "Compra de papel" in cut_body

    with app.app_context():
        cut = CashCut.query.order_by(CashCut.id.desc()).first()
        assert cut is not None
        assert CashMovement.query.count() == 2
        assert float(cut.total_income) == 75
        assert float(cut.extra_income) == 100
        assert float(cut.total_expenses) == 25
        cut_id = cut.id

    ticket_response = client.get(f"/cuts/{cut_id}/ticket")
    assert ticket_response.status_code == 200
    assert ticket_response.mimetype == "application/pdf"
    assert ticket_response.data.startswith(b"%PDF")


def test_employee_dashboard_shows_only_amount_collected_by_current_user():
    app, client = build_client()
    login(client)

    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        empleado1 = User.query.filter_by(username="empleado1").first()
        empleado2 = User.query.filter_by(username="empleado2").first()
        paid_at = utc_now()
        db.session.add_all(
            [
                VehicleRecord(
                    ticket_number="REC-COBRO-EMP1",
                    physical_ticket_number="COBRO-EMP1",
                    client_name="Cobro Empleado Uno",
                    vehicle_type="Moto",
                    plate_number="EMP1",
                    entry_at=paid_at - timedelta(hours=1),
                    paid_at=paid_at,
                    total_amount=30,
                    status="Dentro del estacionamiento",
                    entry_user=admin,
                    payment_user=empleado1,
                ),
                VehicleRecord(
                    ticket_number="REC-COBRO-EMP2",
                    physical_ticket_number="COBRO-EMP2",
                    client_name="Cobro Empleado Dos",
                    vehicle_type="Moto",
                    plate_number="EMP2",
                    entry_at=paid_at - timedelta(hours=1),
                    paid_at=paid_at,
                    total_amount=80,
                    status="Dentro del estacionamiento",
                    entry_user=admin,
                    payment_user=empleado2,
                ),
                CashMovement(
                    movement_type="expense",
                    description="Gasto empleado uno dashboard",
                    amount=10,
                    occurred_at=paid_at,
                    created_by=empleado1,
                ),
                CashMovement(
                    movement_type="expense",
                    description="Gasto empleado dos dashboard",
                    amount=50,
                    occurred_at=paid_at,
                    created_by=empleado2,
                ),
            ]
        )
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login_response = login(client, "empleado1", "EmpleadoUno2026!")
    body = login_response.get_data(as_text=True)

    assert login_response.status_code == 200
    assert "Cobrado por ti" in body
    assert "$20.00" in body
    assert "$30.00" in body
    assert "Gastos $10.00" in body
    assert "Vehiculos $30.00" in body


def test_cash_cut_can_be_filtered_by_user():
    app, client = build_client()
    login(client)

    local_tz = ZoneInfo("America/Mexico_City")
    target_day = datetime(2026, 7, 7, tzinfo=local_tz)
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        empleado1 = User.query.filter_by(username="empleado1").first()
        empleado2 = User.query.filter_by(username="empleado2").first()
        paid_by_employee = VehicleRecord(
            ticket_number="REC-USUARIO-1",
            physical_ticket_number="USR-1",
            client_name="Pago Empleado Uno",
            vehicle_type="Moto",
            plate_number="UNO",
            entry_at=target_day.replace(hour=8).astimezone(timezone.utc),
            paid_at=target_day.replace(hour=9).astimezone(timezone.utc),
            total_amount=30,
            status="Dentro del estacionamiento",
            entry_user=admin,
            payment_user=empleado1,
        )
        paid_by_other = VehicleRecord(
            ticket_number="REC-USUARIO-2",
            physical_ticket_number="USR-2",
            client_name="Pago Empleado Dos",
            vehicle_type="Moto",
            plate_number="DOS",
            entry_at=target_day.replace(hour=8).astimezone(timezone.utc),
            paid_at=target_day.replace(hour=10).astimezone(timezone.utc),
            total_amount=80,
            status="Dentro del estacionamiento",
            entry_user=admin,
            payment_user=empleado2,
        )
        db.session.add_all(
            [
                paid_by_employee,
                paid_by_other,
                CashMovement(
                    movement_type="income",
                    description="Entrada empleado uno",
                    amount=50,
                    occurred_at=target_day.replace(hour=11).astimezone(timezone.utc),
                    created_by=empleado1,
                ),
                CashMovement(
                    movement_type="expense",
                    description="Gasto empleado uno",
                    amount=10,
                    occurred_at=target_day.replace(hour=12).astimezone(timezone.utc),
                    created_by=empleado1,
                ),
                CashMovement(
                    movement_type="income",
                    description="Entrada empleado dos",
                    amount=200,
                    occurred_at=target_day.replace(hour=13).astimezone(timezone.utc),
                    created_by=empleado2,
                ),
            ]
        )
        db.session.commit()
        empleado1_id = empleado1.id

    response = client.post(
        "/cuts/generate",
        data={
            "cut_type": "daily",
            "cut_date": "2026-07-07",
            "target_user_id": str(empleado1_id),
        },
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Usuario del corte" in body
    assert "Empleado 1" in body
    assert "Pago Empleado Uno" in body
    assert "Pago Empleado Dos" not in body
    assert "Entrada empleado uno" in body
    assert "Gasto empleado uno" in body
    assert "Entrada empleado dos" not in body

    with app.app_context():
        cut = CashCut.query.order_by(CashCut.id.desc()).first()
        assert cut.target_user_id == empleado1_id
        assert cut.vehicles_paid == 1
        assert float(cut.extra_income) == 50
        assert float(cut.total_expenses) == 10
        assert float(cut.total_income) == 70


def test_employee_paid_view_shows_records_paid_today_even_if_entered_yesterday():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-PAGADA-HOY",
            "client_name": "Cliente Pago Visible",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "PAG-888",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-PAGADA-HOY").first()
        record.entry_at = utc_now() - timedelta(days=1)
        db.session.commit()
        record_id = record.id

    client.post(f"/records/{record_id}/pay", follow_redirects=True)
    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    response = client.get("/?view=pagados")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Pago Visible" in body


def test_employee_inside_filter_shows_active_weekly_records_from_previous_day():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SEM-DENTRO",
            "client_name": "Cliente Semanal Dentro",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "SEM-DEN",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-SEM-DENTRO").first()
        record.entry_at = utc_now() - timedelta(days=1)
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    response = client.get("/?view=dentro")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Vehiculos dentro ahora" in body
    assert "Cliente Semanal Dentro" in body
    assert "FICHA-SEM-DENTRO" in body


def test_employee_today_exits_show_final_and_weekly_day_exits_from_previous_entries():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SALIDA-FINAL",
            "client_name": "Cliente Salida Final Hoy",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "SAL-FIN",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SALIDA-DIA",
            "client_name": "Cliente Salida Dia Hoy",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "SAL-DIA",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SIGUE-DENTRO",
            "client_name": "Cliente Sigue Dentro",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "SIG-DEN",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        final_record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-SALIDA-FINAL").first()
        weekly_record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-SALIDA-DIA").first()
        final_record.entry_at = utc_now() - timedelta(days=1)
        weekly_record.entry_at = utc_now() - timedelta(days=1)
        db.session.commit()
        final_id = final_record.id
        weekly_id = weekly_record.id

    client.post(f"/records/{final_id}/pay", follow_redirects=True)
    client.post(f"/records/{final_id}/exit", follow_redirects=True)
    client.post(f"/records/{weekly_id}/weekly-exit", follow_redirects=True)

    today = utc_now().astimezone(ZoneInfo("America/Mexico_City")).date().isoformat()
    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    records_response = client.get(f"/records?date={today}&date_field=exit")
    records_body = records_response.get_data(as_text=True)
    assert records_response.status_code == 200
    assert "Cliente Salida Final Hoy" in records_body
    assert "Cliente Salida Dia Hoy" in records_body
    assert "Fichas activas" not in records_body
    assert "Cliente Sigue Dentro" not in records_body

    dashboard_response = client.get("/?view=salidos")
    dashboard_body = dashboard_response.get_data(as_text=True)
    assert dashboard_response.status_code == 200
    assert "Cliente Salida Final Hoy" in dashboard_body
    assert "Cliente Salida Dia Hoy" in dashboard_body


def test_admin_dashboard_uses_local_payment_day_for_totals():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-ZONA",
            "client_name": "Cliente Zona",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "ZON-001",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-ZONA").first()
        tz = ZoneInfo("America/Mexico_City")
        now_local = utc_now().astimezone(tz)
        yesterday_local_late = datetime(
            now_local.year,
            now_local.month,
            now_local.day,
            23,
            30,
            tzinfo=tz,
        ) - timedelta(days=1)
        record.total_amount = 10
        record.paid_at = yesterday_local_late.astimezone(timezone.utc)
        db.session.commit()

        metrics = dashboard_metrics()
        assert float(metrics["total_day"]) == 0.0


def test_admin_dashboard_pending_total_uses_today_live_unpaid_records_only():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "PEND-HOY",
            "client_name": "Pendiente Hoy",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "PEN-HOY",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "PEND-VIEJO",
            "client_name": "Pendiente Viejo",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "PEN-VIE",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        today_record = VehicleRecord.query.filter_by(physical_ticket_number="PEND-HOY").first()
        old_record = VehicleRecord.query.filter_by(physical_ticket_number="PEND-VIEJO").first()
        today_record.total_amount = 250
        old_record.entry_at = utc_now() - timedelta(days=2)
        old_record.total_amount = 999
        db.session.commit()

        metrics = dashboard_metrics()

    assert metrics["pending_total"] == 10.0


def test_admin_paid_filter_can_use_payment_date():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-PAGO-FILTRO",
            "client_name": "Cliente Pago Filtro",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "FIL-001",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-PAGO-FILTRO").first()
        record.entry_at = utc_now() - timedelta(days=1)
        db.session.commit()
        record_id = record.id

    client.post(f"/records/{record_id}/pay", follow_redirects=True)

    today = utc_now().astimezone(ZoneInfo("America/Mexico_City")).date().isoformat()
    response = client.get(f"/records?status=Pagado&date={today}&date_field=payment")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Pago Filtro" in body


def test_admin_record_history_dialog_shows_ticket_events():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-HISTORIAL",
            "client_name": "Cliente Historial",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "HIS-001",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-HISTORIAL").first()
        record_id = record.id

    client.post(f"/records/{record_id}/pay", follow_redirects=True)
    client.post(f"/records/{record_id}/exit", follow_redirects=True)
    response = client.get("/records")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert f'data-open-dialog="record-history-dialog-{record_id}"' in body
    assert f'id="record-history-dialog-{record_id}"' in body
    assert "Registro de entrada" in body
    assert "Pago recibido" in body
    assert "Salida final" in body
    assert "Fecha y hora" in body


def test_record_history_sorts_mixed_timezone_datetimes():
    record = SimpleNamespace(
        entry_at=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
        paid_at=None,
        exit_at=None,
        status="Dentro del estacionamiento",
        entry_user=SimpleNamespace(full_name="Admin"),
        payment_user=None,
        exit_user=None,
        weekly_exit_logs=[],
    )
    audit_log = SimpleNamespace(
        action="vehicle_record_updated",
        created_at=datetime(2026, 1, 1, 9),
        user=SimpleNamespace(full_name="Admin"),
    )

    events = build_record_history_events(record, [audit_log])

    assert [event["event"] for event in events] == [
        "Registro de entrada",
        "Edicion de registro",
    ]


def test_record_note_action_uses_note_or_add_icon():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-NOTA",
            "client_name": "Cliente Con Nota",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "NOT-001",
            "notes": "Dejo casco negro.",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SIN-NOTA",
            "client_name": "Cliente Sin Nota",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "SIN-001",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        with_note = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-NOTA").first()
        without_note = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-SIN-NOTA").first()

    response = client.get("/records")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert f'data-open-dialog="record-note-dialog-{with_note.id}"' in body
    assert f'id="record-note-dialog-{with_note.id}"' in body
    assert "Dejo casco negro." in body
    assert f'aria-label="Agregar nota a la ficha {without_note.display_ticket_number}"' in body
    assert f'data-open-dialog="record-dialog-{without_note.id}"' in body


def test_admin_records_page_shows_today_payment_shortcut():
    _app, client = build_client()
    login(client)

    response = client.get("/records")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Pagos de hoy" in body
    assert "Para validar el corte del dia usa" in body
    assert "Corte actual PDF" in body
    assert "employee-record-grid admin-record-grid" in body
    assert "admin-legacy-record-table" in body
    assert "Ordenar por entrada" in body


def test_admin_home_is_simplified_and_cuts_have_own_page():
    _app, client = build_client()
    login(client)

    home_response = client.get("/")
    home_body = home_response.get_data(as_text=True)

    assert home_response.status_code == 200
    assert "Cortes" in home_body
    assert "Vehiculos que entraron hoy" in home_body
    assert "Total cobrado hoy" in home_body
    assert "Total pendiente" in home_body
    assert "Vehiculos por tipo" not in home_body
    assert "Ultimos registros" not in home_body
    assert "Modo oscuro" not in home_body
    assert "data-theme-toggle" not in home_body

    cuts_response = client.get("/cuts")
    cuts_body = cuts_response.get_data(as_text=True)

    assert cuts_response.status_code == 200
    assert "Tipo de corte" in cuts_body
    assert '<option value="daily">Diario</option>' in cuts_body
    assert '<option value="weekly">Semana</option>' in cuts_body
    assert '<option value="monthly">Mensual</option>' in cuts_body
    assert "Fecha especifica" in cuts_body
    assert "Cortes generados" in cuts_body


def test_admin_dashboard_shows_collected_amount_by_user():
    app, client = build_client()
    login(client)

    paid_at = utc_now()
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        empleado1 = User.query.filter_by(username="empleado1").first()
        empleado2 = User.query.filter_by(username="empleado2").first()
        db.session.add_all(
            [
                VehicleRecord(
                    ticket_number="REC-COBRO-U1",
                    physical_ticket_number="COBRO-U1",
                    client_name="Cobro Usuario Uno",
                    vehicle_type="Moto",
                    plate_number="U1",
                    entry_at=paid_at - timedelta(hours=1),
                    paid_at=paid_at,
                    total_amount=30,
                    status="Dentro del estacionamiento",
                    entry_user=admin,
                    payment_user=empleado1,
                ),
                VehicleRecord(
                    ticket_number="REC-COBRO-U2",
                    physical_ticket_number="COBRO-U2",
                    client_name="Cobro Usuario Dos",
                    vehicle_type="Moto",
                    plate_number="U2",
                    entry_at=paid_at - timedelta(hours=1),
                    paid_at=paid_at,
                    total_amount=80,
                    status="Dentro del estacionamiento",
                    entry_user=admin,
                    payment_user=empleado2,
                ),
                CashMovement(
                    movement_type="expense",
                    description="Gasto dashboard empleado uno",
                    amount=5,
                    occurred_at=paid_at,
                    created_by=empleado1,
                ),
                CashMovement(
                    movement_type="income",
                    description="Entrada dashboard empleado dos",
                    amount=20,
                    occurred_at=paid_at,
                    created_by=empleado2,
                ),
                CashMovement(
                    movement_type="expense",
                    description="Gasto dashboard empleado dos",
                    amount=10,
                    occurred_at=paid_at,
                    created_by=empleado2,
                ),
            ]
        )
        db.session.commit()

    response = client.get("/")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cobrado hoy por cada empleado" in body
    assert "Empleado 1" in body
    assert "$25.00" in body
    assert "$30.00" in body
    assert "Gastos $5.00" in body
    assert "Empleado 2" in body
    assert "$90.00" in body
    assert "$80.00" in body
    assert "Otros ingresos $20.00" in body
    assert "Gastos $10.00" in body


def test_admin_dashboard_shows_vehicle_and_income_charts_by_period():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "GRAF-001",
            "client_name": "Cliente Grafica Moto",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "GRA-001",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "GRAF-002",
            "client_name": "Cliente Grafica Bici",
            "vehicle_type": "Bicicleta",
            "stay_mode": "hourly",
            "plate_number": "GRA-002",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        target_date = datetime(2026, 2, 10, 10, tzinfo=ZoneInfo("America/Mexico_City"))
        moto = VehicleRecord.query.filter_by(physical_ticket_number="GRAF-001").first()
        bici = VehicleRecord.query.filter_by(physical_ticket_number="GRAF-002").first()
        moto.entry_at = target_date.astimezone(timezone.utc)
        moto.paid_at = target_date.replace(hour=12).astimezone(timezone.utc)
        moto.total_amount = 80
        bici.entry_at = target_date.replace(hour=11).astimezone(timezone.utc)
        db.session.commit()

    response = client.get("/?period=custom&date_from=2026-02-10&date_to=2026-02-10")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Entradas y ganancias por tipo de vehiculo" in body
    assert "Vehiculos entrantes" in body
    assert "Ganancias cobradas" in body
    assert "Moto" in body
    assert "Bicicleta" in body
    assert "$80.00" in body


def test_employee_records_page_shows_today_payment_shortcut():
    _app, client = build_client()
    login(client, username="empleado1", password="EmpleadoUno2026!")

    response = client.get("/records")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Pagos de hoy" in body
    assert "Ese filtro toma la fecha real del pago" in body
    assert "Corte actual PDF" in body


def test_admin_can_download_current_cut_pdf():
    _app, client = build_client()
    login(client)

    response = client.get("/reports/current-cut/document")

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.headers["Content-Disposition"].startswith("inline")


def test_employee_can_download_current_cut_pdf():
    _app, client = build_client()
    login(client, username="empleado1", password="EmpleadoUno2026!")

    response = client.get("/reports/current-cut/document")

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"


def test_paid_records_are_ordered_by_payment_time_desc():
    app, client = build_client()
    login(client)

    local_noon = utc_now().astimezone(ZoneInfo("America/Mexico_City")).replace(
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )
    payment_base = local_noon.astimezone(timezone.utc)
    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        older_paid = VehicleRecord(
            ticket_number="PAGO-OLD",
            physical_ticket_number="81",
            client_name="Pago Viejo",
            vehicle_type="Moto",
            plate_number="OLD-001",
            entry_at=payment_base - timedelta(hours=5),
            paid_at=payment_base - timedelta(hours=2),
            total_amount=10,
            status="Pagado",
            entry_user=user,
            payment_user=user,
        )
        newer_paid = VehicleRecord(
            ticket_number="PAGO-NEW",
            physical_ticket_number="82",
            client_name="Pago Nuevo",
            vehicle_type="Moto",
            plate_number="NEW-001",
            entry_at=payment_base - timedelta(hours=4),
            paid_at=payment_base - timedelta(minutes=30),
            total_amount=20,
            status="Pagado",
            entry_user=user,
            payment_user=user,
        )
        db.session.add_all([older_paid, newer_paid])
        db.session.commit()

    today = local_noon.date().isoformat()
    response = client.get(f"/records?status=Pagado&date={today}&date_field=payment")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert body.index("Pago Nuevo") < body.index("Pago Viejo")


def test_records_can_sort_by_entry_ascending():
    app, client = build_client()
    login(client)

    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        older_entry = VehicleRecord(
            ticket_number="ENTRY-OLD",
            physical_ticket_number="91",
            client_name="Entrada Vieja",
            vehicle_type="Moto",
            plate_number="ENT-001",
            entry_at=utc_now() - timedelta(hours=5),
            status="Dentro del estacionamiento",
            entry_user=user,
        )
        newer_entry = VehicleRecord(
            ticket_number="ENTRY-NEW",
            physical_ticket_number="92",
            client_name="Entrada Nueva",
            vehicle_type="Moto",
            plate_number="ENT-002",
            entry_at=utc_now() - timedelta(hours=1),
            status="Dentro del estacionamiento",
            entry_user=user,
        )
        db.session.add_all([older_entry, newer_entry])
        db.session.commit()

    response = client.get("/records?sort_by=entry&sort_dir=asc")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert body.index("Entrada Vieja") < body.index("Entrada Nueva")


def test_records_can_sort_by_exit_descending():
    app, client = build_client()
    login(client)

    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        older_exit = VehicleRecord(
            ticket_number="EXIT-OLD",
            physical_ticket_number="93",
            client_name="Salida Vieja",
            vehicle_type="Moto",
            plate_number="SAL-001",
            entry_at=utc_now() - timedelta(hours=6),
            exit_at=utc_now() - timedelta(hours=3),
            duration_seconds=10800,
            total_amount=10,
            status="Pagado",
            entry_user=user,
            exit_user=user,
            payment_user=user,
            paid_at=utc_now() - timedelta(hours=4),
        )
        newer_exit = VehicleRecord(
            ticket_number="EXIT-NEW",
            physical_ticket_number="94",
            client_name="Salida Nueva",
            vehicle_type="Moto",
            plate_number="SAL-002",
            entry_at=utc_now() - timedelta(hours=4),
            exit_at=utc_now() - timedelta(hours=1),
            duration_seconds=10800,
            total_amount=10,
            status="Pagado",
            entry_user=user,
            exit_user=user,
            payment_user=user,
            paid_at=utc_now() - timedelta(hours=2),
        )
        db.session.add_all([older_exit, newer_exit])
        db.session.commit()

    response = client.get("/records?sort_by=exit&sort_dir=desc")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert body.index("Salida Nueva") < body.index("Salida Vieja")


def test_bootstrap_updates_weekly_motorcycle_tariff_to_six_days():
    app, client = build_client()
    login(client)

    with app.app_context():
        tarifa = Tariff.query.filter_by(vehicle_type="Moto").first()
        tarifa.offer_trigger_units = 7
        tarifa.offer_price = 70
        tarifa.notes = "Valor viejo"
        db.session.commit()

        from app.models import seed_defaults

        seed_defaults()
        db.session.refresh(tarifa)

        assert tarifa.offer_trigger_units == 6
        assert float(tarifa.offer_price) == 40.0
        assert "6 dias" in tarifa.notes


def test_bootstrap_normalizes_open_weekly_records_to_six_days():
    app, client = build_client()
    login(client)

    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        record = VehicleRecord(
            ticket_number="SEM-LEGADO",
            physical_ticket_number="SEM-LEGADO",
            client_name="Semana Legacy",
            vehicle_type="Moto",
            plate_number="LEG-006",
            stay_mode="weekly",
            contracted_days=7,
            entry_at=utc_now() - timedelta(days=5),
            paid_at=utc_now(),
            total_amount=60,
            status="Dentro del estacionamiento",
            entry_user=user,
            payment_user=user,
        )
        db.session.add(record)
        db.session.commit()

        from app.models import seed_defaults

        seed_defaults()
        db.session.refresh(record)

        assert record.contracted_days == 6
        assert float(record.total_amount) == 40.0
        assert "Semana completa" in (record.applied_rate_label or "")


def test_runtime_migration_normalizes_paid_weekly_records_to_six_days():
    app, client = build_client()
    login(client)

    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        tarifa = Tariff.query.filter_by(vehicle_type="Moto").first()
        tarifa.offer_trigger_units = 7
        tarifa.offer_price = 70
        record = VehicleRecord(
            ticket_number="SEM-MIGRACION",
            physical_ticket_number="SEM-MIGRACION",
            client_name="Semana Migracion",
            vehicle_type="Moto",
            plate_number="MIG-006",
            stay_mode="weekly",
            contracted_days=7,
            entry_at=utc_now() - timedelta(days=5),
            paid_at=utc_now(),
            total_amount=70,
            status="Salida registrada",
            entry_user=user,
            payment_user=user,
        )
        db.session.add(record)
        db.session.commit()

        from app.models import apply_runtime_migrations

        apply_runtime_migrations()
        db.session.refresh(record)
        db.session.refresh(tarifa)

        assert tarifa.offer_trigger_units == 6
        assert float(tarifa.offer_price) == 40.0
        assert record.contracted_days == 6
        assert float(record.total_amount) == 40.0
        assert "Semana completa" in (record.applied_rate_label or "")


def test_runtime_migration_normalizes_closed_paid_weekly_records_to_six_days():
    app, client = build_client()
    login(client)

    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        record = VehicleRecord(
            ticket_number="SEM-CERRADO",
            physical_ticket_number="SEM-CERRADO",
            client_name="Semana Cerrada",
            vehicle_type="Moto",
            plate_number="CER-006",
            stay_mode="weekly",
            contracted_days=7,
            entry_at=utc_now() - timedelta(days=2),
            exit_at=utc_now() - timedelta(days=1),
            paid_at=utc_now() - timedelta(days=1),
            duration_seconds=86400,
            total_amount=70,
            status="Pagado",
            entry_user=user,
            exit_user=user,
            payment_user=user,
        )
        db.session.add(record)
        db.session.commit()

        from app.models import apply_runtime_migrations

        apply_runtime_migrations()
        db.session.refresh(record)

        assert record.contracted_days == 6
        assert float(record.total_amount) == 40.0
        assert "Semana completa" in (record.applied_rate_label or "")


def test_weekly_charge_stays_at_six_day_package_after_seven_calendar_days():
    app, client = build_client()
    login(client)

    with app.app_context():
        from app.pricing import calculate_charge

        pricing = calculate_charge(
            "Moto",
            utc_now() - timedelta(days=7),
            utc_now(),
            stay_mode="weekly",
            contracted_days=6,
        )

        assert pricing["billing_units"] == 6
        assert float(pricing["total"]) == 40.0


def test_street_cart_always_uses_ten_peso_flat_rate():
    app, _client = build_client()

    with app.app_context():
        from app.pricing import calculate_charge

        tariff = Tariff.query.filter_by(vehicle_type="Carrito callejero").first()
        assert tariff.billing_scheme == "flat"
        assert float(tariff.rate_amount) == 10.0
        assert tariff.offer_label is None
        assert tariff.offer_trigger_units is None
        assert tariff.offer_price is None

        hourly_pricing = calculate_charge(
            "Carrito callejero",
            utc_now() - timedelta(hours=8),
            utc_now(),
            stay_mode="hourly",
        )
        weekly_pricing = calculate_charge(
            "Carrito callejero",
            utc_now() - timedelta(days=12),
            utc_now(),
            stay_mode="weekly",
            contracted_days=6,
        )

        assert float(hourly_pricing["total"]) == 10.0
        assert float(weekly_pricing["total"]) == 10.0
        assert "Tarifa fija" in hourly_pricing["rate_label"]


def test_bootstrap_normalizes_legacy_weekly_vehicle_type_alias():
    app, client = build_client()
    login(client)

    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        legacy_tariff = Tariff(
            vehicle_type="moto por semana",
            billing_scheme="daily",
            rate_amount=10,
            period_unit="day",
            min_charge_units=1,
            offer_label="Semana vieja",
            offer_trigger_units=7,
            offer_price=70,
            notes="Tarifa anterior",
            active=True,
        )
        db.session.add(legacy_tariff)
        db.session.flush()

        record = VehicleRecord(
            ticket_number="SEM-ALIAS",
            physical_ticket_number="SEM-ALIAS",
            client_name="Semana Alias",
            vehicle_type="moto por semana",
            plate_number="ALI-006",
            stay_mode="weekly",
            contracted_days=7,
            entry_at=utc_now() - timedelta(days=5),
            paid_at=utc_now(),
            total_amount=70,
            status="Dentro del estacionamiento",
            entry_user=user,
            payment_user=user,
        )
        db.session.add(record)
        db.session.commit()

        from app.models import seed_defaults

        seed_defaults()
        db.session.refresh(record)
        db.session.refresh(legacy_tariff)

        assert record.vehicle_type == "Moto"
        assert record.contracted_days == 6
        assert float(record.total_amount) == 40.0
        assert legacy_tariff.active is False


def test_create_vehicle_record_normalizes_legacy_vehicle_alias():
    app, client = build_client()
    login(client)

    with app.app_context():
        if not Tariff.query.filter_by(vehicle_type="moto por semana").first():
            db.session.add(
                Tariff(
                    vehicle_type="moto por semana",
                    billing_scheme="daily",
                    rate_amount=10,
                    period_unit="day",
                    min_charge_units=1,
                    offer_label="Semana vieja",
                    offer_trigger_units=7,
                    offer_price=70,
                    notes="Tarifa anterior",
                    active=True,
                )
            )
            db.session.commit()

    response = client.post(
        "/records/new",
        data={
            "ticket_number": "ALIAS-NEW",
            "client_name": "Alias Nuevo",
            "vehicle_type": "moto por semana",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "ALI-777",
            "notes": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="ALIAS-NEW").first()
        assert record.vehicle_type == "Moto"
        assert record.contracted_days == 6


def test_weekly_record_tracks_partial_exits():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SEM",
            "client_name": "Cliente Semana",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "SEM-123",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-SEM").first()
        record_id = record.id

    pay_response = client.post(f"/records/{record_id}/pay", follow_redirects=True)
    first_exit = client.post(f"/records/{record_id}/weekly-exit", follow_redirects=True)
    blocked_second_exit = client.post(f"/records/{record_id}/weekly-exit", follow_redirects=True)
    daily_entry = client.post(f"/records/{record_id}/weekly-entry", follow_redirects=True)
    second_exit = client.post(f"/records/{record_id}/weekly-exit", follow_redirects=True)
    final_close = client.post(f"/records/{record_id}/exit", follow_redirects=True)

    assert pay_response.status_code == 200
    assert first_exit.status_code == 200
    assert blocked_second_exit.status_code == 200
    assert "Primero registra la entrada del dia" in blocked_second_exit.get_data(as_text=True)
    assert daily_entry.status_code == 200
    assert second_exit.status_code == 200
    assert final_close.status_code == 200

    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.weekly_exit_count == 2
        assert updated.latest_weekly_exit_at is not None
        assert updated.exit_at is not None
        assert updated.status == "Pagado"


def test_weekly_partial_exit_does_not_require_payment_but_final_exit_does():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SEM-SIN-PAGO",
            "client_name": "Cliente Semana Sin Pago",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "SNP-123",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-SEM-SIN-PAGO").first()
        record_id = record.id

    partial_exit = client.post(f"/records/{record_id}/weekly-exit", follow_redirects=True)
    blocked_final_exit = client.post(f"/records/{record_id}/exit", follow_redirects=True)

    assert partial_exit.status_code == 200
    assert "Salida semanal registrada correctamente." in partial_exit.get_data(as_text=True)
    assert blocked_final_exit.status_code == 200
    assert "Primero registra el pago antes de marcar la salida." in blocked_final_exit.get_data(as_text=True)

    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.is_paid is False
        assert updated.exit_at is None
        assert updated.status == "Salida registrada"
        assert updated.weekly_exit_count == 1

    client.post(f"/records/{record_id}/weekly-entry", follow_redirects=True)
    client.post(f"/records/{record_id}/pay", follow_redirects=True)
    final_exit = client.post(f"/records/{record_id}/exit", follow_redirects=True)

    assert final_exit.status_code == 200

    with app.app_context():
        closed = db.session.get(VehicleRecord, record_id)
        assert closed.is_paid is True
        assert closed.exit_at is not None
        assert closed.status == "Pagado"


def test_weekly_partial_exit_hides_record_until_daily_entry():
    app, client = build_client()
    login(client, "empleado1", "EmpleadoUno2026!")

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SEM-ENTRA",
            "client_name": "Cliente Reingreso",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "REE-101",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-SEM-ENTRA").first()
        record_id = record.id

    client.post(f"/records/{record_id}/pay", follow_redirects=True)
    client.post(f"/records/{record_id}/weekly-exit", follow_redirects=True)

    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.status == "Salida registrada"

    hidden_response = client.get("/records")
    hidden_body = hidden_response.get_data(as_text=True)
    assert "Entrada del dia" in hidden_body

    client.post(f"/records/{record_id}/weekly-entry", follow_redirects=True)
    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.status == "Dentro del estacionamiento"
        assert updated.weekly_exit_count == 1
        assert updated.weekly_entry_count == 1


def test_employee_search_finds_weekly_partial_exit_from_previous_day():
    app, client = build_client()
    login(client, "empleado1", "EmpleadoUno2026!")

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SEM-BUSCA",
            "client_name": "Cliente Semana Busqueda",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "BUS-101",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-SEM-BUSCA").first()
        record.entry_at = utc_now() - timedelta(days=1)
        db.session.commit()
        record_id = record.id

    client.post(f"/records/{record_id}/pay", follow_redirects=True)
    client.post(f"/records/{record_id}/weekly-exit", follow_redirects=True)

    response = client.get("/records?search=FICHA-SEM-BUSCA")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Semana Busqueda" in body
    assert "Salida registrada" in body
    assert "Entrada del dia" in body


def test_employee_can_register_exit_edit_and_pay():
    app, client = build_client()
    login(client)
    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-02",
            "client_name": "Cliente Salida",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "SAL-001",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-02").first()
        record_id = record.id

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")
    edit_response = client.post(
        f"/records/{record_id}/update",
        data={
            "ticket_number": "FICHA-02",
            "client_name": "Cliente Editado",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "SAL-001",
            "status": "Dentro del estacionamiento",
            "notes": "Actualizado por empleado",
        },
        follow_redirects=True,
    )
    assert edit_response.status_code == 200
    blocked_exit = client.post(f"/records/{record_id}/exit", follow_redirects=True)
    assert blocked_exit.status_code == 200
    assert "Primero registra el pago antes de marcar la salida." in blocked_exit.get_data(as_text=True)
    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.exit_at is None

    pay_response = client.post(f"/records/{record_id}/pay", follow_redirects=True)
    assert pay_response.status_code == 200
    exit_response = client.post(f"/records/{record_id}/exit", follow_redirects=True)
    assert exit_response.status_code == 200

    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.client_name == "Cliente Editado"
        assert updated.exit_user.username == "empleado1"
        assert updated.status == "Pagado"


def test_employee_sees_exit_and_pay_actions_but_not_delete():
    app, client = build_client()
    login(client)
    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-EMP-01",
            "client_name": "Cliente Empleado",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "EMP-001",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(
            physical_ticket_number="FICHA-EMP-01"
        ).first()
        admin_user = User.query.filter_by(username="admin").first()
        record.paid_at = record.entry_at
        record.payment_user = admin_user
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")
    response = client.get("/records")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Pago registrado" in body
    assert "Registrar salida" in body
    assert "Vehiculos actuales y movimientos del dia" in body
    assert "Eliminar" not in body


def test_employee_two_can_delete_cash_movements_not_vehicle_records():
    app, client = build_client()
    login(client)
    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-EMP-02-DEL",
            "client_name": "Cliente Borra Empleado 2",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "EMP-002",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(
            physical_ticket_number="FICHA-EMP-02-DEL"
        ).first()
        record_id = record.id

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")
    client.post(
        "/cash-movements/new",
        data={
            "movement_type": "income",
            "description": "Entrada para borrar",
            "amount": "50",
        },
        follow_redirects=True,
    )

    with app.app_context():
        movement = CashMovement.query.filter_by(description="Entrada para borrar").first()
        movement_id = movement.id

    blocked_movement_response = client.post(f"/cash-movements/{movement_id}/delete")
    assert blocked_movement_response.status_code == 403

    client.post("/logout", follow_redirects=True)
    login(client, "empleado2", "EmpleadoDos2026!")
    blocked_record_response = client.post(f"/records/{record_id}/delete")
    assert blocked_record_response.status_code == 403

    records_response = client.get("/records")
    records_body = records_response.get_data(as_text=True)
    assert records_response.status_code == 200
    assert "Cliente Borra Empleado 2" in records_body
    assert "Eliminar" not in records_body

    dashboard_response = client.get("/")
    dashboard_body = dashboard_response.get_data(as_text=True)
    assert dashboard_response.status_code == 200
    assert "Entrada para borrar" in dashboard_body
    assert f'action="/cash-movements/{movement_id}/delete"' in dashboard_body

    delete_response = client.post(
        f"/cash-movements/{movement_id}/delete",
        headers={"Referer": "/"},
        follow_redirects=True,
    )
    assert delete_response.status_code == 200
    assert "Movimiento de caja eliminado correctamente." in delete_response.get_data(as_text=True)

    with app.app_context():
        assert db.session.get(VehicleRecord, record_id) is not None
        assert db.session.get(CashMovement, movement_id) is None


def test_employee_zero_two_can_delete_cash_movements():
    app, client = build_client()
    login(client)

    with app.app_context():
        employee_role = Role.query.filter_by(name="employee").first()
        user = User(
            full_name="Empleado 02",
            username="empleado02",
            role=employee_role,
        )
        user.set_password("EmpleadoDos2026!")
        db.session.add(user)
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login(client, "empleado02", "EmpleadoDos2026!")
    client.post(
        "/cash-movements/new",
        data={
            "movement_type": "expense",
            "description": "Gasto empleado 02",
            "amount": "15",
        },
        follow_redirects=True,
    )

    with app.app_context():
        movement = CashMovement.query.filter_by(description="Gasto empleado 02").first()
        movement_id = movement.id

    dashboard_response = client.get("/")
    dashboard_body = dashboard_response.get_data(as_text=True)
    assert dashboard_response.status_code == 200
    assert "Pendiente por cobrar" in dashboard_body
    assert f'action="/cash-movements/{movement_id}/delete"' in dashboard_body

    delete_response = client.post(
        f"/cash-movements/{movement_id}/delete",
        headers={"Referer": "/"},
        follow_redirects=True,
    )
    assert delete_response.status_code == 200
    assert "Movimiento de caja eliminado correctamente." in delete_response.get_data(as_text=True)

    with app.app_context():
        assert db.session.get(CashMovement, movement_id) is None


def test_employee_zero_two_can_delete_vehicle_records():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-EMP-02-TICKET",
            "client_name": "Cliente Ticket Empleado 02",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "E02-DEL",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        employee_role = Role.query.filter_by(name="employee").first()
        user = User(
            full_name="Sergio A",
            username="empleado02",
            role=employee_role,
        )
        user.set_password("EmpleadoDos2026!")
        db.session.add(user)
        record = VehicleRecord.query.filter_by(
            physical_ticket_number="FICHA-EMP-02-TICKET"
        ).first()
        record_id = record.id
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login(client, "empleado02", "EmpleadoDos2026!")
    records_response = client.get("/records")
    records_body = records_response.get_data(as_text=True)
    assert records_response.status_code == 200
    assert "Cliente Ticket Empleado 02" in records_body
    assert f'action="/records/{record_id}/delete"' in records_body

    delete_response = client.post(f"/records/{record_id}/delete", follow_redirects=True)
    assert delete_response.status_code == 200
    assert "Registro eliminado correctamente." in delete_response.get_data(as_text=True)

    with app.app_context():
        assert db.session.get(VehicleRecord, record_id) is None


def test_sergio_a_has_admin_home_and_management_sections():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SERGIO-PENDIENTE",
            "client_name": "Cliente Pendiente Sergio",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "SER-PEN",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        employee_role = Role.query.filter_by(name="employee").first()
        user = User(full_name="Sergio A", username="empleado02", role=employee_role)
        user.set_password("EmpleadoDos2026!")
        db.session.add(user)
        admin = User.query.filter_by(username="admin").first()
        empleado1 = User.query.filter_by(username="empleado1").first()
        empleado2 = User.query.filter_by(username="empleado2").first()
        paid_at = utc_now()
        db.session.add_all(
            [
                VehicleRecord(
                    ticket_number="REC-SERGIO-COBRO-1",
                    physical_ticket_number="SER-COBRO-1",
                    client_name="Cobro Sergio Vista Uno",
                    vehicle_type="Moto",
                    plate_number="SCO-1",
                    entry_at=paid_at - timedelta(hours=1),
                    paid_at=paid_at,
                    total_amount=30,
                    status="Dentro del estacionamiento",
                    entry_user=admin,
                    payment_user=empleado1,
                ),
                VehicleRecord(
                    ticket_number="REC-SERGIO-COBRO-2",
                    physical_ticket_number="SER-COBRO-2",
                    client_name="Cobro Sergio Vista Dos",
                    vehicle_type="Moto",
                    plate_number="SCO-2",
                    entry_at=paid_at - timedelta(hours=1),
                    paid_at=paid_at,
                    total_amount=80,
                    status="Dentro del estacionamiento",
                    entry_user=admin,
                    payment_user=empleado2,
                ),
                CashMovement(
                    movement_type="expense",
                    description="Gasto Sergio vista uno",
                    amount=5,
                    occurred_at=paid_at,
                    created_by=empleado1,
                ),
                CashMovement(
                    movement_type="income",
                    description="Entrada Sergio vista dos",
                    amount=20,
                    occurred_at=paid_at,
                    created_by=empleado2,
                ),
                CashMovement(
                    movement_type="expense",
                    description="Gasto Sergio vista dos",
                    amount=10,
                    occurred_at=paid_at,
                    created_by=empleado2,
                ),
            ]
        )
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login_response = login(client, "empleado02", "EmpleadoDos2026!")
    home_body = login_response.get_data(as_text=True)

    assert login_response.status_code == 200
    assert "Pendiente por cobrar" in home_body
    assert "Por cobrar" in home_body
    assert "$10.00" in home_body
    assert "Tarifas" in home_body
    assert "Empleados" in home_body
    assert "Cobros por usuario" in home_body
    assert "Empleado 1" in home_body
    assert "$25.00" in home_body
    assert "$30.00" in home_body
    assert "Gastos $5.00" in home_body
    assert "Empleado 2" in home_body
    assert "$90.00" in home_body
    assert "$80.00" in home_body
    assert "Otros ingresos $20.00" in home_body
    assert "Gastos $10.00" in home_body
    assert "Movimientos de caja" in home_body
    assert "employee-app-shell" in home_body

    tariffs_response = client.get("/tariffs")
    employees_response = client.get("/employees")
    assert tariffs_response.status_code == 200
    assert employees_response.status_code == 200
    assert "Tarifas y tipos" in tariffs_response.get_data(as_text=True)
    assert "Empleados" in employees_response.get_data(as_text=True)


def test_employee_zero_two_can_edit_vehicle_entry_date_and_filters_update():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-FECHA-E02",
            "client_name": "Cliente Fecha Sergio",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "FEC-E02",
            "notes": "",
        },
        follow_redirects=True,
    )

    local_tz = ZoneInfo("America/Mexico_City")
    target_local = datetime(2026, 1, 15, 9, 30, tzinfo=local_tz)
    with app.app_context():
        employee_role = Role.query.filter_by(name="employee").first()
        user = User(full_name="Sergio A", username="empleado02", role=employee_role)
        user.set_password("EmpleadoDos2026!")
        db.session.add(user)
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-FECHA-E02").first()
        record_id = record.id
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login(client, "empleado02", "EmpleadoDos2026!")
    records_response = client.get("/records")
    records_body = records_response.get_data(as_text=True)
    assert records_response.status_code == 200
    assert 'name="entry_at_local"' in records_body

    response = client.post(
        f"/records/{record_id}/update",
        data={
            "ticket_number": "FICHA-FECHA-E02",
            "client_name": "Cliente Fecha Sergio",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "FEC-E02",
            "entry_at_local": target_local.strftime("%Y-%m-%dT%H:%M"),
            "status": "Dentro del estacionamiento",
            "notes": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.entry_at == target_local.astimezone(timezone.utc).replace(tzinfo=None)

    target_day = target_local.date().isoformat()
    filtered_response = client.get(f"/records?date={target_day}&date_field=entry")
    filtered_body = filtered_response.get_data(as_text=True)
    assert filtered_response.status_code == 200
    assert "Cliente Fecha Sergio" in filtered_body

    today = utc_now().astimezone(local_tz).date().isoformat()
    today_response = client.get(f"/records?date={today}&date_field=entry")
    today_body = today_response.get_data(as_text=True)
    assert today_response.status_code == 200
    assert "Cliente Fecha Sergio" not in today_body


def test_regular_employee_cannot_edit_vehicle_entry_date():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-FECHA-BLOCK",
            "client_name": "Cliente Fecha Bloqueada",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "FEC-BLQ",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-FECHA-BLOCK").first()
        record_id = record.id
        original_entry_at = record.entry_at

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")
    records_response = client.get("/records")
    records_body = records_response.get_data(as_text=True)
    assert records_response.status_code == 200
    assert 'name="entry_at_local"' not in records_body

    response = client.post(
        f"/records/{record_id}/update",
        data={
            "ticket_number": "FICHA-FECHA-BLOCK",
            "client_name": "Cliente Fecha Bloqueada",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "FEC-BLQ",
            "entry_at_local": "2026-01-15T09:30",
            "status": "Dentro del estacionamiento",
            "notes": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.entry_at == original_entry_at


def test_employee_zero_two_entry_date_change_recalculates_paid_total():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-FECHA-PAGO",
            "client_name": "Cliente Fecha Pago",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "FEC-PAG",
            "notes": "",
        },
        follow_redirects=True,
    )

    local_tz = ZoneInfo("America/Mexico_City")
    target_local = datetime(2026, 1, 10, 8, 0, tzinfo=local_tz)
    paid_local = datetime(2026, 1, 12, 8, 0, tzinfo=local_tz)
    with app.app_context():
        employee_role = Role.query.filter_by(name="employee").first()
        user = User(full_name="Sergio A", username="empleado02", role=employee_role)
        user.set_password("EmpleadoDos2026!")
        db.session.add(user)
        admin_user = User.query.filter_by(username="admin").first()
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-FECHA-PAGO").first()
        record.paid_at = paid_local.astimezone(timezone.utc)
        record.payment_user = admin_user
        record.status = "Dentro del estacionamiento"
        record.total_amount = 10
        record_id = record.id
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login(client, "empleado02", "EmpleadoDos2026!")
    response = client.post(
        f"/records/{record_id}/update",
        data={
            "ticket_number": "FICHA-FECHA-PAGO",
            "client_name": "Cliente Fecha Pago",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "FEC-PAG",
            "entry_at_local": target_local.strftime("%Y-%m-%dT%H:%M"),
            "status": "Dentro del estacionamiento",
            "notes": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.entry_at == target_local.astimezone(timezone.utc).replace(tzinfo=None)
        assert updated.duration_seconds == 172800
        assert float(updated.total_amount) == 20.0


def test_records_default_view_shows_inside_and_pending_checkout_panel():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "010",
            "client_name": "Cliente Dentro",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "DNT-010",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "011",
            "client_name": "Cliente Salio",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "SAL-011",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        exited_record = VehicleRecord.query.filter_by(physical_ticket_number="011").first()
        exited_id = exited_record.id

    client.post(f"/records/{exited_id}/pay", follow_redirects=True)
    response = client.get("/records")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Historial de registros" in body
    assert "Cliente Dentro" in body
    assert "Cliente Salio" in body
    assert "Pago registrado" in body


def test_employee_paid_filter_only_shows_paid_records():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "020",
            "client_name": "Cliente Pagado",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "PAG-020",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "021",
            "client_name": "Cliente Pendiente",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "PEN-021",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        paid_record = VehicleRecord.query.filter_by(physical_ticket_number="020").first()
        paid_id = paid_record.id

    client.post(f"/records/{paid_id}/pay", follow_redirects=True)
    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    response = client.get("/records?status=Pagado")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Pagado" in body
    assert "Cliente Pendiente" not in body


def test_employee_records_filters_hide_active_cards_section():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "030",
            "client_name": "Cliente Dentro Filtro",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "DEN-030",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "031",
            "client_name": "Cliente Pago Filtro",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "PAG-031",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        inside_record = VehicleRecord.query.filter_by(physical_ticket_number="030").first()
        inside_record.entry_at = utc_now() - timedelta(days=1)
        paid_record = VehicleRecord.query.filter_by(physical_ticket_number="031").first()
        paid_id = paid_record.id
        db.session.commit()

    client.post(f"/records/{paid_id}/pay", follow_redirects=True)
    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    inside_response = client.get("/records?status=Dentro+del+estacionamiento&date=")
    inside_body = inside_response.get_data(as_text=True)
    assert inside_response.status_code == 200
    assert "Fichas activas" not in inside_body
    assert "Cliente Dentro Filtro" in inside_body

    today = utc_now().astimezone(ZoneInfo("America/Mexico_City")).date().isoformat()
    paid_response = client.get(f"/records?status=Pagado&date={today}&date_field=payment")
    paid_body = paid_response.get_data(as_text=True)
    assert paid_response.status_code == 200
    assert "Fichas activas" not in paid_body
    assert "Cliente Pago Filtro" in paid_body
    assert "Cliente Dentro Filtro" not in paid_body


def test_employee_records_can_filter_by_vehicle_type():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FILTRO-MOTO",
            "client_name": "Cliente Tipo Moto",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "TIP-MOT",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "FILTRO-BICI",
            "client_name": "Cliente Tipo Bici",
            "vehicle_type": "Bicicleta",
            "stay_mode": "hourly",
            "plate_number": "TIP-BIC",
            "notes": "",
        },
        follow_redirects=True,
    )

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    response = client.get("/records?vehicle_type=Bicicleta&date=")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Tipo de vehiculo" in body
    assert '<option value="Bicicleta" selected>Bicicleta</option>' in body
    assert "Cliente Tipo Bici" in body
    assert "Cliente Tipo Moto" not in body


def test_employee_records_can_filter_by_stay_mode():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FILTRO-DIA",
            "client_name": "Cliente Cobro Dia",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "MOD-DIA",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "FILTRO-SEMANA",
            "client_name": "Cliente Cobro Semana",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "MOD-SEM",
            "notes": "",
        },
        follow_redirects=True,
    )

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    response = client.get("/records?stay_mode=weekly&date=")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Modalidad de cobro" in body
    assert '<option value="weekly" selected>Por semana</option>' in body
    assert "Cliente Cobro Semana" in body
    assert "Cliente Cobro Dia" not in body


def test_employee_inside_filter_ignores_date_for_current_vehicles():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FILTRO-DENTRO-SEMANAL",
            "client_name": "Cliente Semanal Dentro Fecha",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "DEN-FEC",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FILTRO-DENTRO-SEMANAL").first()
        record.entry_at = utc_now() - timedelta(days=5)
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    today = utc_now().astimezone(ZoneInfo("America/Mexico_City")).date().isoformat()
    response = client.get(
        "/records?status=Dentro+del+estacionamiento"
        f"&vehicle_type=Moto&stay_mode=weekly&date={today}"
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Semanal Dentro Fecha" in body
    assert "No hay movimientos hoy con ese filtro." not in body


def test_employee_weekly_cards_show_remaining_days():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-DIAS-REST",
            "client_name": "Cliente Dias Restantes",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "REST-006",
            "notes": "",
        },
        follow_redirects=True,
    )

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    response = client.get("/records?status=Dentro+del+estacionamiento&date=")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Dias Restantes" in body
    assert "Dias restantes: 5" in body


def test_employee_today_all_filter_shows_activity_and_active_records():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "TODOS-ACTIVA",
            "client_name": "Cliente Activo Ayer",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "ACT-AYER",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "TODOS-PAGO",
            "client_name": "Cliente Pago Hoy Todos",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "PAG-HOY",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "TODOS-SALIDA-DIA",
            "client_name": "Cliente Salida Dia Todos",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "SAL-DIA",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        active_record = VehicleRecord.query.filter_by(physical_ticket_number="TODOS-ACTIVA").first()
        paid_record = VehicleRecord.query.filter_by(physical_ticket_number="TODOS-PAGO").first()
        weekly_record = VehicleRecord.query.filter_by(physical_ticket_number="TODOS-SALIDA-DIA").first()
        active_record.entry_at = utc_now() - timedelta(days=1)
        paid_record.entry_at = utc_now() - timedelta(days=1)
        weekly_record.entry_at = utc_now() - timedelta(days=1)
        db.session.commit()
        paid_id = paid_record.id
        weekly_id = weekly_record.id

    client.post(f"/records/{paid_id}/pay", follow_redirects=True)
    client.post(f"/records/{weekly_id}/weekly-exit", follow_redirects=True)
    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    today = utc_now().astimezone(ZoneInfo("America/Mexico_City")).date().isoformat()
    response = client.get(f"/records?date={today}&status=")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'href="/records?date=' + today + '&amp;date_field=entry"' in body
    assert '>Entradas de hoy</a>' in body
    assert 'is-active" href="/records?date=' + today + '&amp;status=">Todos los de hoy</a>' in body
    assert 'is-active" href="/records?date=' + today + '&amp;date_field=entry">Entradas de hoy</a>' not in body
    assert "Cliente Activo Ayer" in body
    assert "Cliente Pago Hoy Todos" in body
    assert "Cliente Salida Dia Todos" in body


def test_employee_indecisive_weekly_card_shows_payment_and_exit_actions():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-INC-ACCIONES",
            "client_name": "Cliente Indeciso Acciones",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "INC-ACT",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-INC-ACCIONES").first()
        record_id = record.id

    client.post(f"/records/{record_id}/uncertain", follow_redirects=True)
    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    response = client.get("/records?status=Indeciso&date=")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Indeciso Acciones" in body
    assert body.count(f'action="/records/{record_id}/pay"') >= 2
    assert body.count(f'action="/records/{record_id}/exit"') >= 2
    assert body.count(f'action="/records/{record_id}/weekly-entry"') >= 2
    assert "Salida final" in body


def test_employee_search_finds_active_ticket_even_if_entry_was_previous_day():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "80",
            "client_name": "Cliente Ochenta",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "OCH-080",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="80").first()
        record.entry_at = utc_now() - timedelta(days=1)
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")

    response = client.get("/records?search=80")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Ochenta" in body


def test_search_uses_visible_ticket_number_instead_of_hidden_internal_code():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "14",
            "client_name": "Cliente Visible",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "VIS-014",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="14").first()
        hidden_suffix = record.ticket_number.split("-")[-1]

    response = client.get(f"/records?search={hidden_suffix}")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Visible" not in body

    visible_response = client.get("/records?search=14")
    visible_body = visible_response.get_data(as_text=True)
    assert "Cliente Visible" in visible_body


def test_search_ticket_number_is_exact_for_numeric_tickets():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "6",
            "client_name": "Cliente Seis",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "SEI-006",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "16",
            "client_name": "Cliente Dieciseis",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "DIE-016",
            "notes": "",
        },
        follow_redirects=True,
    )

    response = client.get("/records?search=6")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Seis" in body
    assert "Cliente Dieciseis" not in body


def test_weekly_records_default_view_shows_weekly_exit_action():
    app, client = build_client()
    login(client)
    client.post(
        "/records/new",
        data={
            "ticket_number": "012",
            "client_name": "Cliente Semana Activa",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "SEM-012",
            "notes": "",
        },
        follow_redirects=True,
    )

    response = client.get("/records")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Semana Activa" in body
    assert "Salida del dia" in body


def test_physical_ticket_can_be_reused_after_exit():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-03",
            "client_name": "Cliente Uno",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "AAA-111",
            "notes": "",
        },
        follow_redirects=True,
    )
    with app.app_context():
        first_record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-03").first()
        first_id = first_record.id

    client.post(f"/records/{first_id}/pay", follow_redirects=True)
    client.post(f"/records/{first_id}/exit", follow_redirects=True)

    second_response = client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-03",
            "client_name": "Cliente Dos",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "BBB-222",
            "notes": "",
        },
        follow_redirects=True,
    )
    assert second_response.status_code == 200

    with app.app_context():
        matches = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-03").all()
        assert len(matches) == 2


def test_database_blocks_duplicate_active_physical_ticket():
    app, _client = build_client()

    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        first = VehicleRecord(
            ticket_number="REC-DUP-A",
            physical_ticket_number="DUP-ACTIVA",
            client_name="Cliente Duplicado Uno",
            vehicle_type="Moto",
            plate_number="DUP-001",
            entry_user=user,
        )
        second = VehicleRecord(
            ticket_number="REC-DUP-B",
            physical_ticket_number="DUP-ACTIVA",
            client_name="Cliente Duplicado Dos",
            vehicle_type="Moto",
            plate_number="DUP-002",
            entry_user=user,
        )
        db.session.add(first)
        db.session.commit()
        db.session.add(second)
        try:
            db.session.commit()
            assert False, "La base debe impedir fichas activas duplicadas."
        except IntegrityError:
            db.session.rollback()


def test_bootstrap_users_exist():
    app, _client = build_client()
    with app.app_context():
        usernames = {user.username for user in User.query.all()}
    assert {"admin", "empleado1", "empleado2"}.issubset(usernames)


def test_create_employee_recovers_missing_employee_role():
    app, client = build_client()
    login(client)

    with app.app_context():
        from app.models import Role

        employee_role = Role.query.filter_by(name="employee").first()
        db.session.delete(employee_role)
        db.session.commit()

    response = client.post(
        "/users/new",
        data={
            "full_name": "Nuevo Operador",
            "username": "nuevooperador",
            "password": "ClaveSegura2026!",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Empleado creado correctamente:" in response.get_data(as_text=True)

    with app.app_context():
        user = User.query.filter_by(username="nuevooperador").first()
        assert user is not None
        assert user.role is not None
        assert user.role.name == "employee"
        assert user.password_reference == "ClaveSegura2026!"


def test_admin_can_edit_and_delete_employee():
    app, client = build_client()
    login(client)

    client.post(
        "/users/new",
        data={
            "full_name": "Empleado Editar",
            "username": "empleadoeditar",
            "password": "ClaveSegura2026!",
        },
        follow_redirects=True,
    )

    with app.app_context():
        user = User.query.filter_by(username="empleadoeditar").first()
        user_id = user.id

    edit_response = client.post(
        f"/users/{user_id}/edit",
        data={"full_name": "Empleado Editado", "username": "empleadoeditado"},
        follow_redirects=True,
    )
    assert edit_response.status_code == 200
    assert "Empleado actualizado correctamente:" in edit_response.get_data(as_text=True)

    with app.app_context():
        edited_user = db.session.get(User, user_id)
        assert edited_user.full_name == "Empleado Editado"
        assert edited_user.username == "empleadoeditado"

    delete_response = client.post(f"/users/{user_id}/delete", follow_redirects=True)
    assert delete_response.status_code == 200
    assert "Empleado eliminado correctamente." in delete_response.get_data(as_text=True)

    with app.app_context():
        assert db.session.get(User, user_id) is None


def test_admin_can_assign_employee_weekly_schedule():
    app, client = build_client()
    login(client)

    client.post(
        "/users/new",
        data={
            "full_name": "Empleado Horario",
            "username": "empleadohorario",
            "password": "ClaveSegura2026!",
        },
        follow_redirects=True,
    )

    with app.app_context():
        user = User.query.filter_by(username="empleadohorario").first()
        user_id = user.id

    page_response = client.get("/employees")
    page_body = page_response.get_data(as_text=True)
    assert page_response.status_code == 200
    assert "Horario de acceso" in page_body
    assert 'name="enabled_0"' in page_body

    schedule_response = client.post(
        f"/users/{user_id}/schedule",
        data={
            "enabled_0": "on",
            "start_0": "08:00",
            "end_0": "16:00",
            "start_1": "08:00",
            "end_1": "16:00",
            "start_2": "08:00",
            "end_2": "16:00",
            "start_3": "08:00",
            "end_3": "16:00",
            "start_4": "08:00",
            "end_4": "16:00",
            "start_5": "08:00",
            "end_5": "16:00",
            "start_6": "08:00",
            "end_6": "16:00",
        },
        follow_redirects=True,
    )
    assert schedule_response.status_code == 200
    assert "Horario actualizado correctamente" in schedule_response.get_data(as_text=True)

    with app.app_context():
        monday = EmployeeSchedule.query.filter_by(user_id=user_id, weekday=0).first()
        assert monday is not None
        assert monday.is_enabled is True
        assert monday.start_time.strftime("%H:%M") == "08:00"
        assert monday.end_time.strftime("%H:%M") == "16:00"


def test_employee_login_is_blocked_outside_assigned_schedule():
    app, client = build_client()
    login(client)

    with app.app_context():
        user = User.query.filter_by(username="empleado1").first()
        tz = ZoneInfo("America/Mexico_City")
        today = utc_now().astimezone(tz).weekday()
        db.session.add(
            EmployeeSchedule(
                user=user,
                weekday=today,
                is_enabled=True,
                start_time=datetime.strptime("00:00", "%H:%M").time(),
                end_time=datetime.strptime("00:01", "%H:%M").time(),
            )
        )
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    response = login(client, "empleado1", "EmpleadoUno2026!")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Tu cuenta solo puede usarse dentro del horario asignado." in body
    assert "Bitacora de estacionamiento" not in body


def test_employee_login_allowed_inside_assigned_schedule():
    app, client = build_client()
    login(client)

    with app.app_context():
        user = User.query.filter_by(username="empleado1").first()
        tz = ZoneInfo("America/Mexico_City")
        today = utc_now().astimezone(tz).weekday()
        db.session.add(
            EmployeeSchedule(
                user=user,
                weekday=today,
                is_enabled=True,
                start_time=datetime.strptime("00:00", "%H:%M").time(),
                end_time=datetime.strptime("23:59", "%H:%M").time(),
            )
        )
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    response = login(client, "empleado1", "EmpleadoUno2026!")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Bitacora de estacionamiento" in body


def test_help_page_and_ticket_reprint_route_exist():
    app, client = build_client()
    login(client)
    help_response = client.get("/help")
    assert help_response.status_code == 200
    assert "Preguntas frecuentes" in help_response.get_data(as_text=True)
    assert "Administrador" in help_response.get_data(as_text=True)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-04",
            "client_name": "Cliente Ticket",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "XYZ-999",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-04").first()
        record_id = record.id

    response = client.get(f"/records/{record_id}/ticket")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Descargar PDF" in body
    assert "FICHA-04" in body
    assert "Codigo QR del ticket" in body

    pdf_response = client.get(f"/records/{record_id}/ticket/document")
    assert pdf_response.status_code == 200
    assert pdf_response.mimetype == "application/pdf"

    qr_response = client.get(f"/records/{record_id}/ticket/qr.svg")
    assert qr_response.status_code == 200
    assert qr_response.mimetype == "image/svg+xml"
    assert "<svg" in qr_response.get_data(as_text=True)


def test_employee_help_page_is_role_specific():
    _app, client = build_client()
    login(client, "empleado1", "EmpleadoUno2026!")
    response = client.get("/help")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "<h3>Empleado</h3>" in body
    assert "<h3>Administrador</h3>" not in body


def test_employee_dashboard_shows_simple_core_actions():
    _app, client = build_client()
    login(client, "empleado1", "EmpleadoUno2026!")
    response = client.get("/")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Bitacora de estacionamiento" in body
    assert "Vehiculos dentro ahora" in body
    assert "Vehiculos de hoy" in body
    assert "Dentro ahora" in body
    assert "Ya pagaron" in body
    assert "No pagados" in body
    assert "employee-record-grid dashboard-record-grid" in body
    assert "employee-board-table" not in body
    assert "Vista rapida de los vehiculos que siguen dentro" not in body
    assert "Abrir vista operativa" not in body
    assert "Modo oscuro" not in body
    assert "data-theme-toggle" not in body


def test_employee_dashboard_can_reopen_ticket_after_entry():
    _app, client = build_client()
    login(client, "empleado1", "EmpleadoUno2026!")

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-EMP-TICKET",
            "client_name": "Cliente Ticket Empleado",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "TIC-101",
            "notes": "",
        },
        follow_redirects=True,
    )

    response = client.get("/")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente Ticket Empleado" in body
    assert "Ticket PDF" in body


def test_employee_dashboard_unpaid_view_includes_previous_day_inside_records():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-NO-PAGO-AYER",
            "client_name": "Cliente No Pago Ayer",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "NPA-001",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-PAGO-AYER",
            "client_name": "Cliente Pago Ayer",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "PAG-AYER",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        unpaid = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-NO-PAGO-AYER").first()
        paid = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-PAGO-AYER").first()
        unpaid.entry_at = utc_now() - timedelta(days=1)
        paid.entry_at = utc_now() - timedelta(days=1)
        paid.paid_at = utc_now() - timedelta(days=1)
        paid.status = "Dentro del estacionamiento"
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")
    response = client.get("/?view=no_pagados")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cliente No Pago Ayer" in body
    assert "Cliente Pago Ayer" not in body
    assert "No pagados dentro" in body


def test_operations_page_uses_touch_friendly_copy():
    _app, client = build_client()
    login(client, "empleado1", "EmpleadoUno2026!")
    response = client.get("/operations")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Captura un vehiculo sin pasos complicados" in body
    assert "Registrar vehiculo" in body
    assert "Ficha fisica" in body


def test_ticket_board_page_exists_and_shows_records():
    app, client = build_client()
    login(client)
    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-TAB-01",
            "client_name": "Cliente Tablero",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "TAB-001",
            "notes": "",
        },
        follow_redirects=True,
    )

    response = client.get("/tickets-board")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Tablero de fichas" in body
    assert "001" in body
    assert "100" in body
    assert "Adentro del estacionamiento" in body
    assert "Pendientes por salir" not in body
    assert 'data-open-dialog="ticket-slot-dialog-1"' in body
    assert "Cliente Tablero" in body
    assert "TAB-001" in body
    assert "Abrir en bitacora" in body
    assert "Ficha disponible" in body


def test_ticket_board_metrics_count_all_inside_ticketed_records():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-MET-05",
            "client_name": "Cliente Ayer",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "MET-005",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-MET-06",
            "client_name": "Cliente Semanal Sin Pago",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "MET-006",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        for record in VehicleRecord.query.filter(
            VehicleRecord.physical_ticket_number.in_(["FICHA-MET-05", "FICHA-MET-06"])
        ):
            record.entry_at = utc_now() - timedelta(days=1)
        db.session.commit()

        _records, metrics = build_ticket_board()

    assert metrics["available"] == 98
    assert metrics["occupied"] == 2
    assert metrics["inside"] == 2
    assert metrics["weekly_inside"] == 1

    response = client.get("/tickets-board")
    body = response.get_data(as_text=True)
    assert "Fichas disponibles" in body
    assert "Vehiculos adentro con ficha" in body
    assert "Semanales adentro" in body
    assert "Por hora" in body
    assert "Por semana" in body
    assert ">Moto</span>" not in body
    assert "ticket-combo-hourly-inside" in body
    assert "ticket-combo-weekly-inside" in body
    assert "ticket-stripes-2" in body


def test_ticket_board_marks_paid_weekly_inside_as_inside_color():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-SEM-07",
            "client_name": "Cliente Semanal Pagado",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "SEM-007",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-SEM-07").first()
        client.post(f"/records/{record.id}/pay", follow_redirects=True)

        board_records, metrics = build_ticket_board()
        weekly_slot = next(slot for slot in board_records if slot["slot_number"] == 7)

    assert weekly_slot["status_theme"] == "inside"
    assert weekly_slot["color_signature"] == "weekly-inside"
    assert weekly_slot["color_label"] == "Por semana + Adentro del estacionamiento"
    assert weekly_slot["color_count"] == 2
    assert metrics["inside"] == 1
    assert metrics["weekly_inside"] == 1

    response = client.get("/tickets-board")
    body = response.get_data(as_text=True)
    assert "Pendientes por salir" not in body
    assert "ticket-combo-weekly-inside" in body


def test_ticket_board_marks_weekly_motorcycle_day_exit_with_dedicated_color():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-MOTO-SALIO-08",
            "client_name": "Moto Semanal Salida",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "MOTO-008",
            "notes": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-BICI-SALIO-09",
            "client_name": "Bici Semanal Salida",
            "vehicle_type": "Bicicleta",
            "stay_mode": "weekly",
            "contracted_days": "6",
            "plate_number": "BICI-009",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        moto = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-MOTO-SALIO-08").first()
        bici = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-BICI-SALIO-09").first()

    client.post(f"/records/{moto.id}/weekly-exit", follow_redirects=True)
    client.post(f"/records/{bici.id}/weekly-exit", follow_redirects=True)

    with app.app_context():
        board_records, _metrics = build_ticket_board()
        moto_slot = next(slot for slot in board_records if slot["slot_number"] == 8)
        bici_slot = next(slot for slot in board_records if slot["slot_number"] == 9)

    assert moto_slot["status_theme"] == "weekly-out"
    assert moto_slot["color_signature"] == "weekly-weekly-out"
    assert moto_slot["color_label"] == "Por semana + Moto semanal salio"
    assert moto_slot["color_count"] == 2
    assert bici_slot["status_theme"] == "weekly-out"
    assert bici_slot["color_signature"] == "weekly-weekly-out"
    assert bici_slot["color_count"] == 2

    response = client.get("/tickets-board")
    body = response.get_data(as_text=True)
    assert "Moto semanal salio" in body
    assert "ticket-combo-weekly-weekly-out" in body


def test_uncertain_status_is_red_and_excluded_from_inside_counts():
    app, client = build_client()
    login(client)

    client.post(
        "/records/new",
        data={
            "ticket_number": "FICHA-INC-14",
            "client_name": "Cliente Sin Respuesta",
            "vehicle_type": "Moto",
            "stay_mode": "hourly",
            "plate_number": "INC-014",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-INC-14").first()
        record_id = record.id

    response = client.post(
        f"/records/{record_id}/uncertain",
        headers={"Referer": "/tickets-board"},
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Indeciso" in body
    assert "ticket-slot-uncertain" in body
    assert "ticket-combo-hourly-uncertain" in body
    assert "Indecisos" in body

    with app.app_context():
        _records, metrics = build_ticket_board()
        updated = db.session.get(VehicleRecord, record_id)

    assert updated.status == "Indeciso"
    assert updated.status_theme == "uncertain"
    assert next(slot for slot in _records if slot["slot_number"] == 14)["color_signature"] == "hourly-uncertain"
    assert metrics["available"] == 99
    assert metrics["occupied"] == 1
    assert metrics["inside"] == 0
    assert metrics["uncertain"] == 1


def test_datetime_filter_uses_mexico_city_timezone():
    app, _client = build_client()
    with app.app_context():
        rendered = app.jinja_env.filters["dt"](utc_now())
    assert len(rendered) == 19
    assert rendered.count(":") == 2
