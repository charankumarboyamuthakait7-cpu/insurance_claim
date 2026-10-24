"""Policyholder area: dashboard, own policies and reimbursement claims."""
from datetime import date, timedelta
from flask import Blueprint, current_app, g, render_template, request, redirect, url_for, flash, abort
from sqlalchemy import select

import models as c
import services as svc
from app import restrict_to_role
from models import Policy, Plan, Claim, ClaimFile, db
from werkzeug.utils import secure_filename
import os
from sqlalchemy import select
from .claims import (
    checklist_view, edit_claim_view, new_claim_view, respond_view, submit_view,
)

bp = Blueprint("policyholder", __name__, url_prefix="/policyholder")
restrict_to_role(bp, c.ROLE_POLICYHOLDER)


@bp.route("/")
def dashboard():
    """Show the policyholder's counts and links."""
    return render_template("policyholder/dashboard.html", cards=svc.dashboard_cards(g.user))


@bp.route("/policies")
def my_policies():
    """List the signed-in policyholder's own policies with plan benefits and members."""
    stmt = select(Policy).where(Policy.holder_user_id == g.user.id).order_by(Policy.id)
    return render_template("policyholder/policies.html", policies=db.session.scalars(stmt).all())


@bp.route("/plans")
def available_plans():
    """Show available fictional plans and their configured benefits before purchase."""
    plans = db.session.scalars(select(Plan).order_by(Plan.annual_limit)).all()
    return render_template("policyholder/plans.html", plans=plans)


@bp.route("/plans/<int:plan_id>/buy", methods=["POST"])
def buy_plan(plan_id):
    """Create a one-year policy for the signed-in holder using a simulated purchase."""
    plan = db.session.get(Plan, plan_id)
    if plan is None:
        abort(404)
    start = date.today()
    policy = Policy(holder_user_id=g.user.id, plan_id=plan.id, start_date=start,
                    end_date=start + timedelta(days=365), status=c.POLICY_ACTIVE)
    db.session.add(policy)
    db.session.commit()
    flash("Simulated purchase complete. Your policy is now listed under My Policies.", "success")
    return redirect(url_for("policyholder.my_policies"))


@bp.route("/claims/<int:claim_id>/documents", methods=["POST"])
def upload_documents(claim_id):
    """Store permitted bill/supporting files and register them against an owned claim."""
    claim = db.session.get(Claim, claim_id)
    if claim is None:
        abort(404)
    if claim.policy.holder_user_id != g.user.id or claim.status not in (c.STATUS_DRAFT, c.STATUS_MORE_INFO):
        abort(403)
    files = request.files.getlist("documents")
    if not files or not any(f.filename for f in files):
        flash("Choose at least one document to upload.", "danger")
        return redirect(url_for("claims.detail", claim_id=claim_id))
    allowed = {"pdf", "png", "jpg", "jpeg"}
    folder = current_app.config["CLAIM_UPLOAD_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    for f in files:
        if not f.filename:
            continue
        name = secure_filename(f.filename)
        if "." not in name or name.rsplit(".", 1)[1].lower() not in allowed:
            flash("Only PDF, PNG, JPG and JPEG files are allowed.", "danger")
            return redirect(url_for("claims.detail", claim_id=claim_id))
        if len(f.read()) > 10 * 1024 * 1024:
            flash("Each file must be 10 MB or smaller.", "danger")
            return redirect(url_for("claims.detail", claim_id=claim_id))
        f.seek(0)
        stored = f"claim_{claim.id}_{os.urandom(8).hex()}_{name}"
        f.save(os.path.join(folder, stored))
        db.session.add(ClaimFile(claim_id=claim.id, original_name=name, stored_name=stored,
                                 uploaded_by_user_id=g.user.id))
    for doc in claim.documents:
        doc.checklist_status = c.DOC_RECEIVED
    db.session.commit()
    flash("Documents uploaded successfully.", "success")
    return redirect(url_for("claims.detail", claim_id=claim_id))


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
