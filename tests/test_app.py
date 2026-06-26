from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app import create_app
from app.extensions import db
from app.models import CashCut, Tariff, User, VehicleRecord, get_period_bounds
from app.pricing import utc_now
from app.routes import build_record_history_events
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

    cuts_response = client.get("/cuts")
    cuts_body = cuts_response.get_data(as_text=True)

    assert cuts_response.status_code == 200
    assert "Fecha del corte" in cuts_body
    assert "Corte por fecha" in cuts_body
    assert "Cortes generados" in cuts_body


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

    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        older_paid = VehicleRecord(
            ticket_number="PAGO-OLD",
            physical_ticket_number="81",
            client_name="Pago Viejo",
            vehicle_type="Moto",
            plate_number="OLD-001",
            entry_at=utc_now() - timedelta(hours=5),
            paid_at=utc_now() - timedelta(hours=2),
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
            entry_at=utc_now() - timedelta(hours=4),
            paid_at=utc_now() - timedelta(minutes=30),
            total_amount=20,
            status="Pagado",
            entry_user=user,
            payment_user=user,
        )
        db.session.add_all([older_paid, newer_paid])
        db.session.commit()

    today = utc_now().astimezone(ZoneInfo("America/Mexico_City")).date().isoformat()
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
    assert "Movimientos del dia" in body
    assert "Vehiculos de hoy" in body
    assert "Dentro ahora" in body
    assert "Ya pagaron" in body
    assert "No pagados" in body


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
    assert "Pendiente de salida" in body
    assert "Pagada o cerrada" in body
    assert 'data-open-dialog="ticket-slot-dialog-1"' in body
    assert "Cliente Tablero" in body
    assert "TAB-001" in body
    assert "Abrir en bitacora" in body
    assert "Ficha disponible" in body


def test_datetime_filter_uses_mexico_city_timezone():
    app, _client = build_client()
    with app.app_context():
        rendered = app.jinja_env.filters["dt"](utc_now())
    assert len(rendered) == 19
    assert rendered.count(":") == 2
