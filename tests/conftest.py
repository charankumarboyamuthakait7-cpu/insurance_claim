"""Shared pytest fixtures: fresh in-memory app + seeded data for every test."""
import os
import sys

import pytest
from werkzeug.security import generate_password_hash as real_generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import TestingConfig, create_app  # noqa: E402
from models import db  # noqa: E402
from seed import seed_database  # noqa: E402

PASSWORD = "Demo@12345"


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    """Use a cheap PBKDF2 setting in tests so seeding 15 users stays fast."""

    def quick_hash(raw_password):
        """Hash with few iterations (tests only; production uses Werkzeug defaults)."""
        return real_generate_password_hash(raw_password, method="pbkdf2:sha256:1000")

    monkeypatch.setattr("models.generate_password_hash", quick_hash)


@pytest.fixture()
def app():
    """Create a seeded application with an in-memory SQLite database."""
    application = create_app(TestingConfig)
    with application.app_context():
        seed_database()
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    """Return an anonymous test client."""
    return app.test_client()


def login(client, username, password=PASSWORD):
    """Log ``client`` in as ``username`` and return the response."""
    return client.post("/login", data={"username": username, "password": password})


@pytest.fixture()
def login_as(app):
    """Return a factory that makes a new client logged in as the given username."""

    def make(username):
        """Create a client and authenticate it as ``username``."""
        test_client = app.test_client()
        response = login(test_client, username)
        assert response.status_code == 302, f"login failed for {username}"
        return test_client

    return make
