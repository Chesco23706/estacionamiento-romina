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
            "ticket_number": "FLOW-001",
            "client_name": "Cliente QA",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "7",
            "plate_number": "ABC-123",
            "notes": "Registro de prueba",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert "Abrir PDF" in create_response.get_data(as_text=True)

    with app.app_context():
        record = VehicleRecord.query.filter_by(ticket_number="FLOW-001").first()
        assert record is not None
        assert record.stay_mode == "weekly"
        assert record.contracted_days == 7
        record_id = record.id

    exit_response = client.post(f"/records/{record_id}/exit", follow_redirects=True)
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
        assert float(updated.total_amount) >= 10.0
        assert CashCut.query.count() == 1


def test_employee_cannot_access_admin_routes():
    _app, client = build_client()
    login(client, "empleado1", "EmpleadoUno2026!")
    response = client.post("/cuts/generate", data={"cut_type": "daily"})
    assert response.status_code == 403


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


def test_ticket_reprint_route_exists():
    app, client = build_client()
    login(client)
    client.post(
        "/records/new",
        data={
            "ticket_number": "FLOW-002",
            "client_name": "Cliente Ticket",
            "vehicle_type": "Moto",
            "stay_mode": "weekly",
            "contracted_days": "5",
            "plate_number": "XYZ-999",
            "notes": "",
        },
        follow_redirects=True,
    )

    with app.app_context():
        record = VehicleRecord.query.filter_by(ticket_number="FLOW-002").first()
        record_id = record.id

    response = client.get(f"/records/{record_id}/ticket")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Descargar PDF" in body
    assert "FLOW-002" in body

    pdf_response = client.get(f"/records/{record_id}/ticket/document")
    assert pdf_response.status_code == 200
    assert pdf_response.mimetype == "application/pdf"
