"""Claims-officer area: dashboard, review queue, assignment, information requests, decisions."""
from functools import partial

from flask import Blueprint, g, render_template, request
from sqlalchemy import select
from sqlalchemy.orm import joinedload

import models as c
import services as svc
from app import restrict_to_role
from models import Claim, Hospital, db
from .claims import get_visible_claim_or_abort, run_claim_action

bp = Blueprint("officer", __name__, url_prefix="/officer")
restrict_to_role(bp, c.ROLE_OFFICER)


@bp.route("/")
def dashboard():
    """Show the officer's counts and links."""
    return render_template("officer/dashboard.html", cards=svc.dashboard_cards(g.user))


@bp.route("/queue")
def queue():
    """Show submitted / in_review / more_info claims with age, assignee and combined filters."""
    filters = svc.parse_filters(request.args)
    stmt = svc.visible_claims_query(g.user).where(Claim.status.in_(c.QUEUE_STATUSES))
    stmt = svc.apply_filters(stmt, filters).options(
        joinedload(Claim.member), joinedload(Claim.hospital), joinedload(Claim.officer)
    ).order_by(Claim.submitted_at.asc(), Claim.id.asc())
    page = db.paginate(stmt, page=filters["page"], per_page=15, error_out=False)
    hospitals = db.session.scalars(select(Hospital).order_by(Hospital.name)).all()
    return render_template("claims/list.html", page=page, filters=filters, hospitals=hospitals,
                           queue=True, endpoint="officer.queue")


@bp.route("/claims/<int:claim_id>/take", methods=["POST"])
def take(claim_id):
    """Assign the claim to the current officer and start review (conflict-safe)."""
    get_visible_claim_or_abort(claim_id)
    return run_claim_action(claim_id, partial(svc.take_for_review, claim_id, g.user),
                            "Claim assigned to you and moved to review.")


@bp.route("/claims/<int:claim_id>/request-info", methods=["POST"])
def request_info(claim_id):
    """Ask the submitter for more information (in_review → more_info)."""
    get_visible_claim_or_abort(claim_id)
    action = partial(svc.request_information, claim_id, g.user,
                     request.form.get("reason"), request.form.getlist("document_types"))
    return run_claim_action(claim_id, action, "Information request sent to the submitter.")


@bp.route("/claims/<int:claim_id>/approve", methods=["POST"])
def approve(claim_id):
    """Approve with an eligible amount and written reason (in_review → approved)."""
    get_visible_claim_or_abort(claim_id)
    action = partial(svc.approve_claim, claim_id, g.user,
                     request.form.get("eligible_amount"), request.form.get("reason"))
    return run_claim_action(claim_id, action, "Claim approved. Payout calculation saved in the history.")


@bp.route("/claims/<int:claim_id>/reject", methods=["POST"])
def reject(claim_id):
    """Reject with a written reason (in_review → rejected)."""
    get_visible_claim_or_abort(claim_id)
    action = partial(svc.reject_claim, claim_id, g.user, request.form.get("reason"))
    return run_claim_action(claim_id, action, "Claim rejected.")
