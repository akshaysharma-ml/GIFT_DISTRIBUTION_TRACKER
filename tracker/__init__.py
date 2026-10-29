import os

from flask import Flask, flash, redirect, url_for
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from .extensions import csrf, db, login_manager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app(test_config=None):
    app = Flask(
        __name__,
        template_folder=os.path.join(BASE_DIR, "templates"),
        static_folder=os.path.join(BASE_DIR, "static"),
    )

    db_url = os.environ.get("DATABASE_URL") or "sqlite:///" + os.path.join(
        BASE_DIR, "data", "gift_tracker.db"
    )
    if db_url.startswith("postgres://"):  # SQLAlchemy needs the postgresql:// scheme
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        SQLALCHEMY_DATABASE_URI=db_url,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "main.login"
    login_manager.login_message_category = "info"

    from .routes import bp

    app.register_blueprint(bp)

    @app.errorhandler(413)
    def too_large(_):
        flash("File is too large (max 16 MB).", "error")
        return redirect(url_for("main.upload"))

    with app.app_context():
        from . import models  # noqa: F401  (register tables before create_all)

        db.create_all()
        _add_missing_columns()
        _seed_admin()

    return app


def _add_missing_columns():
    """Add columns introduced by newer versions to tables that already exist."""
    inspector = inspect(db.engine)
    preparer = db.engine.dialect.identifier_preparer
    for table in db.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl_type = column.type.compile(dialect=db.engine.dialect)
            statement = "ALTER TABLE {} ADD COLUMN {} {}".format(
                preparer.quote(table.name), preparer.quote(column.name), ddl_type
            )
            with db.engine.begin() as connection:
                connection.execute(text(statement))


def _seed_admin():
    from .models import User

    if User.query.filter_by(role="admin").first():
        return
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "admin123")
    if password == "admin123":
        print("WARNING: using default admin password 'admin123'. Set ADMIN_PASSWORD.")
    admin = User(username=username, role="admin", branch="All")
    admin.set_password(password)
    db.session.add(admin)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()