"""Database and fixed values: constants, the ``db`` object and every ORM model.

All database access in the project goes through these models (no raw SQL).
Foreign keys, unique constraints, check constraints and the indexes required by
the BRD (claims(policy_id, status), claims(officer_user_id, status) and
claim_history(claim_id, timestamp)) are declared here.
"""
from datetime import datetime, timezone
from decimal import Decimal

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def utcnow():
    """Return the current UTC time as a naive datetime (stored as-is in MySQL)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ==========================================================================
# Fixed values: roles, claim states, labels
# ==========================================================================

ROLE_POLICYHOLDER = "policyholder"
ROLE_HOSPITAL = "hospital_desk"
ROLE_OFFICER = "claims_officer"
ROLE_ADMIN = "administrator"
ROLES = (ROLE_POLICYHOLDER, ROLE_HOSPITAL, ROLE_OFFICER, ROLE_ADMIN)
ROLE_LABELS = {
    ROLE_POLICYHOLDER: "Policyholder",
    ROLE_HOSPITAL: "Hospital desk",
    ROLE_OFFICER: "Claims officer",
    ROLE_ADMIN: "Administrator",
}

CLAIM_CASHLESS = "cashless"
CLAIM_REIMBURSEMENT = "reimbursement"
CLAIM_TYPES = (CLAIM_CASHLESS, CLAIM_REIMBURSEMENT)
CLAIM_TYPE_LABELS = {CLAIM_CASHLESS: "Cashless", CLAIM_REIMBURSEMENT: "Reimbursement"}
# Which claim type each submitting role may create.
CLAIM_TYPE_BY_ROLE = {
    ROLE_POLICYHOLDER: CLAIM_REIMBURSEMENT,
    ROLE_HOSPITAL: CLAIM_CASHLESS,
}

TREATMENT_INPATIENT = "inpatient"
TREATMENT_DAYCARE = "daycare"
TREATMENT_TYPES = (TREATMENT_INPATIENT, TREATMENT_DAYCARE)
TREATMENT_LABELS = {TREATMENT_INPATIENT: "Inpatient", TREATMENT_DAYCARE: "Daycare"}

STATUS_DRAFT = "draft"
STATUS_SUBMITTED = "submitted"
STATUS_IN_REVIEW = "in_review"
STATUS_MORE_INFO = "more_info"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_PAID = "paid"
CLAIM_STATUSES = (
    STATUS_DRAFT, STATUS_SUBMITTED, STATUS_IN_REVIEW, STATUS_MORE_INFO,
    STATUS_APPROVED, STATUS_REJECTED, STATUS_PAID,
)
STATUS_LABELS = {
    STATUS_DRAFT: "Draft",
    STATUS_SUBMITTED: "Submitted",
    STATUS_IN_REVIEW: "In review",
    STATUS_MORE_INFO: "More information needed",
    STATUS_APPROVED: "Approved",
    STATUS_REJECTED: "Rejected",
    STATUS_PAID: "Paid (simulated)",
}
TERMINAL_STATUSES = (STATUS_REJECTED, STATUS_PAID)
QUEUE_STATUSES = (STATUS_SUBMITTED, STATUS_IN_REVIEW, STATUS_MORE_INFO)

# (from_status, to_status) -> roles allowed to perform the move.
# Ownership/assignment conditions are enforced separately in the service layer.
ALLOWED_TRANSITIONS = {
    (STATUS_DRAFT, STATUS_SUBMITTED): (ROLE_POLICYHOLDER, ROLE_HOSPITAL),
    (STATUS_SUBMITTED, STATUS_IN_REVIEW): (ROLE_OFFICER,),
    (STATUS_IN_REVIEW, STATUS_MORE_INFO): (ROLE_OFFICER,),
    (STATUS_MORE_INFO, STATUS_SUBMITTED): (ROLE_POLICYHOLDER, ROLE_HOSPITAL),
    (STATUS_IN_REVIEW, STATUS_APPROVED): (ROLE_OFFICER,),
    (STATUS_IN_REVIEW, STATUS_REJECTED): (ROLE_OFFICER,),
    (STATUS_APPROVED, STATUS_PAID): (ROLE_ADMIN,),
}

DOC_HOSPITAL_BILL = "hospital_bill"
DOC_DISCHARGE_SUMMARY = "discharge_summary"
DOCUMENT_TYPES = (DOC_HOSPITAL_BILL, DOC_DISCHARGE_SUMMARY)
DOC_LABELS = {DOC_HOSPITAL_BILL: "Hospital bill", DOC_DISCHARGE_SUMMARY: "Discharge summary"}
DOC_RECEIVED = "received"
DOC_MISSING = "missing"
DOC_STATUSES = (DOC_RECEIVED, DOC_MISSING)

POLICY_ACTIVE = "active"
POLICY_EXPIRED = "expired"
POLICY_CANCELLED = "cancelled"
POLICY_STATUSES = (POLICY_ACTIVE, POLICY_EXPIRED, POLICY_CANCELLED)

PAYEE_HOSPITAL = "hospital"
PAYEE_POLICYHOLDER = "policyholder"
PAYEE_TYPES = (PAYEE_HOSPITAL, PAYEE_POLICYHOLDER)

EVENT_STATUS = "status_change"
EVENT_CHECKLIST = "checklist"
EVENT_EDIT = "edit"
EVENT_TYPES = (EVENT_STATUS, EVENT_CHECKLIST, EVENT_EDIT)

RELATIONSHIPS = ("Self", "Spouse", "Child", "Parent", "Other")


# ==========================================================================
# ORM models
# ==========================================================================

def _in_list(column, values):
    """Build a SQL ``column IN (...)`` string for a CheckConstraint."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Hospital(db.Model):
    """A hospital in the directory; ``network_status`` marks cashless eligibility."""

    __tablename__ = "hospitals"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    city = db.Column(db.String(80), nullable=False)
    network_status = db.Column(db.Boolean, nullable=False, default=False)

    users = db.relationship("User", back_populates="hospital")


class User(db.Model):
    """A login account with exactly one role; hospital desks link to a hospital."""

    __tablename__ = "users"
    __table_args__ = (
        db.CheckConstraint(_in_list("role", ROLES), name="ck_users_role"),
    )

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    hospital_id = db.Column(db.Integer, db.ForeignKey("hospitals.id"), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    hospital = db.relationship("Hospital", back_populates="users")

    def set_password(self, raw_password):
        """Store a salted hash of ``raw_password`` (the plain text is never kept)."""
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        """Return True when ``raw_password`` matches the stored salted hash."""
        return check_password_hash(self.password_hash, raw_password)

    @property
    def role_label(self):
        """Human-readable role name."""
        return ROLE_LABELS.get(self.role, self.role)


class Plan(db.Model):
    """A fictional insurance plan with limit, deductible, co-pay and covered treatments."""

    __tablename__ = "plans"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    covered_treatment_types = db.Column(db.String(100), nullable=False)
    annual_limit = db.Column(db.Numeric(12, 2), nullable=False)
    deductible = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    copay_percent = db.Column(db.Numeric(5, 2), nullable=False, default=Decimal("0.00"))

    policies = db.relationship("Policy", back_populates="plan")

    @property
    def covered_types(self):
        """Return the covered treatment types as a list of strings."""
        return [t for t in self.covered_treatment_types.split(",") if t]

    def covers(self, treatment_type):
        """Return True if ``treatment_type`` is listed in this plan."""
        return treatment_type in self.covered_types


class Policy(db.Model):
    """A policy held by one policyholder under one plan, covering many members."""

    __tablename__ = "policies"
    __table_args__ = (
        db.CheckConstraint(_in_list("status", POLICY_STATUSES), name="ck_policies_status"),
        db.CheckConstraint("end_date >= start_date", name="ck_policies_dates"),
    )

    id = db.Column(db.Integer, primary_key=True)
    holder_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plans.id"), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), nullable=False, default=POLICY_ACTIVE)

    holder = db.relationship("User", foreign_keys=[holder_user_id])
    plan = db.relationship("Plan", back_populates="policies")
    members = db.relationship("Member", back_populates="policy", order_by="Member.id")
    claims = db.relationship("Claim", back_populates="policy")

    @property
    def label(self):
        """Display reference such as ``POL-0003``."""
        return f"POL-{self.id:04d}"

    def covers_date(self, on_date):
        """Return True if the policy is active and ``on_date`` is within its dates (inclusive)."""
        return (
            self.status == POLICY_ACTIVE
            and self.start_date <= on_date <= self.end_date
        )


class Member(db.Model):
    """A person covered by a policy; claims may be raised for covered members only."""

    __tablename__ = "members"

    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policies.id"), nullable=False, index=True)
    display_name = db.Column(db.String(100), nullable=False)
    relationship_to_holder = db.Column("relationship", db.String(30), nullable=False)

    policy = db.relationship("Policy", back_populates="members")
    claims = db.relationship("Claim", back_populates="member")


class Claim(db.Model):
    """A claim linked to one policy, covered member, hospital and treatment event."""

    __tablename__ = "claims"
    __table_args__ = (
        db.CheckConstraint(_in_list("status", CLAIM_STATUSES), name="ck_claims_status"),
        db.CheckConstraint(_in_list("claim_type", CLAIM_TYPES), name="ck_claims_type"),
        db.CheckConstraint(_in_list("treatment_type", TREATMENT_TYPES), name="ck_claims_treatment"),
        db.CheckConstraint("requested_amount > 0", name="ck_claims_requested_positive"),
        db.CheckConstraint("discharge_date >= admission_date", name="ck_claims_dates"),
        db.Index("ix_claims_policy_status", "policy_id", "status"),
        db.Index("ix_claims_officer_status", "officer_user_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policies.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    hospital_id = db.Column(db.Integer, db.ForeignKey("hospitals.id"), nullable=False)
    claim_type = db.Column(db.String(20), nullable=False)
    treatment_type = db.Column(db.String(20), nullable=False)
    admission_date = db.Column(db.Date, nullable=False)
    discharge_date = db.Column(db.Date, nullable=False)
    requested_amount = db.Column(db.Numeric(12, 2), nullable=False)
    eligible_amount = db.Column(db.Numeric(12, 2), nullable=True)
    payout_amount = db.Column(db.Numeric(12, 2), nullable=True)
    deductible_snapshot = db.Column(db.Numeric(12, 2), nullable=True)
    copay_snapshot = db.Column(db.Numeric(5, 2), nullable=True)
    description = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_DRAFT)
    decision_reason = db.Column(db.String(1000), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    officer_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    submitted_at = db.Column(db.DateTime, nullable=True)

    policy = db.relationship("Policy", back_populates="claims")
    member = db.relationship("Member", back_populates="claims")
    hospital = db.relationship("Hospital")
    creator = db.relationship("User", foreign_keys=[created_by_user_id])
    officer = db.relationship("User", foreign_keys=[officer_user_id])
    documents = db.relationship(
        "ClaimDocument", back_populates="claim", cascade="all, delete-orphan",
        order_by="ClaimDocument.id",
    )
    history = db.relationship(
        "ClaimHistory", back_populates="claim", cascade="all, delete-orphan",
        order_by="ClaimHistory.timestamp, ClaimHistory.id",
    )
    settlement = db.relationship("Settlement", back_populates="claim", uselist=False)

    @property
    def label(self):
        """Display reference such as ``CLM-1004``."""
        return f"CLM-{self.id}"

    @property
    def age_days(self):
        """Whole days since first submission, or None for a claim never submitted."""
        if self.submitted_at is None:
            return None
        return (utcnow() - self.submitted_at).days

    def document_status(self, document_type):
        """Return the checklist status of ``document_type`` (defaults to missing)."""
        for doc in self.documents:
            if doc.document_type == document_type:
                return doc.checklist_status
        return DOC_MISSING

    @property
    def missing_documents(self):
        """List of document types whose checklist status is 'missing'."""
        return [d.document_type for d in self.documents if d.checklist_status == DOC_MISSING]


class ClaimDocument(db.Model):
    """Checklist metadata: whether a required document is received or missing."""

    __tablename__ = "claim_documents"
    __table_args__ = (
        db.UniqueConstraint("claim_id", "document_type", name="uq_claim_document"),
        db.CheckConstraint(_in_list("document_type", DOCUMENT_TYPES), name="ck_doc_type"),
        db.CheckConstraint(_in_list("checklist_status", DOC_STATUSES), name="ck_doc_status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("claims.id"), nullable=False, index=True)
    document_type = db.Column(db.String(30), nullable=False)
    checklist_status = db.Column(db.String(10), nullable=False, default=DOC_MISSING)
    info_requested = db.Column(db.Boolean, nullable=False, default=False)

    claim = db.relationship("Claim", back_populates="documents")


class ClaimHistory(db.Model):
    """One audit event for a claim: actor, previous/new state, note and timestamp."""

    __tablename__ = "claim_history"
    __table_args__ = (db.Index("ix_history_claim_timestamp", "claim_id", "timestamp"),)

    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("claims.id"), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    from_status = db.Column(db.String(20), nullable=True)
    to_status = db.Column(db.String(20), nullable=False)
    event_type = db.Column(db.String(20), nullable=False, default=EVENT_STATUS)
    note = db.Column(db.String(1000), nullable=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=utcnow)

    claim = db.relationship("Claim", back_populates="history")
    actor = db.relationship("User", foreign_keys=[actor_user_id])


class Settlement(db.Model):
    """A simulated payment event for an approved claim (at most one per claim)."""

    __tablename__ = "settlements"
    __table_args__ = (
        db.CheckConstraint(_in_list("payee_type", PAYEE_TYPES), name="ck_settlement_payee"),
    )

    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("claims.id"), nullable=False, unique=True)
    payee_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    recorded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    recorded_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    claim = db.relationship("Claim", back_populates="settlement")
    recorder = db.relationship("User", foreign_keys=[recorded_by])


class ClaimFile(db.Model):
    """Metadata for a securely stored supporting file attached to a claim."""
    __tablename__ = "claim_files"

    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("claims.id"), nullable=False, index=True)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(300), nullable=False, unique=True)
    uploaded_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=utcnow)
