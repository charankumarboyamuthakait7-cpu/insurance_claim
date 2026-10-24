"""Hospital-desk area: dashboard and cashless claims for the desk's own hospital."""
from flask import Blueprint, g, render_template

import models as c
import services as svc
from app import restrict_to_role
from .claims import (
    checklist_view, edit_claim_view, new_claim_view, respond_view, submit_view,
)

bp = Blueprint("hospital", __name__, url_prefix="/hospital")
restrict_to_role(bp, c.ROLE_HOSPITAL)


@bp.route("/")
def dashboard():
    """Show the hospital desk's counts and links."""
    return render_template("hospital/dashboard.html", cards=svc.dashboard_cards(g.user))


@bp.route("/claims/new", methods=["GET", "POST"])
def new_claim():
    """Create a claim (step 1: pick the policy, step 2: save a draft or submit)."""
    return new_claim_view()


@bp.route("/claims/<int:claim_id>/edit", methods=["GET", "POST"])
def edit_claim(claim_id):
    """Edit a draft claim (creator only)."""
    return edit_claim_view(claim_id)


@bp.route("/claims/<int:claim_id>/submit", methods=["POST"])
def submit(claim_id):
    """Submit an existing draft (draft → submitted)."""
    return submit_view(claim_id)


@bp.route("/claims/<int:claim_id>/checklist", methods=["POST"])
def checklist(claim_id):
    """Update document-checklist metadata for a draft or more_info claim."""
    return checklist_view(claim_id)


@bp.route("/claims/<int:claim_id>/respond", methods=["POST"])
def respond(claim_id):
    """Answer an information request (more_info → submitted)."""
    return respond_view(claim_id)
