"""Login, logout and the home redirect - the same login is used by every role."""
from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

import services as svc
from app import ROLE_BLUEPRINT, login_required
from models import User, db

bp = Blueprint("auth", __name__)
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def is_safe_next(target):
    """Return True only for same-site relative paths (prevents open redirects)."""
    return bool(target) and target.startswith("/") and not target.startswith("//") and "\\" not in target


@bp.before_app_request
def load_user():
    """Load the logged-in user (if any, and still active) into ``g.user``."""
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        user = db.session.get(User, user_id)
        if user is not None and user.is_active:
            g.user = user
        else:
            session.clear()


@bp.route("/")
def home():
    """Send each visitor to their own role's dashboard (or to the login page)."""
    if g.user is None:
        return redirect(url_for("auth.login"))
    return redirect(url_for(f"{ROLE_BLUEPRINT[g.user.role]}.dashboard"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    """Show the login form and authenticate username/password credentials."""
    if g.user is not None:
        return redirect(url_for("auth.home"))
    if request.method == "POST":
        username = svc.clean_text(request.form.get("username"))
        password = request.form.get("password") or ""
        user = db.session.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if user is not None and user.is_active and user.check_password(password):
            session.clear()
            session["user_id"] = user.id
            session.permanent = True
            flash(f"Welcome, {user.display_name}.", "success")
            target = request.args.get("next")
            return redirect(target if is_safe_next(target) else url_for("auth.home"))
        if user is None:
            check_password_hash(_DUMMY_HASH, password)  # keep timing similar
        flash("Invalid username or password.", "danger")
        return render_template("auth/login.html", username=username), 401
    return render_template("auth/login.html", username="")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    """Clear the session (POST only, CSRF protected)."""
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
