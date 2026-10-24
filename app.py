"""Application setup for the Health Insurance Claims Processing Simulator.

Holds the settings, CSRF protection, small template helpers, login/role
decorators, error pages, CLI commands and the ``create_app`` factory.
This is a classroom simulator: all policies, money rules and outcomes are
fictional and must never be presented as real insurance advice or decisions.
"""
import os
from datetime import timedelta
from decimal import Decimal
from functools import wraps

import click
from dotenv import load_dotenv
from flask import Flask, abort, flash, g, redirect, render_template, request, url_for
from flask_wtf import CSRFProtect

import models as c  # fixed values (roles, statuses, labels) live in models.py
from models import db, utcnow  # noqa: F401  (utcnow re-exported for convenience)

# Which blueprint (URL area) each role uses.
ROLE_BLUEPRINT = {
    c.ROLE_POLICYHOLDER: "policyholder",
    c.ROLE_HOSPITAL: "hospital",
    c.ROLE_OFFICER: "officer",
    c.ROLE_ADMIN: "admin",
}

csrf = CSRFProtect()


# ==========================================================================
# Settings
# ==========================================================================

load_dotenv()


class Config:
    """Default configuration used for local and production-like runs."""

    SECRET_KEY = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "mysql+pymysql://claims_user:claims_password@localhost:3306/claims_simulator",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=2)
    WTF_CSRF_TIME_LIMIT = 3600
    CLAIMS_PER_PAGE = 15
    DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "Demo@12345")


class TestingConfig(Config):
    """Configuration for automated tests: in-memory SQLite, CSRF off by default."""

    TESTING = True
    SECRET_KEY = "test-secret-key"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    DEMO_PASSWORD = "Demo@12345"


# ==========================================================================
# Template helpers (formatting, labels)
# ==========================================================================

def format_inr(value):
    """Format a numeric value as Indian-rupee text with lakh/crore grouping.

    Args:
        value: Decimal, int, float, str or None.

    Returns:
        A string such as ``₹1,00,000.00`` or an em dash when value is None.
    """
    if value is None:
        return "—"
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    sign = "-" if amount < 0 else ""
    whole, _, fraction = f"{abs(amount):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{fraction}"


def format_datetime(value):
    """Format a datetime for display in the UI (UTC)."""
    return value.strftime("%d %b %Y, %H:%M UTC") if value else "—"


def format_date(value):
    """Format a date for display in the UI."""
    return value.strftime("%d %b %Y") if value else "—"


def status_label(status):
    """Return the human-readable label for a claim status value."""
    return c.STATUS_LABELS.get(status, status)


def status_badge(status):
    """Return the Bootstrap contextual colour name used for a claim status badge."""
    return {
        c.STATUS_DRAFT: "secondary",
        c.STATUS_SUBMITTED: "primary",
        c.STATUS_IN_REVIEW: "info",
        c.STATUS_MORE_INFO: "warning",
        c.STATUS_APPROVED: "success",
        c.STATUS_REJECTED: "danger",
        c.STATUS_PAID: "dark",
    }.get(status, "secondary")


def doc_label(value):
    """Return the readable name of a checklist document type."""
    return c.DOC_LABELS.get(value, value)


def role_label(value):
    """Return the readable name of a user role."""
    return c.ROLE_LABELS.get(value, value)


def type_label(value):
    """Return the readable name of a claim type."""
    return c.CLAIM_TYPE_LABELS.get(value, value)


def treatment_label(value):
    """Return the readable name of a treatment type."""
    return c.TREATMENT_LABELS.get(value, value)


def register_template_helpers(app):
    """Register Jinja filters and global constants on the Flask app."""
    app.add_template_filter(format_inr, "inr")
    app.add_template_filter(format_datetime, "dt")
    app.add_template_filter(format_date, "d")
    app.add_template_filter(status_label, "status_label")
    app.add_template_filter(status_badge, "status_badge")
    app.add_template_filter(doc_label, "doc_label")
    app.add_template_filter(role_label, "role_label")
    app.add_template_filter(type_label, "type_label")
    app.add_template_filter(treatment_label, "treatment_label")
    app.jinja_env.globals.update(
        C=c,
        STATUS_LABELS=c.STATUS_LABELS,
        CLAIM_TYPE_LABELS=c.CLAIM_TYPE_LABELS,
        DOC_LABELS=c.DOC_LABELS,
    )


# ==========================================================================
# Login and role decorators
# ==========================================================================

def login_required(view):
    """Redirect anonymous visitors to the login page; otherwise run the view."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        """Check ``g.user`` before delegating to the wrapped view."""
        if g.get("user") is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def restrict_to_role(blueprint, role):
    """Make every page of ``blueprint`` available only to users with ``role``.

    Anonymous visitors are sent to the login page; other roles get HTTP 403.
    This runs on the server for every request, so hiding links is never the
    only protection.
    """

    @blueprint.before_request
    def gate():
        """Check login and role before any view of the blueprint runs."""
        if g.get("user") is None:
            return redirect(url_for("auth.login", next=request.path))
        if g.user.role != role:
            abort(403, description="Your role is not allowed to use this page.")
        return None

    return gate


def role_url(endpoint, **values):
    """Build a URL in the current user's own role area (e.g. ``new_claim``).

    Used in templates shared by the policyholder and hospital-desk areas.
    """
    return url_for(f"{ROLE_BLUEPRINT[g.user.role]}.{endpoint}", **values)


def create_app(config_object=None):
    """Create and configure the Flask application.

    Args:
        config_object: a config class or import string; defaults to ``Config``
            (environment-based, MySQL).
    """
    app = Flask(__name__)
    app.config.from_object(config_object or Config)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY is not set. Copy .env.example to .env and set it.")

    db.init_app(app)
    csrf.init_app(app)
    register_template_helpers(app)

    import services
    from routes import register_blueprints

    register_blueprints(app)
    app.add_template_global(services.next_action, "next_action")
    app.add_template_global(services.can_take, "can_take")
    app.add_template_global(role_url, "role_url")
    register_error_handlers(app)
    register_security_headers(app)
    register_cli(app)
    return app


def register_error_handlers(app):
    """Attach friendly error pages for 400, 403, 404 and 500."""
    titles = {
        400: "Request rejected",
        403: "Access denied",
        404: "Page not found",
        500: "Something went wrong",
    }

    def make_handler(code):
        """Build an error handler function for one HTTP status code."""

        def handler(error):
            """Render the shared error template for ``error``."""
            if code == 500:
                db.session.rollback()
            message = getattr(error, "description", None) or titles[code]
            if code == 500:
                message = "An unexpected error occurred. Nothing was changed."
            return render_template(
                "errors/error.html", code=code, title=titles[code], message=message
            ), code

        return handler

    for code in titles:
        app.register_error_handler(code, make_handler(code))


def register_security_headers(app):
    """Add basic hardening headers to every response."""

    @app.after_request
    def add_headers(response):
        """Set content-type sniffing, framing and referrer protections."""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


def register_cli(app):
    """Register ``flask init-db`` and ``flask seed-db`` commands."""

    @app.cli.command("init-db")
    def init_db_command():
        """Create all tables (does nothing for tables that already exist)."""
        db.create_all()
        click.echo("Tables created.")

    @app.cli.command("seed-db")
    @click.option("--reset", is_flag=True, help="Drop all tables first (destroys data).")
    def seed_db_command(reset):
        """Load the synthetic demo data (hospitals, plans, users, policies, claims)."""
        from seed import seed_database

        seed_database(reset=reset)
        click.echo("Seed data loaded. Demo password: see DEMO_PASSWORD in your .env.")
