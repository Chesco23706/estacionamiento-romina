import os

from flask import Flask, session

from config import INSTANCE_DIR, get_config

from .extensions import csrf, db, login_manager
from .routes import main_bp
from .security import apply_security_headers, configure_proxy


def create_app(config_object=None):
    public_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public")
    app = Flask(
        __name__,
        instance_relative_config=True,
        static_folder=public_dir,
        static_url_path="",
    )
    app.config.from_object(config_object or get_config())

    database_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if database_uri.startswith("sqlite:///"):
        os.makedirs(INSTANCE_DIR, exist_ok=True)

    configure_proxy(app)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    @app.before_request
    def keep_session_permanent():
        session.permanent = True

    @app.after_request
    def set_security_headers(response):
        return apply_security_headers(response)

    with app.app_context():
        from . import models

        db.create_all()
        models.apply_runtime_migrations()
        models.seed_defaults()

    app.register_blueprint(main_bp)
    register_cli_commands(app)

    return app


def register_cli_commands(app):
    @app.cli.command("seed-admin")
    def seed_admin_command():
        from .models import bootstrap_admin_user

        bootstrap_admin_user()
        print("Administrador de bootstrap verificado correctamente.")
