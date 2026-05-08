from app import create_app
from app.extensions import db
from app.models import CashCut, User, VehicleRecord
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
            "contracted_days": "7",
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
        assert record.contracted_days == 7
        assert record.service_wash is True
        assert record.service_oil_change is True
        record_id = record.id

    exit_response = client.post(f"/records/{record_id}/exit", follow_redirects=True)
    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.status == "Salida registrada"

    pay_response = client.post(f"/records/{record_id}/pay", follow_redirects=True)
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
            "contracted_days": "7",
            "plate_number": "SEM-123",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(physical_ticket_number="FICHA-SEM").first()
        record_id = record.id

    first_exit = client.post(f"/records/{record_id}/weekly-exit", follow_redirects=True)
    second_exit = client.post(f"/records/{record_id}/weekly-exit", follow_redirects=True)
    final_close = client.post(f"/records/{record_id}/exit", follow_redirects=True)

    assert first_exit.status_code == 200
    assert second_exit.status_code == 200
    assert final_close.status_code == 200

    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.weekly_exit_count == 2
        assert updated.latest_weekly_exit_at is not None
        assert updated.exit_at is not None
        assert updated.status == "Salida registrada"


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
    response = client.post(f"/records/{record_id}/exit", follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        updated = db.session.get(VehicleRecord, record_id)
        assert updated.status == "Salida registrada"

    pay_response = client.post(f"/records/{record_id}/pay", follow_redirects=True)
    assert pay_response.status_code == 200

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
        record.close_record(User.query.filter_by(username="admin").first())
        db.session.commit()

    client.post("/logout", follow_redirects=True)
    login(client, "empleado1", "EmpleadoUno2026!")
    response = client.get("/records?status=Salida+registrada")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Marcar pagado" in body
    assert "Editar" in body
    assert "Eliminar" not in body


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
            "contracted_days": "7",
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

    pdf_response = client.get(f"/records/{record_id}/ticket/document")
    assert pdf_response.status_code == 200
    assert pdf_response.mimetype == "application/pdf"


def test_employee_help_page_is_role_specific():
    _app, client = build_client()
    login(client, "empleado1", "EmpleadoUno2026!")
    response = client.get("/help")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "<h3>Empleado</h3>" in body
    assert "<h3>Administrador</h3>" not in body


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
    assert "FICHA-TAB-01" in body
    assert "Marcar salida" in body
