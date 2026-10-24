"""Pages shared by every role (claim list, claim details, hospital directory, reports)
and the claim-form handlers reused by the policyholder and hospital-desk files.
"""
from datetime import date
from functools import partial

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

import models as c
import services as svc
from app import login_required
from models import Claim, Hospital, Policy, db

bp = Blueprint("claims", __name__)


# --------------------------------------------------------------- shared helpers
def get_visible_claim_or_abort(claim_id):
    """Return the claim if the current user may see it; 404 if missing, 403 if out of scope."""
    claim = db.session.get(Claim, claim_id)
    if claim is None:
        abort(404)
    if not svc.claim_visible_to(g.user, claim):
        abort(403, description="You are not allowed to view this claim.")
    return claim


def claim_detail_context(claim):
    """Build the template context (permissions, checks, explanation) for the detail page."""
    user = g.user
    policy = claim.policy
    privileged = user.role in (c.ROLE_OFFICER, c.ROLE_ADMIN)
    show_financials = user.role in (c.ROLE_POLICYHOLDER, c.ROLE_OFFICER, c.ROLE_ADMIN)
    explanation = None
    if claim.eligible_amount is not None and claim.deductible_snapshot is not None:
        explanation = svc.build_explanation(
            claim.requested_amount, claim.eligible_amount,
            claim.deductible_snapshot, claim.copay_snapshot,
        )
    return {
        "claim": claim,
        "policy": policy,
        "plan": policy.plan,
        "show_financials": show_financials,
        "remaining": svc.remaining_cover(policy, claim.id) if show_financials else None,
        "checks": svc.review_checks(claim) if privileged and claim.status != c.STATUS_DRAFT else None,
        "explanation": explanation,
        "can_submit": svc.can_submit(user, claim),
        "can_edit": svc.can_edit_draft(user, claim),
        "can_checklist": svc.can_update_checklist(user, claim),
        "can_respond": svc.can_respond(user, claim),
        "can_take": svc.can_take(user, claim),
        "can_decide": svc.can_decide(user, claim),
        "can_settle": svc.can_settle(user, claim),
    }


def render_claim_detail(claim, form=None, errors=None, status=200):
    """Render the claim detail page, optionally with submitted values and field errors."""
    html = render_template(
        "claims/detail.html", form=form or {}, errors=errors or {}, **claim_detail_context(claim)
    )
    return html, status


def run_claim_action(claim_id, action, success_message):
    """Run a service action, then redirect (PRG) on success or re-render with errors.

    ``PermissionDenied`` becomes HTTP 403; ``WorkflowError`` shows the message and
    any field errors on the detail page with HTTP 422.
    """
    try:
        action()
    except svc.PermissionDenied as exc:
        db.session.rollback()
        abort(403, description=str(exc))
    except svc.WorkflowError as exc:
        flash(exc.message, "danger")
        claim = get_visible_claim_or_abort(claim_id)
        return render_claim_detail(claim, form=request.form, errors=exc.errors, status=422)
    flash(success_message, "success")
    return redirect(url_for("claims.detail", claim_id=claim_id))


def hospital_choices():
    """Hospitals the current user may pick: only their own for hospital desks."""
    stmt = select(Hospital).order_by(Hospital.name)
    if g.user.role == c.ROLE_HOSPITAL:
        stmt = stmt.where(Hospital.id == g.user.hospital_id)
    return db.session.scalars(stmt).all()


def selected_policy(raw_id):
    """Return the policy chosen in step 1 if the current user may use it, else None."""
    policy = db.session.get(Policy, svc.parse_int(raw_id) or 0) if raw_id else None
    if raw_id and policy is None:
        flash("Policy not found.", "warning")
    elif policy is not None and g.user.role == c.ROLE_POLICYHOLDER and policy.holder_user_id != g.user.id:
        flash("That policy is not available to your account.", "danger")
        return None
    return policy


def render_claim_form(policy, form, errors, claim=None, status=200):
    """Render the new/edit claim form (member list depends on the selected policy)."""
    policies = []
    if g.user.role == c.ROLE_POLICYHOLDER:
        policies = db.session.scalars(
            select(Policy).where(Policy.holder_user_id == g.user.id).order_by(Policy.id)
        ).all()
    html = render_template(
        "claims/form.html", policy=policy, policies=policies, members=policy.members if policy else [],
        hospitals=hospital_choices(), form=form, errors=errors, claim=claim, today=date.today().isoformat(),
    )
    return html, status


# ------------------------------------------- handlers reused by the two submitter roles
def new_claim_view():
    """Create a claim: step 1 picks the policy (GET), step 2 saves a draft or submits (POST)."""
    if g.user.role == c.ROLE_HOSPITAL and g.user.hospital_id is None:
        abort(403, description="Your account is not linked to a hospital.")
    if request.method == "POST":
        submit = request.form.get("action") == "submit"
        try:
            claim = svc.create_claim(g.user, request.form, submit=submit)
        except svc.WorkflowError as exc:
            flash(exc.message, "danger")
            policy = db.session.get(Policy, svc.parse_int(request.form.get("policy_id")) or 0)
            if policy is not None and g.user.role == c.ROLE_POLICYHOLDER and policy.holder_user_id != g.user.id:
                policy = None
            return render_claim_form(policy, request.form, exc.errors, status=422)
        flash("Claim submitted." if submit else "Draft saved.", "success")
        return redirect(url_for("claims.detail", claim_id=claim.id))
    policy = selected_policy(request.args.get("policy_id"))
    return render_claim_form(policy, {}, {})


def edit_claim_view(claim_id):
    """Edit a draft claim (creator only); can also submit it directly."""
    claim = get_visible_claim_or_abort(claim_id)
    if not svc.is_creator(g.user, claim):
        abort(403, description="Only the creator can edit this claim.")
    if claim.status != c.STATUS_DRAFT:
        flash("Only draft claims can be edited.", "warning")
        return redirect(url_for("claims.detail", claim_id=claim_id))
    if request.method == "POST":
        submit = request.form.get("action") == "submit"
        try:
            svc.update_draft(claim_id, g.user, request.form, submit=submit)
        except svc.PermissionDenied as exc:
            abort(403, description=str(exc))
        except svc.WorkflowError as exc:
            flash(exc.message, "danger")
            claim = db.session.get(Claim, claim_id)
            return render_claim_form(claim.policy, request.form, exc.errors, claim=claim, status=422)
        flash("Claim submitted." if submit else "Draft updated.", "success")
        return redirect(url_for("claims.detail", claim_id=claim_id))
    return render_claim_form(claim.policy, svc.claim_to_form(claim), {}, claim=claim)


def submit_view(claim_id):
    """Submit an existing draft (draft → submitted)."""
    get_visible_claim_or_abort(claim_id)
    return run_claim_action(claim_id, partial(svc.submit_claim, claim_id, g.user),
                            "Claim submitted for review.")


def checklist_view(claim_id):
    """Update document-checklist metadata for a draft or more_info claim."""
    get_visible_claim_or_abort(claim_id)
    return run_claim_action(claim_id, partial(svc.update_checklist, claim_id, g.user, request.form),
                            "Checklist saved.")


def respond_view(claim_id):
    """Answer an information request (more_info → submitted)."""
    get_visible_claim_or_abort(claim_id)
    return run_claim_action(
        claim_id, partial(svc.respond_to_info, claim_id, g.user, request.form.get("note")),
        "Response sent. The claim is back in the queue.")


# ------------------------------------------------------------------ shared pages
@bp.route("/claims")
@login_required
def list_claims():
    """List claims in the user's scope with combined search and filters (GET parameters)."""
    filters = svc.parse_filters(request.args)
    stmt = svc.apply_filters(svc.visible_claims_query(g.user), filters).options(
        joinedload(Claim.member), joinedload(Claim.hospital)
    ).order_by(Claim.id.desc())
    page = db.paginate(stmt, page=filters["page"], per_page=15, error_out=False)
    hospitals = hospital_choices() if g.user.role == c.ROLE_HOSPITAL else db.session.scalars(
        select(Hospital).order_by(Hospital.name)).all()
    return render_template("claims/list.html", page=page, filters=filters, hospitals=hospitals,
                           queue=False, endpoint="claims.list_claims")


@bp.route("/claims/<int:claim_id>")
@login_required
def detail(claim_id):
    """Show claim detail: snapshot, checklist, decision, timeline and role-specific actions."""
    return render_claim_detail(get_visible_claim_or_abort(claim_id))


@bp.route("/hospitals")
@login_required
def hospital_directory():
    """Show the hospital directory with optional name/city search and network filter."""
    q = svc.clean_text(request.args.get("q"))[:60]
    network_only = request.args.get("network") == "1"
    stmt = select(Hospital).order_by(Hospital.name)
    if q:
        needle = q.lower()
        stmt = stmt.where(
            func.lower(Hospital.name).contains(needle, autoescape=True)
            | func.lower(Hospital.city).contains(needle, autoescape=True)
        )
    if network_only:
        stmt = stmt.where(Hospital.network_status.is_(True))
    return render_template("hospitals.html", hospitals=db.session.scalars(stmt).all(),
                           q=q, network_only=network_only)


@bp.route("/reports")
@login_required
def reports_page():
    """Show claims by status and type with requested, eligible and simulated paid totals."""
    rows, totals = svc.claims_report(g.user)
    return render_template("reports.html", rows=rows, totals=totals)
