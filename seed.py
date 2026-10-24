"""Synthetic seed data (no real people, medical records, phone, Aadhaar or bank details).

Import order follows the BRD: hospitals → plans → users → policies → members →
claims → claim_documents → claim_history → settlements. Claim IDs 1001-1013
create deliberate learning situations (see README).
"""
from datetime import date, timedelta
from decimal import Decimal

from flask import current_app
from sqlalchemy import func, select

import models as c
from app import format_inr
from models import (
    Claim, ClaimDocument, ClaimHistory, Hospital, Member, Plan, Policy, Settlement, User,
    db, utcnow,
)
from services import compute_payout

ADMIN, OFFICER1, OFFICER2, OFFICER3 = 1, 2, 3, 4
DESK_SUNRISE, DESK_LAKEVIEW, DESK_GREENFIELD, DESK_HARBOR, DESK_MAPLE = 5, 6, 7, 8, 9
HOLDER1, HOLDER2, HOLDER3, HOLDER4, HOLDER5, HOLDER6 = 10, 11, 12, 13, 14, 15


def _seed_reference():
    """Insert hospitals, plans, users, policies and members."""
    for hid, name, city, network in [
        (1, "Sunrise General Hospital", "Hyderabad", True),
        (2, "Lakeview Medical Centre", "Hyderabad", True),
        (3, "Greenfield Care Hospital", "Pune", True),
        (4, "Harbor City Hospital", "Chennai", False),
        (5, "Maple Grove Clinic", "Bengaluru", True),
    ]:
        db.session.add(Hospital(id=hid, name=name, city=city, network_status=network))
    for pid, name, types, limit, ded, copay in [
        (1, "Silver Shield", "inpatient", 300000, 5000, 20),
        (2, "Gold Guard", "daycare,inpatient", 500000, 2000, 10),
        (3, "Platinum Plus", "daycare,inpatient", 1000000, 1000, 0),
    ]:
        db.session.add(Plan(id=pid, name=name, covered_treatment_types=types, annual_limit=Decimal(limit),
                            deductible=Decimal(ded), copay_percent=Decimal(copay)))
    db.session.flush()

    demo_password = current_app.config["DEMO_PASSWORD"]
    users = [
        (ADMIN, "admin", "Asha Admin", c.ROLE_ADMIN, None),
        (OFFICER1, "officer1", "Ravi Officer", c.ROLE_OFFICER, None),
        (OFFICER2, "officer2", "Meera Officer", c.ROLE_OFFICER, None),
        (OFFICER3, "officer3", "Karan Officer", c.ROLE_OFFICER, None),
        (DESK_SUNRISE, "desk_sunrise", "Sunrise Desk", c.ROLE_HOSPITAL, 1),
        (DESK_LAKEVIEW, "desk_lakeview", "Lakeview Desk", c.ROLE_HOSPITAL, 2),
        (DESK_GREENFIELD, "desk_greenfield", "Greenfield Desk", c.ROLE_HOSPITAL, 3),
        (DESK_HARBOR, "desk_harbor", "Harbor Desk (non-network)", c.ROLE_HOSPITAL, 4),
        (DESK_MAPLE, "desk_maple", "Maple Desk", c.ROLE_HOSPITAL, 5),
        (HOLDER1, "holder1", "Arun Demo", c.ROLE_POLICYHOLDER, None),
        (HOLDER2, "holder2", "Bela Sample", c.ROLE_POLICYHOLDER, None),
        (HOLDER3, "holder3", "Dev Placeholder", c.ROLE_POLICYHOLDER, None),
        (HOLDER4, "holder4", "Esha Example", c.ROLE_POLICYHOLDER, None),
        (HOLDER5, "holder5", "Gita Fictional", c.ROLE_POLICYHOLDER, None),
        (HOLDER6, "holder6", "Hari Synthetic", c.ROLE_POLICYHOLDER, None),
    ]
    for uid, username, name, role, hospital_id in users:
        user = User(id=uid, username=username, display_name=name, role=role, hospital_id=hospital_id)
        user.set_password(demo_password)
        db.session.add(user)
    db.session.flush()

    y26 = (date(2026, 1, 1), date(2026, 12, 31))
    for pid, holder, plan, (start, end), status in [
        (1, HOLDER1, 2, y26, c.POLICY_ACTIVE), (2, HOLDER1, 1, y26, c.POLICY_ACTIVE),
        (3, HOLDER2, 3, y26, c.POLICY_ACTIVE),
        (4, HOLDER3, 1, (date(2024, 1, 1), date(2024, 12, 31)), c.POLICY_EXPIRED),
        (5, HOLDER4, 2, y26, c.POLICY_ACTIVE), (6, HOLDER5, 1, y26, c.POLICY_ACTIVE),
        (7, HOLDER6, 3, y26, c.POLICY_ACTIVE),
        (8, HOLDER6, 2, (date(2025, 1, 1), date(2025, 12, 31)), c.POLICY_CANCELLED),
    ]:
        db.session.add(Policy(id=pid, holder_user_id=holder, plan_id=plan, start_date=start,
                              end_date=end, status=status))
    db.session.flush()
    for mid, pid, name, rel in [
        (1, 1, "Arun Demo", "Self"), (2, 1, "Lata Demo", "Spouse"), (3, 2, "Arun Demo", "Self"),
        (4, 3, "Bela Sample", "Self"), (5, 3, "Chitra Sample", "Child"),
        (6, 4, "Dev Placeholder", "Self"), (7, 5, "Esha Example", "Self"),
        (8, 5, "Farid Example", "Spouse"), (9, 6, "Gita Fictional", "Self"),
        (10, 7, "Hari Synthetic", "Self"), (11, 8, "Indu Synthetic", "Self"),
    ]:
        db.session.add(Member(id=mid, policy_id=pid, display_name=name, relationship_to_holder=rel))
    db.session.flush()


def _events(creator, officer, final, note=None):
    """Build the standard history path ending at ``final`` as (actor, from, to, note) tuples."""
    s = c
    path = [(creator, None, s.STATUS_DRAFT, "Draft created")]
    if final == s.STATUS_DRAFT:
        return path
    path.append((creator, s.STATUS_DRAFT, s.STATUS_SUBMITTED, "Submitted by creator"))
    if final == s.STATUS_SUBMITTED:
        return path
    path.append((officer, s.STATUS_SUBMITTED, s.STATUS_IN_REVIEW, "Assigned to officer and review started"))
    if final == s.STATUS_IN_REVIEW:
        return path
    path.append((officer, s.STATUS_IN_REVIEW, final, note))
    if final == s.STATUS_PAID:
        path.insert(-1, (officer, s.STATUS_IN_REVIEW, s.STATUS_APPROVED, "Approved after review"))
        path[-1] = (ADMIN, s.STATUS_APPROVED, s.STATUS_PAID, note)
    return path


def _add_claim(spec):
    """Create one claim with checklist rows and history from a spec dict."""
    plan = db.session.get(Policy, spec["policy"]).plan
    claim = Claim(
        id=spec["id"], claim_type=spec["type"], policy_id=spec["policy"], member_id=spec["member"],
        hospital_id=spec["hospital"], treatment_type=spec["treatment"], admission_date=spec["adm"],
        discharge_date=spec["dis"], requested_amount=Decimal(spec["requested"]),
        description=spec.get("description", "Synthetic demo claim"), status=spec["status"],
        created_by_user_id=spec["creator"], officer_user_id=spec.get("officer"),
        decision_reason=spec.get("reason"),
    )
    bill, summary = spec.get("docs", (c.DOC_RECEIVED, c.DOC_RECEIVED))
    claim.documents = [
        ClaimDocument(document_type=c.DOC_HOSPITAL_BILL, checklist_status=bill,
                      info_requested=spec["status"] == c.STATUS_MORE_INFO and bill == c.DOC_MISSING),
        ClaimDocument(document_type=c.DOC_DISCHARGE_SUMMARY, checklist_status=summary),
    ]
    if spec.get("eligible") is not None:
        eligible = Decimal(spec["eligible"])
        claim.eligible_amount = eligible
        claim.payout_amount = compute_payout(eligible, plan.deductible, plan.copay_percent)
        claim.deductible_snapshot, claim.copay_snapshot = plan.deductible, plan.copay_percent
    moment = utcnow() - timedelta(days=spec.get("age", 10))
    claim.created_at = moment
    for actor, previous, new, note in spec["events"]:
        moment += timedelta(hours=3)
        if new == c.STATUS_SUBMITTED and claim.submitted_at is None:
            claim.submitted_at = moment
        claim.history.append(ClaimHistory(actor_user_id=actor, from_status=previous, to_status=new,
                                          note=note, timestamp=moment))
    claim.updated_at = moment
    db.session.add(claim)
    db.session.flush()
    if spec["status"] == c.STATUS_PAID:
        payee = c.PAYEE_HOSPITAL if claim.claim_type == c.CLAIM_CASHLESS else c.PAYEE_POLICYHOLDER
        db.session.add(Settlement(claim_id=claim.id, payee_type=payee, amount=claim.payout_amount,
                                  recorded_by=ADMIN, recorded_at=moment))


def _seed_claims():
    """Insert the 13 demo claims that drive the learning scenarios."""
    R, CL = c.CLAIM_REIMBURSEMENT, c.CLAIM_CASHLESS
    I, D = c.TREATMENT_INPATIENT, c.TREATMENT_DAYCARE

    def paid_note(amount):
        """Return the history note used for a seeded simulated settlement."""
        return f"Simulated settlement of {format_inr(amount)} recorded"

    specs = [
        dict(id=1001, type=R, policy=1, member=1, hospital=2, treatment=I, adm=date(2026, 2, 10),
             dis=date(2026, 2, 14), requested="60000", eligible="50000", status=c.STATUS_PAID,
             creator=HOLDER1, officer=OFFICER1, reason="Bill reviewed; partial amount eligible.", age=60,
             events=_events(HOLDER1, OFFICER1, c.STATUS_PAID, paid_note(Decimal("43200.00")))),
        dict(id=1002, type=R, policy=4, member=6, hospital=1, treatment=I, adm=date(2026, 5, 2),
             dis=date(2026, 5, 6), requested="40000", status=c.STATUS_SUBMITTED, creator=HOLDER3,
             docs=(c.DOC_MISSING, c.DOC_RECEIVED), age=20,
             events=_events(HOLDER3, None, c.STATUS_SUBMITTED)),
        dict(id=1003, type=R, policy=1, member=2, hospital=1, treatment=D, adm=date(2026, 9, 20),
             dis=date(2026, 9, 20), requested="15000", status=c.STATUS_DRAFT, creator=HOLDER1,
             docs=(c.DOC_MISSING, c.DOC_MISSING), age=5, events=_events(HOLDER1, None, c.STATUS_DRAFT)),
        dict(id=1004, type=CL, policy=3, member=4, hospital=1, treatment=I, adm=date(2026, 8, 1),
             dis=date(2026, 8, 4), requested="120000", status=c.STATUS_IN_REVIEW, creator=DESK_SUNRISE,
             officer=OFFICER1, age=15, events=_events(DESK_SUNRISE, OFFICER1, c.STATUS_IN_REVIEW)),
        dict(id=1005, type=R, policy=6, member=9, hospital=3, treatment=D, adm=date(2026, 6, 15),
             dis=date(2026, 6, 15), requested="18000", status=c.STATUS_SUBMITTED, creator=HOLDER5,
             docs=(c.DOC_RECEIVED, c.DOC_MISSING), age=18, events=_events(HOLDER5, None, c.STATUS_SUBMITTED)),
        dict(id=1006, type=R, policy=5, member=7, hospital=2, treatment=I, adm=date(2026, 7, 5),
             dis=date(2026, 7, 9), requested="85000", status=c.STATUS_MORE_INFO, creator=HOLDER4,
             officer=OFFICER3, docs=(c.DOC_MISSING, c.DOC_RECEIVED), age=25,
             events=_events(HOLDER4, OFFICER3, c.STATUS_MORE_INFO,
                            "Requested: Hospital bill. Reason: Bill copy is not legible.")),
        dict(id=1007, type=R, policy=7, member=10, hospital=3, treatment=I, adm=date(2026, 7, 10),
             dis=date(2026, 7, 12), requested="30000", status=c.STATUS_SUBMITTED, creator=HOLDER6, age=12,
             events=_events(HOLDER6, None, c.STATUS_SUBMITTED)),
        dict(id=1008, type=R, policy=7, member=10, hospital=3, treatment=I, adm=date(2026, 7, 10),
             dis=date(2026, 7, 12), requested="30000", status=c.STATUS_SUBMITTED, creator=HOLDER6, age=11,
             events=_events(HOLDER6, None, c.STATUS_SUBMITTED)),
        dict(id=1009, type=R, policy=2, member=3, hospital=1, treatment=I, adm=date(2026, 3, 3),
             dis=date(2026, 3, 5), requested="22000", status=c.STATUS_REJECTED, creator=HOLDER1,
             officer=OFFICER1, reason="Hospital bill does not match the claimed admission dates.", age=45,
             events=_events(HOLDER1, OFFICER1, c.STATUS_REJECTED,
                            "Rejected. Reason: Hospital bill does not match the claimed admission dates.")),
        dict(id=1010, type=CL, policy=1, member=2, hospital=2, treatment=D, adm=date(2026, 9, 1),
             dis=date(2026, 9, 1), requested="25000", eligible="20000", status=c.STATUS_APPROVED,
             creator=DESK_LAKEVIEW, officer=OFFICER1, reason="Eligible amount reduced after bill review.",
             age=8, events=_events(DESK_LAKEVIEW, OFFICER1, c.STATUS_APPROVED,
                                   "Approved. Eligible ₹20,000.00; simulated payout ₹16,200.00. "
                                   "Reason: Eligible amount reduced after bill review.")),
        dict(id=1011, type=CL, policy=5, member=8, hospital=5, treatment=I, adm=date(2026, 9, 10),
             dis=date(2026, 9, 12), requested="60000", status=c.STATUS_SUBMITTED, creator=DESK_MAPLE,
             docs=(c.DOC_RECEIVED, c.DOC_MISSING), age=6, events=_events(DESK_MAPLE, None, c.STATUS_SUBMITTED)),
        dict(id=1012, type=R, policy=2, member=3, hospital=3, treatment=I, adm=date(2026, 4, 1),
             dis=date(2026, 4, 10), requested="310000", eligible="295000", status=c.STATUS_PAID,
             creator=HOLDER1, officer=OFFICER3, reason="Large claim reviewed; most of the bill eligible.", age=90,
             events=_events(HOLDER1, OFFICER3, c.STATUS_PAID, paid_note(Decimal("232000.00")))),
        dict(id=1013, type=R, policy=2, member=3, hospital=2, treatment=I, adm=date(2026, 10, 1),
             dis=date(2026, 10, 5), requested="150000", status=c.STATUS_SUBMITTED, creator=HOLDER1, age=3,
             events=_events(HOLDER1, None, c.STATUS_SUBMITTED)),
    ]
    for spec in specs:
        _add_claim(spec)


def seed_database(reset=False):
    """Create tables and load the synthetic data; returns False if data already exists.

    Args:
        reset: drop every table first (destroys existing data).
    """
    if reset:
        db.drop_all()
    db.create_all()
    if db.session.scalar(select(func.count(User.id))):
        return False
    _seed_reference()
    _seed_claims()
    db.session.commit()
    return True
