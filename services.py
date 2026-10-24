"""Business logic: payout maths, access rules, search, validation, claim workflow and reports.

Everything that decides what is allowed lives here, so route files only receive
requests and show pages. Money uses Decimal, all queries use the ORM, and every
state change runs inside a transaction (see ``transaction`` and the workflow section).
"""
import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import text, and_, false, func, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import NotFound

import models as c  # fixed values (roles, statuses, labels) live in models.py
from app import format_inr, status_label
from models import (
    Claim, ClaimDocument, ClaimHistory, Hospital, Member, Plan, Policy, Settlement, User,
    db, utcnow,
)


# ==========================================================================
# Payout calculation (Decimal only)
# ==========================================================================

TWO_PLACES = Decimal("0.01")


def to_decimal(value):
    """Convert int, str or Decimal to Decimal without passing through float."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def money(value):
    """Round a value to 2 decimal places (half-up) and return it as Decimal."""
    return to_decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def compute_payout(eligible, deductible, copay_percent):
    """Return the simulated payout for an eligible amount.

    Formula: ``round((eligible - min(eligible, deductible)) * (1 - copay/100), 2)``.
    All arithmetic uses Decimal so no binary floating-point error can appear.
    """
    eligible = to_decimal(eligible)
    deductible = to_decimal(deductible)
    copay_percent = to_decimal(copay_percent)
    after_deductible = eligible - min(eligible, deductible)
    payout = after_deductible * (Decimal("1") - copay_percent / Decimal("100"))
    return money(payout)


def build_explanation(requested, eligible, deductible, copay_percent):
    """Return a step-by-step breakdown of how requested became payout.

    The dictionary keys are: requested, eligible, not_eligible_difference,
    deductible_applied, after_deductible, copay_percent, copay_share, payout.
    """
    requested = money(requested)
    eligible = money(eligible)
    deductible = to_decimal(deductible)
    deductible_applied = money(min(eligible, deductible))
    after_deductible = money(eligible - deductible_applied)
    payout = compute_payout(eligible, deductible, copay_percent)
    return {
        "requested": requested,
        "eligible": eligible,
        "not_eligible_difference": money(requested - eligible),
        "deductible_applied": deductible_applied,
        "after_deductible": after_deductible,
        "copay_percent": to_decimal(copay_percent),
        "copay_share": money(after_deductible - payout),
        "payout": payout,
    }


# ==========================================================================
# Access rules (who may see / do what)
# ==========================================================================

def claim_filter(user):
    """Return the SQLAlchemy condition describing which claims ``user`` may see.

    Policyholder: claims on policies they own (other people's drafts excluded).
    Hospital desk: cashless claims for the desk's own hospital.
    Claims officer: every non-draft claim.
    Administrator: every claim.
    """
    if user is None:
        return false()
    if user.role == c.ROLE_POLICYHOLDER:
        own_policies = select(Policy.id).where(Policy.holder_user_id == user.id)
        return and_(
            Claim.policy_id.in_(own_policies),
            or_(Claim.status != c.STATUS_DRAFT, Claim.created_by_user_id == user.id),
        )
    if user.role == c.ROLE_HOSPITAL:
        if user.hospital_id is None:
            return false()
        return and_(
            Claim.hospital_id == user.hospital_id,
            Claim.claim_type == c.CLAIM_CASHLESS,
        )
    if user.role == c.ROLE_OFFICER:
        return Claim.status != c.STATUS_DRAFT
    if user.role == c.ROLE_ADMIN:
        return true()
    return false()


def visible_claims_query(user):
    """Return a ``select(Claim)`` statement already restricted to ``user``'s scope."""
    return select(Claim).where(claim_filter(user))


def claim_visible_to(user, claim):
    """Return True if ``claim`` falls inside ``user``'s scope (checked in the database)."""
    stmt = select(Claim.id).where(Claim.id == claim.id, claim_filter(user))
    return db.session.execute(stmt).first() is not None


def is_creator(user, claim):
    """Return True if ``user`` created ``claim`` (the original submitter)."""
    return user is not None and claim.created_by_user_id == user.id


def can_submit(user, claim):
    """Creator may submit their own draft."""
    return is_creator(user, claim) and claim.status == c.STATUS_DRAFT


def can_edit_draft(user, claim):
    """Creator may edit the claim fields while it is still a draft."""
    return is_creator(user, claim) and claim.status == c.STATUS_DRAFT


def can_update_checklist(user, claim):
    """Creator may update checklist metadata for a draft or more_info claim."""
    return is_creator(user, claim) and claim.status in (c.STATUS_DRAFT, c.STATUS_MORE_INFO)


def can_respond(user, claim):
    """Original submitter may respond to a more_info request."""
    return is_creator(user, claim) and claim.status == c.STATUS_MORE_INFO


def can_take(user, claim):
    """Officer may take a submitted claim that is unassigned or already theirs."""
    return (
        user is not None
        and user.role == c.ROLE_OFFICER
        and claim.status == c.STATUS_SUBMITTED
        and claim.officer_user_id in (None, user.id)
    )


def can_decide(user, claim):
    """Only the assigned officer may request info, approve or reject an in_review claim."""
    return (
        user is not None
        and user.role == c.ROLE_OFFICER
        and claim.status == c.STATUS_IN_REVIEW
        and claim.officer_user_id == user.id
    )


def can_settle(user, claim):
    """Administrator may record a simulated settlement for an approved claim."""
    return user is not None and user.role == c.ROLE_ADMIN and claim.status == c.STATUS_APPROVED


def next_action(user, claim):
    """Return a short text describing what ``user`` should do next with ``claim``."""
    if can_submit(user, claim):
        return "Submit draft"
    if can_respond(user, claim):
        return "Respond to information request"
    if can_take(user, claim):
        return "Take for review"
    if can_decide(user, claim):
        return "Record decision"
    if can_settle(user, claim):
        return "Record simulated settlement"
    labels = {
        c.STATUS_SUBMITTED: "Waiting for a claims officer",
        c.STATUS_IN_REVIEW: "Under review",
        c.STATUS_MORE_INFO: "Waiting for submitter's response",
        c.STATUS_APPROVED: "Waiting for simulated settlement",
        c.STATUS_DRAFT: "Draft not yet submitted",
    }
    return labels.get(claim.status, "No further action (final state)")


# ==========================================================================
# Search and filters
# ==========================================================================

def parse_filters(args):
    """Validate GET parameters and return a clean filters dict.

    Unknown or malformed values are dropped, never trusted. Keys: q, status,
    claim_type, hospital_id (all optional) and page.
    """
    filters = {"q": (args.get("q") or "").strip()[:60]}
    status = (args.get("status") or "").strip()
    filters["status"] = status if status in c.CLAIM_STATUSES else ""
    claim_type = (args.get("claim_type") or "").strip()
    filters["claim_type"] = claim_type if claim_type in c.CLAIM_TYPES else ""
    hospital_raw = (args.get("hospital_id") or "").strip()
    filters["hospital_id"] = int(hospital_raw) if re.fullmatch(r"\d{1,9}", hospital_raw) else None
    page_raw = (args.get("page") or "1").strip()
    filters["page"] = int(page_raw) if re.fullmatch(r"\d{1,6}", page_raw) and int(page_raw) > 0 else 1
    return filters


def apply_filters(stmt, filters):
    """Add the combined search/status/type/hospital conditions to a claim statement."""
    query_text = filters.get("q")
    if query_text:
        id_match = re.fullmatch(r"(?:clm-?)?(\d{1,9})", query_text, flags=re.IGNORECASE)
        name_match = Claim.member_id.in_(
            select(Member.id).where(
                func.lower(Member.display_name).contains(query_text.lower(), autoescape=True)
            )
        )
        if id_match:
            stmt = stmt.where(or_(Claim.id == int(id_match.group(1)), name_match))
        else:
            stmt = stmt.where(name_match)
    if filters.get("status"):
        stmt = stmt.where(Claim.status == filters["status"])
    if filters.get("claim_type"):
        stmt = stmt.where(Claim.claim_type == filters["claim_type"])
    if filters.get("hospital_id"):
        stmt = stmt.where(Claim.hospital_id == filters["hospital_id"])
    return stmt


# ==========================================================================
# Input validation (server side)
# ==========================================================================

MIN_DATE = date(2000, 1, 1)
MAX_MONEY = Decimal("99999999.99")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
OPEN_CASHLESS_STATUSES = (
    c.STATUS_SUBMITTED, c.STATUS_IN_REVIEW, c.STATUS_MORE_INFO, c.STATUS_APPROVED,
)


# --------------------------------------------------------------------------- #
# Low-level parsers
# --------------------------------------------------------------------------- #
def clean_text(value):
    """Return ``value`` as a stripped string with NUL characters removed ('' for None)."""
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def get_list(form, key):
    """Return all submitted values for ``key`` (works with MultiDict and plain dict)."""
    if hasattr(form, "getlist"):
        return form.getlist(key)
    value = form.get(key, [])
    return list(value) if isinstance(value, (list, tuple)) else [value]


def parse_int(value):
    """Parse a positive integer id; return None when blank or malformed."""
    text = clean_text(value)
    return int(text) if re.fullmatch(r"\d{1,9}", text) else None


def parse_date(value):
    """Parse an ISO ``YYYY-MM-DD`` date; return None when blank or invalid."""
    try:
        return datetime.strptime(clean_text(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_money(value, allow_zero=False):
    """Parse a money amount (max 2 decimals) into Decimal, or None when invalid.

    Rejects NaN/Infinity, negatives, more than 2 decimal places, zero (unless
    ``allow_zero``) and values above 99,999,999.99.
    """
    text = clean_text(value).replace(",", "")
    if not re.fullmatch(r"\d{1,8}(\.\d{1,2})?", text):
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if amount < 0 or (amount == 0 and not allow_zero) or amount > MAX_MONEY:
        return None
    return amount.quantize(Decimal("0.01"))


def parse_percent(value):
    """Parse a percentage from 0 to 100 (max 2 decimals) into Decimal, or None."""
    text = clean_text(value)
    if not re.fullmatch(r"\d{1,3}(\.\d{1,2})?", text):
        return None
    amount = Decimal(text)
    return amount if Decimal("0") <= amount <= Decimal("100") else None


def validate_reason(value, errors, key="reason", label="Reason", min_len=5, max_len=1000):
    """Validate a mandatory written reason/note; record any error in ``errors``."""
    text = clean_text(value)
    if len(text) < min_len:
        errors[key] = f"{label} is required (at least {min_len} characters)."
    elif len(text) > max_len:
        errors[key] = f"{label} must be at most {max_len} characters."
    return text


# --------------------------------------------------------------------------- #
# Claim validation
# --------------------------------------------------------------------------- #
def validate_checklist(form, errors):
    """Read the two checklist fields; return {document_type: 'received'|'missing'}."""
    checklist = {}
    for doc_type in c.DOCUMENT_TYPES:
        value = clean_text(form.get(f"doc_{doc_type}")) or c.DOC_MISSING
        if value not in c.DOC_STATUSES:
            errors[f"doc_{doc_type}"] = "Checklist value must be 'received' or 'missing'."
            value = c.DOC_MISSING
        checklist[doc_type] = value
    return checklist


def validate_claim_input(actor, form, for_submit=False, existing=None):
    """Validate claim creation/edit input and return ``(cleaned, errors)``.

    Checks: role may create this claim type, policy exists and (for holders) is
    owned by the actor, member belongs to the chosen policy, hospital exists and
    is within the actor's scope, cashless needs a network hospital, treatment
    type, date order, admission not in the future (on submit), positive amount
    and a description (on submit).

    Args:
        actor: logged-in User.
        form: dict-like with the submitted values.
        for_submit: True when the claim is being submitted (stricter checks).
        existing: the Claim being edited; its claim type and policy are fixed.
    """
    errors, cleaned = {}, {}

    # Claim type is dictated by the actor's role and cannot be forged.
    required_type = c.CLAIM_TYPE_BY_ROLE.get(actor.role)
    if existing is not None:
        claim_type = existing.claim_type
    else:
        claim_type = clean_text(form.get("claim_type")) or required_type
    if required_type is None:
        errors["claim_type"] = "Your role cannot create claims."
    elif claim_type not in c.CLAIM_TYPES:
        errors["claim_type"] = "Choose a valid claim type."
    elif claim_type != required_type:
        label = c.CLAIM_TYPE_LABELS[required_type].lower()
        errors["claim_type"] = f"Your role can only submit {label} claims."
    cleaned["claim_type"] = claim_type

    # Policy: must exist; holders may only use their own policies.
    policy = None
    if existing is not None:
        policy = existing.policy
    else:
        policy = db.session.get(Policy, parse_int(form.get("policy_id")) or 0)
        if policy is None:
            errors["policy_id"] = "Select a valid policy."
        elif actor.role == c.ROLE_POLICYHOLDER and policy.holder_user_id != actor.id:
            errors["policy_id"] = "That policy is not available to your account."
            policy = None
    cleaned["policy"] = policy

    # Member must be covered by exactly the chosen policy.
    member = db.session.get(Member, parse_int(form.get("member_id")) or 0)
    if member is None:
        errors["member_id"] = "Select a covered member."
    elif policy is not None and member.policy_id != policy.id:
        errors["member_id"] = "The selected member is not covered by the chosen policy."
        member = None
    cleaned["member"] = member

    # Hospital scope and network rule.
    hospital = db.session.get(Hospital, parse_int(form.get("hospital_id")) or 0)
    if hospital is None:
        errors["hospital_id"] = "Select a valid hospital."
    else:
        if actor.role == c.ROLE_HOSPITAL and hospital.id != actor.hospital_id:
            errors["hospital_id"] = "Cashless requests can only be submitted for your own hospital."
        elif claim_type == c.CLAIM_CASHLESS and not hospital.network_status:
            errors["hospital_id"] = "Cashless requests need a network hospital."
    cleaned["hospital"] = hospital

    treatment = clean_text(form.get("treatment_type"))
    if treatment not in c.TREATMENT_TYPES:
        errors["treatment_type"] = "Choose inpatient or daycare."
    cleaned["treatment_type"] = treatment

    admission = parse_date(form.get("admission_date"))
    discharge = parse_date(form.get("discharge_date"))
    if admission is None:
        errors["admission_date"] = "Enter a valid admission date."
    elif admission < MIN_DATE:
        errors["admission_date"] = "Admission date is too far in the past."
    elif for_submit and admission > date.today():
        errors["admission_date"] = "Admission date cannot be in the future."
    if discharge is None:
        errors["discharge_date"] = "Enter a valid discharge date."
    elif admission is not None and discharge < admission:
        errors["discharge_date"] = "Discharge date cannot be before the admission date."
    cleaned["admission_date"], cleaned["discharge_date"] = admission, discharge

    amount = parse_money(form.get("requested_amount"))
    if amount is None:
        errors["requested_amount"] = "Enter a positive amount with at most 2 decimals (max 99,999,999.99)."
    cleaned["requested_amount"] = amount
    if amount is not None and policy is not None:
        available = remaining_cover(policy, existing.id if existing is not None else None)
        if amount > available:
            errors["requested_amount"] = (
                f"Requested amount exceeds available coverage ({format_inr(available)} remaining)."
            )

    description = clean_text(form.get("description"))
    if len(description) > 500:
        errors["description"] = "Description must be at most 500 characters."
    elif for_submit and not description:
        errors["description"] = "A short description is required to submit."
    cleaned["description"] = description or None

    cleaned["checklist"] = validate_checklist(form, errors)
    return cleaned, errors


# --------------------------------------------------------------------------- #
# Administrator reference-data validation
# --------------------------------------------------------------------------- #
def validate_hospital(form, existing=None):
    """Validate hospital create/edit input; returns ``(cleaned, errors)``.

    Protects against un-networking a hospital that still has open cashless claims.
    """
    errors = {}
    name = clean_text(form.get("name"))
    city = clean_text(form.get("city"))
    network = clean_text(form.get("network_status")) in ("on", "1", "true")
    if not 3 <= len(name) <= 120:
        errors["name"] = "Name must be 3 to 120 characters."
    else:
        stmt = select(Hospital.id).where(func.lower(Hospital.name) == name.lower())
        if existing is not None:
            stmt = stmt.where(Hospital.id != existing.id)
        if db.session.execute(stmt).first():
            errors["name"] = "A hospital with this name already exists."
    if not 2 <= len(city) <= 80:
        errors["city"] = "City must be 2 to 80 characters."
    if existing is not None and existing.network_status and not network:
        open_count = db.session.scalar(
            select(func.count(Claim.id)).where(
                Claim.hospital_id == existing.id,
                Claim.claim_type == c.CLAIM_CASHLESS,
                Claim.status.in_(OPEN_CASHLESS_STATUSES),
            )
        )
        if open_count:
            errors["network_status"] = (
                f"Cannot remove network status: {open_count} open cashless claim(s) exist."
            )
    return {"name": name, "city": city, "network_status": network}, errors


def validate_plan(form, existing=None):
    """Validate plan create/edit input; returns ``(cleaned, errors)``."""
    errors = {}
    name = clean_text(form.get("name"))
    types = [t for t in get_list(form, "covered_treatment_types") if t in c.TREATMENT_TYPES]
    limit = parse_money(form.get("annual_limit"))
    deductible = parse_money(form.get("deductible"), allow_zero=True)
    copay = parse_percent(form.get("copay_percent"))
    if not 3 <= len(name) <= 80:
        errors["name"] = "Name must be 3 to 80 characters."
    else:
        stmt = select(Plan.id).where(func.lower(Plan.name) == name.lower())
        if existing is not None:
            stmt = stmt.where(Plan.id != existing.id)
        if db.session.execute(stmt).first():
            errors["name"] = "A plan with this name already exists."
    if not types:
        errors["covered_treatment_types"] = "Select at least one covered treatment type."
    if limit is None:
        errors["annual_limit"] = "Enter a positive annual limit."
    if deductible is None:
        errors["deductible"] = "Enter the deductible (0 or more)."
    elif limit is not None and deductible > limit:
        errors["deductible"] = "Deductible cannot exceed the annual limit."
    if copay is None:
        errors["copay_percent"] = "Co-pay must be between 0 and 100."
    return {
        "name": name, "covered_treatment_types": ",".join(sorted(set(types))),
        "annual_limit": limit, "deductible": deductible, "copay_percent": copay,
    }, errors


def validate_user(form, acting_user, existing=None):
    """Validate user create/edit input; returns ``(cleaned, errors)``.

    Protections: unique username, strong-enough password, hospital desk needs a
    hospital, an admin cannot demote/deactivate themselves, the last active
    administrator is kept, and a holder who owns policies keeps the holder role.
    """
    errors = {}
    username = clean_text(form.get("username")) if existing is None else existing.username
    display_name = clean_text(form.get("display_name"))
    role = clean_text(form.get("role"))
    is_active = clean_text(form.get("is_active")) in ("on", "1", "true")
    password = form.get("password") or ""
    hospital_id = parse_int(form.get("hospital_id"))

    if existing is None:
        if not USERNAME_RE.fullmatch(username):
            errors["username"] = "Username must be 3-50 letters, digits, dots, dashes or underscores."
        elif db.session.execute(select(User.id).where(User.username == username)).first():
            errors["username"] = "That username is already taken."
    if not 2 <= len(display_name) <= 100:
        errors["display_name"] = "Display name must be 2 to 100 characters."
    if role not in c.ROLES:
        errors["role"] = "Choose a valid role."
    if role == c.ROLE_HOSPITAL:
        if db.session.get(Hospital, hospital_id or 0) is None:
            errors["hospital_id"] = "A hospital desk account must be linked to a hospital."
    else:
        hospital_id = None
    if existing is None or password:
        if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
            errors["password"] = "Password must be at least 8 characters with letters and digits."
    if existing is not None:
        if existing.id == acting_user.id and (role != existing.role or not is_active):
            errors["role"] = "You cannot change your own role or deactivate yourself."
        if existing.role == c.ROLE_ADMIN and (role != c.ROLE_ADMIN or not is_active):
            others = db.session.scalar(
                select(func.count(User.id)).where(
                    User.role == c.ROLE_ADMIN, User.is_active.is_(True), User.id != existing.id
                )
            )
            if not others:
                errors["role"] = "At least one active administrator must remain."
        if existing.role == c.ROLE_POLICYHOLDER and role != c.ROLE_POLICYHOLDER:
            owns = db.session.scalar(
                select(func.count(Policy.id)).where(Policy.holder_user_id == existing.id)
            )
            if owns:
                errors["role"] = "This user holds policies and must remain a policyholder."
    return {
        "username": username, "display_name": display_name, "role": role,
        "hospital_id": hospital_id, "is_active": is_active, "password": password,
    }, errors


def validate_policy(form, existing=None):
    """Validate policy create/edit input; returns ``(cleaned, errors)``.

    On edit only status and end date may change (holder and plan are protected).
    """
    errors = {}
    status = clean_text(form.get("status"))
    end_date = parse_date(form.get("end_date"))
    if status not in c.POLICY_STATUSES:
        errors["status"] = "Choose a valid status."
    if end_date is None:
        errors["end_date"] = "Enter a valid end date."
    cleaned = {"status": status, "end_date": end_date}
    if existing is not None:
        if end_date is not None and end_date < existing.start_date:
            errors["end_date"] = "End date cannot be before the start date."
        return cleaned, errors
    holder = db.session.get(User, parse_int(form.get("holder_user_id")) or 0)
    plan = db.session.get(Plan, parse_int(form.get("plan_id")) or 0)
    start_date = parse_date(form.get("start_date"))
    if holder is None or holder.role != c.ROLE_POLICYHOLDER or not holder.is_active:
        errors["holder_user_id"] = "Choose an active policyholder."
    if plan is None:
        errors["plan_id"] = "Choose a plan."
    if start_date is None:
        errors["start_date"] = "Enter a valid start date."
    elif end_date is not None and end_date < start_date:
        errors["end_date"] = "End date cannot be before the start date."
    cleaned.update({"holder": holder, "plan": plan, "start_date": start_date})
    return cleaned, errors


def validate_member(form):
    """Validate a new covered member (name and relationship); returns ``(cleaned, errors)``."""
    errors = {}
    name = clean_text(form.get("display_name"))
    relationship = clean_text(form.get("relationship"))
    if not 2 <= len(name) <= 100:
        errors["display_name"] = "Name must be 2 to 100 characters."
    if relationship not in c.RELATIONSHIPS:
        errors["relationship"] = "Choose a valid relationship."
    return {"display_name": name, "relationship": relationship}, errors


# ==========================================================================
# Claim workflow (all status changes happen here)
# ==========================================================================

class WorkflowError(Exception):
    """A business-rule or validation failure that should be shown to the user."""

    def __init__(self, message, errors=None):
        """Store a user-facing ``message`` and optional per-field ``errors`` dict."""
        super().__init__(message)
        self.message = message
        self.errors = errors or {}


class PermissionDenied(Exception):
    """The actor's role, ownership or assignment does not allow this action (HTTP 403)."""


@contextmanager
def transaction():
    """Commit when the block succeeds; roll everything back on any exception."""
    try:
        yield db.session
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def require(condition, message="You are not allowed to perform this action."):
    """Raise PermissionDenied unless ``condition`` is true."""
    if not condition:
        raise PermissionDenied(message)


def lock_claim(claim_id):
    """Load a claim with a row lock and fresh values; raise NotFound if it is missing."""
    stmt = (
        select(Claim).where(Claim.id == claim_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    claim = db.session.execute(stmt).scalar_one_or_none()
    if claim is None:
        raise NotFound()
    return claim


def record_history(claim, actor, from_status, to_status, note, event_type=c.EVENT_STATUS):
    """Append an audit event (actor, previous/new state, note, timestamp) to a claim."""
    claim.history.append(ClaimHistory(
        actor_user_id=actor.id, from_status=from_status, to_status=to_status,
        note=note, event_type=event_type, timestamp=utcnow(),
    ))


def check_transition(from_status, to_status, actor):
    """Validate a (from, to) status pair and the actor's role; raise if not allowed."""
    roles = c.ALLOWED_TRANSITIONS.get((from_status, to_status))
    if roles is None:
        raise WorkflowError(
            f"Invalid status change: {status_label(from_status)} → {status_label(to_status)} "
            "is not an allowed transition."
        )
    require(actor.role in roles, "Your role cannot perform this status change.")


def change_status(claim, to_status, actor, note):
    """Move ``claim`` to ``to_status`` after validating it, and write the history event."""
    check_transition(claim.status, to_status, actor)
    previous = claim.status
    # Pass the acting user and human-readable note to the MySQL trigger on this
    # same transaction/connection. The trigger writes the status-change event.
    db.session.execute(
        text("SET @claimcare_actor_user_id = :actor_id, @claimcare_history_note = :note"),
        {"actor_id": actor.id, "note": note},
    )
    claim.status = to_status
    claim.updated_at = utcnow()


def claim_to_form(claim):
    """Return the claim's stored values as a form-like dict (used to re-validate)."""
    data = {
        "claim_type": claim.claim_type, "policy_id": str(claim.policy_id),
        "member_id": str(claim.member_id), "hospital_id": str(claim.hospital_id),
        "treatment_type": claim.treatment_type,
        "admission_date": claim.admission_date.isoformat(),
        "discharge_date": claim.discharge_date.isoformat(),
        "requested_amount": str(claim.requested_amount),
        "description": claim.description or "",
    }
    for doc in claim.documents:
        data[f"doc_{doc.document_type}"] = doc.checklist_status
    return data


def _apply_checklist(claim, actor, new_values):
    """Set checklist statuses and write one audit event if anything changed."""
    changes = []
    for doc in claim.documents:
        wanted = new_values.get(doc.document_type, doc.checklist_status)
        if wanted != doc.checklist_status:
            changes.append(f"{c.DOC_LABELS[doc.document_type]}: {doc.checklist_status} → {wanted}")
            doc.checklist_status = wanted
    if changes:
        record_history(claim, actor, claim.status, claim.status,
                       "Checklist updated - " + "; ".join(changes), c.EVENT_CHECKLIST)
    return changes


# --------------------------------------------------------------------------- #
# Submitter actions
# --------------------------------------------------------------------------- #
def create_claim(actor, form, submit=False):
    """Create a draft (or draft + submit) claim after full server-side validation."""
    cleaned, errors = validate_claim_input(actor, form, for_submit=submit)
    if errors:
        raise WorkflowError("Please correct the highlighted fields.", errors)
    with transaction():
        claim = Claim(
            policy_id=cleaned["policy"].id, member_id=cleaned["member"].id,
            hospital_id=cleaned["hospital"].id, claim_type=cleaned["claim_type"],
            treatment_type=cleaned["treatment_type"],
            admission_date=cleaned["admission_date"], discharge_date=cleaned["discharge_date"],
            requested_amount=cleaned["requested_amount"], description=cleaned["description"],
            status=c.STATUS_DRAFT, created_by_user_id=actor.id,
            documents=[
                ClaimDocument(document_type=t, checklist_status=cleaned["checklist"][t])
                for t in c.DOCUMENT_TYPES
            ],
        )
        db.session.add(claim)
        # The AFTER INSERT trigger creates the initial claim-history row.
        if submit:
            change_status(claim, c.STATUS_SUBMITTED, actor, "Submitted by creator")
            claim.submitted_at = utcnow()
    return claim


def update_draft(claim_id, actor, form, submit=False):
    """Edit a draft's fields and checklist (creator only); optionally submit it."""
    with transaction():
        claim = lock_claim(claim_id)
        require(is_creator(actor, claim), "Only the creator can edit this claim.")
        if claim.status != c.STATUS_DRAFT:
            raise WorkflowError("Only draft claims can be edited.")
        cleaned, errors = validate_claim_input(actor, form, for_submit=submit, existing=claim)
        if errors:
            raise WorkflowError("Please correct the highlighted fields.", errors)
        claim.member_id = cleaned["member"].id
        claim.hospital_id = cleaned["hospital"].id
        claim.treatment_type = cleaned["treatment_type"]
        claim.admission_date = cleaned["admission_date"]
        claim.discharge_date = cleaned["discharge_date"]
        claim.requested_amount = cleaned["requested_amount"]
        claim.description = cleaned["description"]
        _apply_checklist(claim, actor, cleaned["checklist"])
        record_history(claim, actor, c.STATUS_DRAFT, c.STATUS_DRAFT, "Draft updated", c.EVENT_EDIT)
        if submit:
            change_status(claim, c.STATUS_SUBMITTED, actor, "Submitted by creator")
            claim.submitted_at = utcnow()
    return claim


def submit_claim(claim_id, actor):
    """Submit an existing draft: re-validates every field, then draft → submitted."""
    with transaction():
        claim = lock_claim(claim_id)
        require(is_creator(actor, claim), "Only the creator can submit this claim.")
        check_transition(claim.status, c.STATUS_SUBMITTED, actor)
        _, errors = validate_claim_input(actor, claim_to_form(claim), for_submit=True, existing=claim)
        if errors:
            raise WorkflowError("This draft cannot be submitted: " + "; ".join(errors.values()), errors)
        change_status(claim, c.STATUS_SUBMITTED, actor, "Submitted by creator")
        claim.submitted_at = utcnow()
    return claim


def update_checklist(claim_id, actor, form):
    """Update checklist metadata on a draft or more_info claim (creator only, audited)."""
    errors = {}
    values = validate_checklist(form, errors)
    if errors:
        raise WorkflowError("Invalid checklist values.", errors)
    with transaction():
        claim = lock_claim(claim_id)
        require(is_creator(actor, claim), "Only the submitter can update the checklist.")
        if claim.status not in (c.STATUS_DRAFT, c.STATUS_MORE_INFO):
            raise WorkflowError("The checklist can only be changed for draft or more-information claims.")
        changes = _apply_checklist(claim, actor, values)
    return changes


def respond_to_info(claim_id, actor, note):
    """Original submitter answers an information request: more_info → submitted."""
    errors = {}
    text = validate_reason(note, errors, key="note", label="Response")
    with transaction():
        claim = lock_claim(claim_id)
        require(is_creator(actor, claim), "Only the original submitter can respond.")
        check_transition(claim.status, c.STATUS_SUBMITTED, actor)
        if errors:
            raise WorkflowError("Please write a response.", errors)
        for doc in claim.documents:
            doc.info_requested = False
        change_status(claim, c.STATUS_SUBMITTED, actor, f"Response from submitter: {text}")
    return claim


# --------------------------------------------------------------------------- #
# Claims officer actions
# --------------------------------------------------------------------------- #
def take_for_review(claim_id, officer):
    """Assign a submitted claim to ``officer`` and move it to in_review, atomically.

    The conditional UPDATE only succeeds while the claim is still ``submitted``
    and unassigned (or already assigned to this officer), so a second officer
    gets a clear conflict message instead of silently overwriting the first.
    """
    require(officer.role == c.ROLE_OFFICER, "Only claims officers can review claims.")
    with transaction():
        claim = lock_claim(claim_id)
        if claim.status == c.STATUS_IN_REVIEW and claim.officer_user_id not in (None, officer.id):
            raise WorkflowError(f"{claim.label} is already being reviewed by {claim.officer.display_name}.")
        check_transition(claim.status, c.STATUS_IN_REVIEW, officer)
        if claim.officer_user_id not in (None, officer.id):
            raise WorkflowError(f"{claim.label} is assigned to {claim.officer.display_name}.")
        db.session.execute(
            text("SET @claimcare_actor_user_id = :actor_id, @claimcare_history_note = :note"),
            {"actor_id": officer.id, "note": f"Assigned to {officer.display_name} and review started"},
        )
        result = db.session.execute(
            update(Claim)
            .where(
                Claim.id == claim.id, Claim.status == c.STATUS_SUBMITTED,
                or_(Claim.officer_user_id.is_(None), Claim.officer_user_id == officer.id),
            )
            .values(status=c.STATUS_IN_REVIEW, officer_user_id=officer.id, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise WorkflowError("Another officer took this claim first. Refresh the queue.")
        db.session.refresh(claim)
    return claim


def _lock_assigned_claim(claim_id, officer, target_status):
    """Lock a claim and verify transition + that ``officer`` is the assigned reviewer."""
    require(officer.role == c.ROLE_OFFICER, "Only claims officers can record decisions.")
    claim = lock_claim(claim_id)
    check_transition(claim.status, target_status, officer)
    require(claim.officer_user_id == officer.id, "Only the assigned officer can act on this claim.")
    return claim


def request_information(claim_id, officer, reason, document_types):
    """in_review → more_info: record the reason and which checklist items are requested."""
    errors = {}
    text = validate_reason(reason, errors)
    wanted = [d for d in get_list({"d": document_types}, "d") if d in c.DOCUMENT_TYPES]
    if not wanted:
        errors["document_types"] = "Select at least one checklist item to request."
    with transaction():
        claim = _lock_assigned_claim(claim_id, officer, c.STATUS_MORE_INFO)
        if errors:
            raise WorkflowError("Cannot request information yet.", errors)
        for doc in claim.documents:
            if doc.document_type in wanted:
                doc.checklist_status = c.DOC_MISSING
                doc.info_requested = True
        names = ", ".join(c.DOC_LABELS[d] for d in wanted)
        change_status(claim, c.STATUS_MORE_INFO, officer, f"Requested: {names}. Reason: {text}")
    return claim


def reject_claim(claim_id, officer, reason):
    """in_review → rejected. A written reason is mandatory; no eligible amount is stored."""
    errors = {}
    text = validate_reason(reason, errors)
    with transaction():
        claim = _lock_assigned_claim(claim_id, officer, c.STATUS_REJECTED)
        if errors:
            raise WorkflowError("A written reason is required to reject a claim.", errors)
        claim.decision_reason = text
        change_status(claim, c.STATUS_REJECTED, officer, f"Rejected. Reason: {text}")
    return claim


def approve_claim(claim_id, officer, eligible_raw, reason):
    """in_review → approved with an officer-entered eligible amount and written reason.

    Enforced: 0 < eligible <= requested, eligible <= remaining cover, admission
    inside an active policy period, treatment type covered by the plan. The
    payout is calculated and frozen together with the plan's deductible/co-pay
    so later plan edits cannot change an existing decision.
    """
    errors = {}
    text = validate_reason(reason, errors)
    eligible = parse_money(eligible_raw)
    with transaction():
        claim = _lock_assigned_claim(claim_id, officer, c.STATUS_APPROVED)
        policy, plan = claim.policy, claim.policy.plan
        if eligible is None:
            errors["eligible_amount"] = "Enter a positive eligible amount (max 2 decimals)."
        elif eligible > claim.requested_amount:
            errors["eligible_amount"] = (
                f"Eligible amount cannot exceed the requested amount ({format_inr(claim.requested_amount)})."
            )
        else:
            remaining = remaining_cover(policy, exclude_claim_id=claim.id)
            if eligible > remaining:
                errors["eligible_amount"] = (
                    f"Eligible amount exceeds the remaining simulated cover ({format_inr(remaining)})."
                )
        if not policy.covers_date(claim.admission_date):
            errors["policy"] = (
                "Admission date is outside the active policy period (or the policy is not active); "
                "this claim cannot be approved - reject it with a reason."
            )
        if not plan.covers(claim.treatment_type):
            errors["treatment"] = (
                f"The plan does not cover {claim.treatment_type} treatment; "
                "this claim cannot be approved - reject it with a reason."
            )
        if errors:
            raise WorkflowError("This claim cannot be approved as entered.", errors)
        payout = compute_payout(eligible, plan.deductible, plan.copay_percent)
        claim.eligible_amount = eligible
        claim.payout_amount = payout
        claim.deductible_snapshot = plan.deductible
        claim.copay_snapshot = plan.copay_percent
        claim.decision_reason = text
        change_status(
            claim, c.STATUS_APPROVED, officer,
            f"Approved. Eligible {format_inr(eligible)}; simulated payout {format_inr(payout)}. Reason: {text}",
        )
    return claim


# --------------------------------------------------------------------------- #
# Administrator action
# --------------------------------------------------------------------------- #
def payee_for(claim):
    """Return the payee type implied by the claim type (hospital or policyholder)."""
    return c.PAYEE_HOSPITAL if claim.claim_type == c.CLAIM_CASHLESS else c.PAYEE_POLICYHOLDER


def record_settlement(claim_id, admin, amount_raw):
    """approved → paid: record the simulated settlement (administrator only).

    The posted amount must equal the payout recomputed from the frozen decision
    values, so it cannot be silently changed. No real money is transferred.
    """
    require(admin.role == c.ROLE_ADMIN, "Only administrators can record settlements.")
    try:
        with transaction():
            claim = lock_claim(claim_id)
            check_transition(claim.status, c.STATUS_PAID, admin)
            if db.session.execute(select(Settlement.id).where(Settlement.claim_id == claim.id)).first():
                raise WorkflowError("This claim already has a settlement record.")
            expected = compute_payout(claim.eligible_amount, claim.deductible_snapshot, claim.copay_snapshot)
            posted = parse_money(amount_raw)
            if claim.payout_amount != expected:
                raise WorkflowError("Stored payout does not match the calculation; settlement refused.")
            if posted is None or posted != expected:
                raise WorkflowError(
                    f"Settlement amount must equal the computed payout ({format_inr(expected)}).",
                    {"amount": "Amount does not match the computed payout."},
                )
            if expected > remaining_cover(claim.policy, exclude_claim_id=claim.id):
                raise WorkflowError("Payout would exceed the remaining simulated cover for this policy.")
            payee = payee_for(claim)
            db.session.add(Settlement(
                claim_id=claim.id, payee_type=payee, amount=expected,
                recorded_by=admin.id, recorded_at=utcnow(),
            ))
            change_status(claim, c.STATUS_PAID, admin,
                          f"Simulated settlement of {format_inr(expected)} recorded for {payee}")
    except IntegrityError as exc:
        raise WorkflowError("This claim already has a settlement record.") from exc
    return claim


# --------------------------------------------------------------------------- #
# Review helpers (read-only)
# --------------------------------------------------------------------------- #
def remaining_cover(policy, exclude_claim_id=None):
    """Annual limit minus simulated paid payouts for the policy's current policy year.

    The "policy year" is the policy's own start-end period; only settlements of
    claims admitted inside that period are counted.
    """
    stmt = (
        select(func.coalesce(func.sum(Settlement.amount), 0))
        .join(Claim, Claim.id == Settlement.claim_id)
        .where(
            Claim.policy_id == policy.id,
            Claim.admission_date >= policy.start_date,
            Claim.admission_date <= policy.end_date,
        )
    )
    if exclude_claim_id is not None:
        stmt = stmt.where(Claim.id != exclude_claim_id)
    paid = Decimal(str(db.session.execute(stmt).scalar_one()))
    return max(Decimal("0.00"), Decimal(policy.plan.annual_limit) - paid)


def find_duplicates(claim):
    """Return other non-draft claims with the same member, hospital, dates and type."""
    stmt = select(Claim).where(
        Claim.id != claim.id, Claim.member_id == claim.member_id,
        Claim.hospital_id == claim.hospital_id,
        Claim.admission_date == claim.admission_date,
        Claim.discharge_date == claim.discharge_date,
        Claim.claim_type == claim.claim_type, Claim.status != c.STATUS_DRAFT,
    )
    return list(db.session.execute(stmt).scalars())


def review_checks(claim):
    """Collect the officer's review checklist: policy period, treatment, documents, duplicates, cover."""
    policy = claim.policy
    return {
        "policy_ok": policy.covers_date(claim.admission_date),
        "treatment_ok": policy.plan.covers(claim.treatment_type),
        "hospital_network": claim.hospital.network_status,
        "missing_documents": claim.missing_documents,
        "duplicates": find_duplicates(claim),
        "remaining_cover": remaining_cover(policy, exclude_claim_id=claim.id),
    }


# ==========================================================================
# Dashboard and report totals
# ==========================================================================

def _count_by_status(user):
    """Return {status: count} for the claims ``user`` may see."""
    stmt = select(Claim.status, func.count(Claim.id)).where(claim_filter(user)).group_by(Claim.status)
    return {status: count for status, count in db.session.execute(stmt).all()}


def dashboard_cards(user):
    """Build the role-specific dashboard cards: label, value and optional link filters."""
    counts = _count_by_status(user)
    total = sum(counts.values())
    cards = [{"label": "Claims in your scope", "value": total, "params": {}}]
    if user.role == c.ROLE_POLICYHOLDER:
        policies = db.session.scalar(select(func.count(Policy.id)).where(Policy.holder_user_id == user.id))
        cards.insert(0, {"label": "My policies", "value": policies, "endpoint": "policyholder.my_policies"})
        for status in (c.STATUS_DRAFT, c.STATUS_SUBMITTED, c.STATUS_MORE_INFO, c.STATUS_APPROVED, c.STATUS_PAID):
            cards.append({"label": c.STATUS_LABELS[status], "value": counts.get(status, 0),
                          "params": {"status": status}})
    elif user.role == c.ROLE_HOSPITAL:
        for status in (c.STATUS_DRAFT, c.STATUS_SUBMITTED, c.STATUS_IN_REVIEW, c.STATUS_MORE_INFO,
                       c.STATUS_APPROVED, c.STATUS_PAID):
            cards.append({"label": c.STATUS_LABELS[status], "value": counts.get(status, 0),
                          "params": {"status": status}})
    elif user.role == c.ROLE_OFFICER:
        waiting = db.session.scalar(
            select(func.count(Claim.id)).where(
                Claim.status == c.STATUS_SUBMITTED,
                (Claim.officer_user_id.is_(None)) | (Claim.officer_user_id == user.id),
            )
        )
        mine_review = db.session.scalar(select(func.count(Claim.id)).where(
            Claim.status == c.STATUS_IN_REVIEW, Claim.officer_user_id == user.id))
        mine_info = db.session.scalar(select(func.count(Claim.id)).where(
            Claim.status == c.STATUS_MORE_INFO, Claim.officer_user_id == user.id))
        cards = [
            {"label": "Waiting for a reviewer", "value": waiting, "endpoint": "officer.queue",
             "params": {"status": c.STATUS_SUBMITTED}},
            {"label": "My claims in review", "value": mine_review, "endpoint": "officer.queue",
             "params": {"status": c.STATUS_IN_REVIEW}},
            {"label": "Waiting for information", "value": mine_info, "endpoint": "officer.queue",
             "params": {"status": c.STATUS_MORE_INFO}},
        ]
    elif user.role == c.ROLE_ADMIN:
        cards += [
            {"label": "Approved, awaiting settlement", "value": counts.get(c.STATUS_APPROVED, 0),
             "params": {"status": c.STATUS_APPROVED}},
            {"label": "Paid (simulated)", "value": counts.get(c.STATUS_PAID, 0),
             "params": {"status": c.STATUS_PAID}},
            {"label": "User accounts", "value": db.session.scalar(select(func.count(User.id))),
             "endpoint": "admin.users"},
            {"label": "Hospitals", "value": db.session.scalar(select(func.count(Hospital.id))),
             "endpoint": "admin.hospitals"},
        ]
    return cards


def claims_report(user):
    """Return report rows (status, type, count, requested, eligible, simulated paid) and totals."""
    stmt = (
        select(
            Claim.status, Claim.claim_type, func.count(Claim.id),
            func.coalesce(func.sum(Claim.requested_amount), 0),
            func.coalesce(func.sum(Claim.eligible_amount), 0),
            func.coalesce(func.sum(Settlement.amount), 0),
        )
        .outerjoin(Settlement, Settlement.claim_id == Claim.id)
        .where(claim_filter(user))
        .group_by(Claim.status, Claim.claim_type)
    )
    rows = []
    totals = {"count": 0, "requested": Decimal("0"), "eligible": Decimal("0"), "paid": Decimal("0")}
    for status, claim_type, count, requested, eligible, paid in db.session.execute(stmt).all():
        row = {"status": status, "claim_type": claim_type, "count": count,
               "requested": Decimal(str(requested)), "eligible": Decimal(str(eligible)),
               "paid": Decimal(str(paid))}
        rows.append(row)
        totals["count"] += count
        for key in ("requested", "eligible", "paid"):
            totals[key] += row[key]
    order = {s: i for i, s in enumerate(c.CLAIM_STATUSES)}

    def sort_key(row):
        """Sort report rows by workflow order of status, then claim type."""
        return order.get(row["status"], 99), row["claim_type"]

    rows.sort(key=sort_key)
    return rows, totals
