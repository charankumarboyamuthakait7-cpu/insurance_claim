"""Blueprint registration: one file per role plus shared pages and login."""


def register_blueprints(app):
    """Register every blueprint on the Flask app."""
    from . import admin, auth, claims, hospital, officer, policyholder

    for module in (auth, claims, policyholder, hospital, officer, admin):
        app.register_blueprint(module.bp)
