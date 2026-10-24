"""Administrator routes: hospitals, plans, users (roles) and policies/members.

Destructive changes are protected (see ``services.validation``): nothing is
deleted, accounts are deactivated instead, and risky edits are refused.
"""
from functools import partial

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask import abort
from sqlalchemy import select

import models as c
import services as svc
from app import restrict_to_role
from models import Hospital, Member, Plan, Policy, User, db
from .claims import get_visible_claim_or_abort, run_claim_action

bp = Blueprint("admin", __name__, url_prefix="/admin")


restrict_to_role(bp, c.ROLE_ADMIN)


def render_list(**context):
    """Render the shared admin list+create page; HTTP 422 when ``errors`` is non-empty."""
    status = 422 if context.get("errors") else 200
    return render_template("admin/list.html", **context), status


def render_edit(title, fields, values, errors, back_url):
    """Render the shared admin edit page; HTTP 422 when ``errors`` is non-empty."""
    html = render_template("admin/form.html", title=title, fields=fields, values=values,
                           errors=errors, back_url=back_url)
    return html, (422 if errors else 200)


def checkbox_values(form):
    """Return a values dict from a submitted form, keeping multi-value fields as lists."""
    values = form.to_dict()
    values["covered_treatment_types"] = form.getlist("covered_treatment_types")
    return values


# ----------------------------------------------------------------- field specs
def hospital_fields():
    """Field definitions for the hospital form."""
    return [
        {"name": "name", "label": "Hospital name", "type": "text", "required": True},
        {"name": "city", "label": "City", "type": "text", "required": True},
        {"name": "network_status", "label": "Network hospital (cashless eligible)", "type": "checkbox"},
    ]


def plan_fields():
    """Field definitions for the plan form."""
    return [
        {"name": "name", "label": "Plan name", "type": "text", "required": True},
        {"name": "covered_treatment_types", "label": "Covered treatment types", "type": "checkboxes",
         "options": [(t, c.TREATMENT_LABELS[t]) for t in c.TREATMENT_TYPES]},
        {"name": "annual_limit", "label": "Annual limit (₹)", "type": "number", "step": "0.01", "required": True},
        {"name": "deductible", "label": "Deductible (₹)", "type": "number", "step": "0.01", "required": True},
        {"name": "copay_percent", "label": "Co-pay (%)", "type": "number", "step": "0.01", "required": True},
    ]


def user_fields(editing):
    """Field definitions for the user form (username is read-only when editing)."""
    hospitals = [("", "— none —")] + [
        (str(h.id), h.name) for h in db.session.scalars(select(Hospital).order_by(Hospital.name))
    ]
    return [
        {"name": "username", "label": "Username", "type": "text", "required": True, "readonly": editing},
        {"name": "display_name", "label": "Display name", "type": "text", "required": True},
        {"name": "role", "label": "Role", "type": "select",
         "options": [(r, c.ROLE_LABELS[r]) for r in c.ROLES]},
        {"name": "hospital_id", "label": "Hospital (hospital desk only)", "type": "select", "options": hospitals},
        {"name": "password", "label": "Password" + (" (leave blank to keep)" if editing else ""),
         "type": "password", "required": not editing},
        {"name": "is_active", "label": "Account active", "type": "checkbox"},
    ]


def policy_fields(editing):
    """Field definitions for the policy form (holder, plan and start date locked on edit)."""
    holders = [(str(u.id), f"{u.display_name} ({u.username})") for u in db.session.scalars(
        select(User).where(User.role == c.ROLE_POLICYHOLDER, User.is_active.is_(True)).order_by(User.display_name))]
    plans = [(str(p.id), p.name) for p in db.session.scalars(select(Plan).order_by(Plan.name))]
    fields = []
    if not editing:
        fields += [
            {"name": "holder_user_id", "label": "Policyholder", "type": "select", "options": holders},
            {"name": "plan_id", "label": "Plan", "type": "select", "options": plans},
            {"name": "start_date", "label": "Start date", "type": "date", "required": True},
        ]
    fields += [
        {"name": "end_date", "label": "End date", "type": "date", "required": True},
        {"name": "status", "label": "Status", "type": "select",
         "options": [(s, s.title()) for s in c.POLICY_STATUSES]},
    ]
    return fields


def member_fields():
    """Field definitions for adding a covered member."""
    return [
        {"name": "display_name", "label": "Member name (synthetic)", "type": "text", "required": True},
        {"name": "relationship", "label": "Relationship", "type": "select",
         "options": [(r, r) for r in c.RELATIONSHIPS]},
    ]


# ------------------------------------------------------------------- dashboard
@bp.route("/")
def dashboard():
    """Admin dashboard: counts plus links to each reference-data area."""
    return render_template("admin/dashboard.html", cards=svc.dashboard_cards(g.user))


# -------------------------------------------------------------------- hospitals
@bp.route("/hospitals", methods=["GET", "POST"])
def hospitals():
    """List hospitals and create a new one."""
    values, errors = {}, {}
    if request.method == "POST":
        cleaned, errors = svc.validate_hospital(request.form)
        if not errors:
            with svc.transaction():
                db.session.add(Hospital(**cleaned))
            flash("Hospital added.", "success")
            return redirect(url_for("admin.hospitals"))
        flash("Please correct the highlighted fields.", "danger")
        values = request.form.to_dict()
    rows = [{"cells": [h.name, h.city, "Yes" if h.network_status else "No"],
             "edit_url": url_for("admin.edit_hospital", hospital_id=h.id)}
            for h in db.session.scalars(select(Hospital).order_by(Hospital.name))]
    return render_list(title="Hospitals", columns=["Name", "City", "Network"], rows=rows,
                       create_title="Add hospital", fields=hospital_fields(), values=values,
                       errors=errors, post_url=url_for("admin.hospitals"))


@bp.route("/hospitals/<int:hospital_id>/edit", methods=["GET", "POST"])
def edit_hospital(hospital_id):
    """Edit a hospital (network status change is blocked while cashless claims are open)."""
    hospital = db.session.get(Hospital, hospital_id) or abort(404)
    values, errors = {"name": hospital.name, "city": hospital.city,
                      "network_status": "on" if hospital.network_status else ""}, {}
    if request.method == "POST":
        cleaned, errors = svc.validate_hospital(request.form, existing=hospital)
        if not errors:
            with svc.transaction():
                hospital.name, hospital.city = cleaned["name"], cleaned["city"]
                hospital.network_status = cleaned["network_status"]
            flash("Hospital updated.", "success")
            return redirect(url_for("admin.hospitals"))
        values = request.form.to_dict()
    return render_edit("Edit hospital", hospital_fields(), values, errors, url_for("admin.hospitals"))


# ------------------------------------------------------------------------ plans
@bp.route("/plans", methods=["GET", "POST"])
def plans():
    """List plans and create a new one."""
    values, errors = {"covered_treatment_types": []}, {}
    if request.method == "POST":
        cleaned, errors = svc.validate_plan(request.form)
        if not errors:
            with svc.transaction():
                db.session.add(Plan(**cleaned))
            flash("Plan added.", "success")
            return redirect(url_for("admin.plans"))
        flash("Please correct the highlighted fields.", "danger")
        values = checkbox_values(request.form)
    rows = [{"cells": [p.name, ", ".join(p.covered_types), str(p.annual_limit), str(p.deductible),
                       f"{p.copay_percent}%"],
             "edit_url": url_for("admin.edit_plan", plan_id=p.id)}
            for p in db.session.scalars(select(Plan).order_by(Plan.name))]
    return render_list(title="Plans (fictional)", columns=["Name", "Covered", "Annual limit", "Deductible", "Co-pay"],
                       rows=rows, create_title="Add plan", fields=plan_fields(), values=values,
                       errors=errors, post_url=url_for("admin.plans"))


@bp.route("/plans/<int:plan_id>/edit", methods=["GET", "POST"])
def edit_plan(plan_id):
    """Edit a plan; existing decisions keep their frozen deductible/co-pay values."""
    plan = db.session.get(Plan, plan_id) or abort(404)
    values, errors = {"name": plan.name, "covered_treatment_types": plan.covered_types,
                      "annual_limit": str(plan.annual_limit), "deductible": str(plan.deductible),
                      "copay_percent": str(plan.copay_percent)}, {}
    if request.method == "POST":
        cleaned, errors = svc.validate_plan(request.form, existing=plan)
        if not errors:
            with svc.transaction():
                for key, val in cleaned.items():
                    setattr(plan, key, val)
            flash("Plan updated.", "success")
            return redirect(url_for("admin.plans"))
        values = checkbox_values(request.form)
    return render_edit("Edit plan", plan_fields(), values, errors, url_for("admin.plans"))


# ------------------------------------------------------------------------ users
@bp.route("/users", methods=["GET", "POST"])
def users():
    """List user accounts and create a new one (roles are assigned here only)."""
    values, errors = {"is_active": "on"}, {}
    if request.method == "POST":
        cleaned, errors = svc.validate_user(request.form, g.user)
        if not errors:
            with svc.transaction():
                user = User(username=cleaned["username"], display_name=cleaned["display_name"],
                            role=cleaned["role"], hospital_id=cleaned["hospital_id"],
                            is_active=cleaned["is_active"])
                user.set_password(cleaned["password"])
                db.session.add(user)
            flash("User created.", "success")
            return redirect(url_for("admin.users"))
        flash("Please correct the highlighted fields.", "danger")
        values = request.form.to_dict()
        values.pop("password", None)
    rows = [{"cells": [u.username, u.display_name, c.ROLE_LABELS[u.role],
                       u.hospital.name if u.hospital else "—", "Active" if u.is_active else "Inactive"],
             "edit_url": url_for("admin.edit_user", user_id=u.id)}
            for u in db.session.scalars(select(User).order_by(User.role, User.username))]
    return render_list(title="Users and roles", columns=["Username", "Name", "Role", "Hospital", "Status"],
                       rows=rows, create_title="Add user", fields=user_fields(False), values=values,
                       errors=errors, post_url=url_for("admin.users"))


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
def edit_user(user_id):
    """Change a user's name, role, hospital, active flag or password (with safeguards)."""
    user = db.session.get(User, user_id) or abort(404)
    values, errors = {"username": user.username, "display_name": user.display_name, "role": user.role,
                      "hospital_id": str(user.hospital_id or ""), "is_active": "on" if user.is_active else ""}, {}
    if request.method == "POST":
        cleaned, errors = svc.validate_user(request.form, g.user, existing=user)
        if not errors:
            with svc.transaction():
                user.display_name, user.role = cleaned["display_name"], cleaned["role"]
                user.hospital_id, user.is_active = cleaned["hospital_id"], cleaned["is_active"]
                if cleaned["password"]:
                    user.set_password(cleaned["password"])
            flash("User updated.", "success")
            return redirect(url_for("admin.users"))
        values = request.form.to_dict()
        values.pop("password", None)
        values["username"] = user.username
    return render_edit("Edit user", user_fields(True), values, errors, url_for("admin.users"))


# --------------------------------------------------------------------- policies
@bp.route("/policies", methods=["GET", "POST"])
def policies():
    """List policies and create a new synthetic policy."""
    values, errors = {"status": c.POLICY_ACTIVE}, {}
    if request.method == "POST":
        cleaned, errors = svc.validate_policy(request.form)
        if not errors:
            with svc.transaction():
                db.session.add(Policy(holder_user_id=cleaned["holder"].id, plan_id=cleaned["plan"].id,
                                      start_date=cleaned["start_date"], end_date=cleaned["end_date"],
                                      status=cleaned["status"]))
            flash("Policy created. Add covered members next.", "success")
            return redirect(url_for("admin.policies"))
        flash("Please correct the highlighted fields.", "danger")
        values = request.form.to_dict()
    rows = [{"cells": [p.label, p.holder.display_name, p.plan.name, f"{p.start_date} → {p.end_date}",
                       p.status.title(), str(len(p.members))],
             "edit_url": url_for("admin.edit_policy", policy_id=p.id),
             "extra_url": url_for("admin.policy_members", policy_id=p.id), "extra_label": "Members"}
            for p in db.session.scalars(select(Policy).order_by(Policy.id))]
    return render_list(title="Policies", columns=["Policy", "Holder", "Plan", "Period", "Status", "Members"],
                       rows=rows, create_title="Add policy", fields=policy_fields(False), values=values,
                       errors=errors, post_url=url_for("admin.policies"))


@bp.route("/policies/<int:policy_id>/edit", methods=["GET", "POST"])
def edit_policy(policy_id):
    """Change a policy's status or end date (holder and plan are protected)."""
    policy = db.session.get(Policy, policy_id) or abort(404)
    values, errors = {"end_date": policy.end_date.isoformat(), "status": policy.status}, {}
    if request.method == "POST":
        cleaned, errors = svc.validate_policy(request.form, existing=policy)
        if not errors:
            with svc.transaction():
                policy.status, policy.end_date = cleaned["status"], cleaned["end_date"]
            flash("Policy updated.", "success")
            return redirect(url_for("admin.policies"))
        values = request.form.to_dict()
    return render_edit(f"Edit {policy.label}", policy_fields(True), values, errors, url_for("admin.policies"))


@bp.route("/policies/<int:policy_id>/members", methods=["GET", "POST"])
def policy_members(policy_id):
    """List and add covered members of one policy."""
    policy = db.session.get(Policy, policy_id) or abort(404)
    values, errors = {}, {}
    if request.method == "POST":
        cleaned, errors = svc.validate_member(request.form)
        if not errors:
            with svc.transaction():
                db.session.add(Member(policy_id=policy.id, display_name=cleaned["display_name"],
                                      relationship_to_holder=cleaned["relationship"]))
            flash("Member added.", "success")
            return redirect(url_for("admin.policy_members", policy_id=policy.id))
        flash("Please correct the highlighted fields.", "danger")
        values = request.form.to_dict()
    rows = [{"cells": [m.display_name, m.relationship_to_holder]} for m in policy.members]
    return render_list(title=f"Members of {policy.label}", columns=["Name", "Relationship"], rows=rows,
                       create_title="Add member", fields=member_fields(), values=values, errors=errors,
                       post_url=url_for("admin.policy_members", policy_id=policy.id),
                       back_url=url_for("admin.policies"))


# ------------------------------------------------------------------ settlement
@bp.route("/claims/<int:claim_id>/settle", methods=["POST"])
def settle(claim_id):
    """Record the simulated settlement for an approved claim (administrator only)."""
    get_visible_claim_or_abort(claim_id)
    return run_claim_action(
        claim_id, partial(svc.record_settlement, claim_id, g.user, request.form.get("amount")),
        "Simulated settlement recorded. No real money was transferred.")
