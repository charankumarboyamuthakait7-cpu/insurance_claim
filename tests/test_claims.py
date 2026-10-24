"""Automated tests for the BRD acceptance criteria (section 11 - Testing)."""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

import services as svc
from app import TestingConfig, create_app
from models import Claim, ClaimHistory, Settlement, User, db
from seed import seed_database

PAST_START, PAST_END = "2026-03-15", "2026-03-18"


def claim_form(**overrides):
    """Return valid reimbursement form data for holder1 / policy 1, with overrides."""
    data = {
        "policy_id": "1", "claim_type": "reimbursement", "member_id": "1", "hospital_id": "2",
        "treatment_type": "inpatient", "admission_date": PAST_START, "discharge_date": PAST_END,
        "requested_amount": "50000", "description": "Synthetic test claim",
        "doc_hospital_bill": "received", "doc_discharge_summary": "received", "action": "submit",
    }
    data.update(overrides)
    return data


def claim_count(app):
    """Return the number of claims in the database."""
    with app.app_context():
        return db.session.scalar(select(func.count(Claim.id)))


def get_claim(app, claim_id):
    """Return (status, officer_id) for a claim."""
    with app.app_context():
        claim = db.session.get(Claim, claim_id)
        return claim.status, claim.officer_user_id


def new_claim_id(response):
    """Extract the claim id from a redirect to /claims/<id>."""
    assert response.status_code == 302, response.get_data(as_text=True)[:500]
    return int(response.headers["Location"].rstrip("/").split("/")[-1])


# ------------------------------------------------------------ authentication
def test_login_logout_and_invalid_credentials(client):
    """Valid login redirects, bad password shows a clear message, logout ends the session."""
    bad = client.post("/login", data={"username": "holder1", "password": "wrong"})
    assert bad.status_code == 401 and b"Invalid username or password" in bad.data
    ok = client.post("/login", data={"username": "holder1", "password": "Demo@12345"})
    assert ok.status_code == 302
    assert client.get("/policyholder/").status_code == 200
    client.post("/logout")
    assert client.get("/policyholder/").status_code == 302


def test_passwords_are_salted_hashes(app):
    """Stored passwords are hashes, never the plain text, and differ per user."""
    with app.app_context():
        hashes = db.session.scalars(select(User.password_hash)).all()
    assert all("Demo@12345" not in h for h in hashes)
    assert len(set(hashes)) == len(hashes)


def test_anonymous_and_wrong_role_pages_denied(client, login_as):
    """Direct URLs are protected even when no button points to them."""
    assert client.get("/claims").status_code == 302
    holder = login_as("holder1")
    assert holder.get("/admin/users").status_code == 403
    assert holder.get("/officer/queue").status_code == 403


# ----------------------------------------------------- row-level access / forged POST
def test_direct_url_to_another_holders_claim_denied(login_as):
    """A policyholder cannot open someone else's claim by typing its URL."""
    assert login_as("holder2").get("/claims/1001").status_code == 403
    assert login_as("holder1").get("/claims/1001").status_code == 200


def test_forged_posts_are_denied(login_as):
    """Role and ownership are enforced on the server for hidden buttons."""
    assert login_as("holder1").post("/officer/claims/1004/approve", data={"reason": "x" * 10}).status_code == 403
    assert login_as("desk_lakeview").post("/admin/claims/1010/settle", data={"amount": "16200"}).status_code == 403
    assert login_as("holder1").post("/policyholder/claims/1002/submit").status_code == 403


def test_hospital_desk_sees_only_its_own_cashless_claims(login_as):
    """Hospital desk scope comes from users.hospital_id, not from query parameters."""
    desk = login_as("desk_sunrise")
    page = desk.get("/claims?hospital_id=2").get_data(as_text=True)
    assert "CLM-1004" not in page and "CLM-1010" not in page
    assert "CLM-1004" in desk.get("/claims").get_data(as_text=True)
    assert desk.get("/claims/1010").status_code == 403


# ------------------------------------------------------------ claim creation rules
def test_member_from_another_policy_rejected(app, login_as):
    """Cross-policy member submissions are refused and nothing is created."""
    before = claim_count(app)
    response = login_as("holder1").post("/policyholder/claims/new", data=claim_form(member_id="4"))
    assert response.status_code == 422
    assert b"not covered by the chosen policy" in response.data
    assert claim_count(app) == before


def test_other_holders_policy_rejected(app, login_as):
    """A policyholder cannot create a claim on a policy they do not own."""
    before = claim_count(app)
    response = login_as("holder2").post("/policyholder/claims/new", data=claim_form())
    assert response.status_code == 422 and claim_count(app) == before


def test_hospital_scope_and_network_rules(app, login_as):
    """Hospital desk may only use its own hospital, and cashless needs a network hospital."""
    before = claim_count(app)
    base = claim_form(policy_id="3", member_id="4", claim_type="cashless")
    other = login_as("desk_sunrise").post("/hospital/claims/new", data={**base, "hospital_id": "2"})
    assert other.status_code == 422 and b"own hospital" in other.data
    non_network = login_as("desk_harbor").post("/hospital/claims/new", data={**base, "hospital_id": "4"})
    assert non_network.status_code == 422 and b"network hospital" in non_network.data
    forged_type = login_as("desk_sunrise").post("/hospital/claims/new", data={**base, "hospital_id": "1", "claim_type": "reimbursement"})
    assert forged_type.status_code == 422
    assert claim_count(app) == before
    ok = login_as("desk_sunrise").post("/hospital/claims/new", data={**base, "hospital_id": "1"})
    assert ok.status_code == 302


def test_invalid_inputs_never_create_claims(app, login_as):
    """Blank, malformed, negative, future and reversed-date inputs are all rejected."""
    holder = login_as("holder1")
    before = claim_count(app)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    bad_inputs = [
        {}, claim_form(requested_amount="-5"), claim_form(requested_amount="abc"),
        claim_form(requested_amount="10.999"), claim_form(requested_amount="NaN"),
        claim_form(admission_date=tomorrow, discharge_date=tomorrow),
        claim_form(admission_date="2026-03-20", discharge_date="2026-03-10"),
        claim_form(treatment_type="outpatient"), claim_form(description=""),
        claim_form(hospital_id="999"), claim_form(claim_type="cashless"),
    ]
    for data in bad_inputs:
        assert holder.post("/policyholder/claims/new", data=data).status_code == 422, data
    assert claim_count(app) == before


def test_draft_can_be_saved_but_future_admission_cannot_be_submitted(app, login_as):
    """Drafts are allowed; submitting needs a valid, non-future admission date."""
    holder = login_as("holder1")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    draft = holder.post("/policyholder/claims/new", data=claim_form(action="draft", admission_date=tomorrow, discharge_date=tomorrow))
    claim_id = new_claim_id(draft)
    assert get_claim(app, claim_id)[0] == "draft"
    submit = holder.post(f"/policyholder/claims/{claim_id}/submit")
    assert submit.status_code == 422 and get_claim(app, claim_id)[0] == "draft"


def test_checklist_changes_are_audited(app, login_as):
    """Checklist edits on drafts/more_info create history events; other states refuse them."""
    holder = login_as("holder1")
    response = holder.post("/policyholder/claims/1003/checklist", data={"doc_hospital_bill": "received", "doc_discharge_summary": "missing"})
    assert response.status_code == 302
    with app.app_context():
        events = db.session.scalars(select(ClaimHistory).where(ClaimHistory.claim_id == 1003, ClaimHistory.event_type == "checklist")).all()
        assert len(events) == 1 and "Hospital bill" in events[0].note
    assert holder.post("/policyholder/claims/1013/checklist", data={"doc_hospital_bill": "missing"}).status_code == 422


# ------------------------------------------------------------- workflow
def test_full_workflow_reimbursement(app, login_as):
    """draft→submitted→in_review→more_info→submitted→in_review→approved→paid with history."""
    holder, officer, admin = login_as("holder1"), login_as("officer1"), login_as("admin")
    claim_id = new_claim_id(holder.post("/policyholder/claims/new", data=claim_form(doc_hospital_bill="missing")))
    assert officer.post(f"/officer/claims/{claim_id}/take").status_code == 302
    info = officer.post(f"/officer/claims/{claim_id}/request-info", data={"reason": "Please attach the bill", "document_types": ["hospital_bill"]})
    assert info.status_code == 302 and get_claim(app, claim_id)[0] == "more_info"
    assert holder.post(f"/policyholder/claims/{claim_id}/respond", data={"note": ""}).status_code == 422
    assert holder.post(f"/policyholder/claims/{claim_id}/respond", data={"note": "Bill is now available"}).status_code == 302
    assert officer.post(f"/officer/claims/{claim_id}/take").status_code == 302
    approve = officer.post(f"/officer/claims/{claim_id}/approve", data={"eligible_amount": "40000", "reason": "Reviewed bill and policy"})
    assert approve.status_code == 302 and get_claim(app, claim_id)[0] == "approved"
    detail = admin.get(f"/claims/{claim_id}").get_data(as_text=True)
    assert "₹34,200.00" in detail
    assert admin.post(f"/admin/claims/{claim_id}/settle", data={"amount": "34200.00"}).status_code == 302
    with app.app_context():
        claim = db.session.get(Claim, claim_id)
        assert claim.status == "paid" and claim.settlement.payee_type == "policyholder"
        assert claim.settlement.amount == Decimal("34200.00")
        assert [h.to_status for h in claim.history if h.event_type == "status_change"] == [
            "draft", "submitted", "in_review", "more_info", "submitted", "in_review", "approved", "paid"]
        assert all(h.actor_user_id for h in claim.history)


def test_invalid_transitions_are_refused(app, login_as):
    """Skipping states or acting on terminal claims never changes the status."""
    officer, admin, holder = login_as("officer1"), login_as("admin"), login_as("holder1")
    assert officer.post("/officer/claims/1013/approve", data={"eligible_amount": "1000", "reason": "valid reason"}).status_code == 422
    assert admin.post("/admin/claims/1004/settle", data={"amount": "100"}).status_code == 422
    assert holder.post("/policyholder/claims/1001/submit").status_code == 422
    assert officer.post("/officer/claims/1009/reject", data={"reason": "reopen attempt"}).status_code == 422
    assert get_claim(app, 1013)[0] == "submitted" and get_claim(app, 1004)[0] == "in_review"
    assert get_claim(app, 1001)[0] == "paid" and get_claim(app, 1009)[0] == "rejected"


def test_two_officers_cannot_overwrite_assignment(app, login_as):
    """The second officer to take a claim gets a conflict message; assignment is unchanged."""
    first, second = login_as("officer1"), login_as("officer2")
    assert first.post("/officer/claims/1007/take").status_code == 302
    clash = second.post("/officer/claims/1007/take")
    assert clash.status_code == 422 and b"already being reviewed" in clash.data
    assert get_claim(app, 1007) == ("in_review", 2)
    assert second.post("/officer/claims/1007/approve", data={"eligible_amount": "100", "reason": "not mine"}).status_code == 403


def test_decision_validation_rules(app, login_as):
    """Approval needs 0 < eligible <= requested and a reason; rejection needs a reason."""
    officer = login_as("officer1")  # officer1 owns claim 1004 (in_review)
    for data in ({"eligible_amount": "999999", "reason": "too much"}, {"eligible_amount": "0", "reason": "zero"},
                 {"eligible_amount": "abc", "reason": "text"}, {"eligible_amount": "1000", "reason": ""}):
        assert officer.post("/officer/claims/1004/approve", data=data).status_code == 422, data
    assert officer.post("/officer/claims/1004/reject", data={"reason": ""}).status_code == 422
    assert get_claim(app, 1004)[0] == "in_review"
    assert officer.post("/officer/claims/1004/reject", data={"reason": "Documents do not support the claim"}).status_code == 302


def test_remaining_cover_limits_approval(app, login_as):
    """Policy 2 has ₹68,000 simulated cover left, so a larger eligible amount is refused."""
    officer = login_as("officer1")
    assert officer.post("/officer/claims/1013/take").status_code == 302
    too_much = officer.post("/officer/claims/1013/approve", data={"eligible_amount": "100000", "reason": "over the limit"})
    assert too_much.status_code == 422 and b"remaining simulated cover" in too_much.data
    assert officer.post("/officer/claims/1013/approve", data={"eligible_amount": "68000", "reason": "up to the limit"}).status_code == 302


def test_policy_period_and_treatment_rules_block_approval(login_as):
    """Out-of-period admissions and uncovered treatment types must be rejected, not approved."""
    officer = login_as("officer1")
    officer.post("/officer/claims/1002/take")
    period = officer.post("/officer/claims/1002/approve", data={"eligible_amount": "1000", "reason": "try"})
    assert period.status_code == 422 and b"outside the active policy period" in period.data
    officer.post("/officer/claims/1005/take")
    treatment = officer.post("/officer/claims/1005/approve", data={"eligible_amount": "1000", "reason": "try"})
    assert treatment.status_code == 422 and b"does not cover daycare" in treatment.data


def test_duplicate_warning_shown_to_officer(login_as):
    """Same member/hospital/dates/type raises a visible warning but does not block."""
    officer = login_as("officer1")
    page = officer.get("/claims/1007").get_data(as_text=True)
    assert "Potential duplicate" in page and "CLM-1008" in page


# ------------------------------------------------------------- settlement & maths
def test_payout_formula_uses_decimals():
    """BRD example: eligible ₹20,000, deductible ₹2,000, co-pay 10% → ₹16,200.00."""
    assert svc.compute_payout(Decimal("20000"), Decimal("2000"), Decimal("10")) == Decimal("16200.00")
    assert svc.compute_payout(Decimal("1500"), Decimal("2000"), Decimal("10")) == Decimal("0.00")
    assert svc.compute_payout(Decimal("100.10"), Decimal("0"), Decimal("33.33")) == Decimal("66.74")


def test_settlement_amount_payee_and_single_record(app, login_as):
    """Settlement must equal the computed payout; cashless pays the hospital; only once."""
    admin = login_as("admin")
    assert admin.post("/admin/claims/1010/settle", data={"amount": "20000"}).status_code == 422
    assert admin.post("/admin/claims/1010/settle", data={"amount": "16200.00"}).status_code == 302
    with app.app_context():
        settlement = db.session.scalar(select(Settlement).where(Settlement.claim_id == 1010))
        assert settlement.payee_type == "hospital" and settlement.amount == Decimal("16200.00")
    assert admin.post("/admin/claims/1010/settle", data={"amount": "16200.00"}).status_code == 422


# ------------------------------------------------------------- search / reports
def test_combined_search_filters_and_empty_state(login_as):
    """Search + status + type + hospital combine; reset shows all; empty results explain."""
    officer = login_as("officer1")
    hit = officer.get("/officer/queue?q=Hari&status=submitted&claim_type=reimbursement&hospital_id=3").get_data(as_text=True)
    assert "CLM-1007" in hit and "CLM-1008" in hit and "CLM-1002" not in hit
    by_id = officer.get("/officer/queue?q=CLM-1002").get_data(as_text=True)
    assert "CLM-1002" in by_id and "CLM-1007" not in by_id
    assert "No records match" in officer.get("/officer/queue?q=nobody").get_data(as_text=True)
    assert "CLM-1002" in officer.get("/officer/queue").get_data(as_text=True)


def test_search_never_leaks_other_holders_claims(login_as):
    """Changing query parameters cannot widen a policyholder's scope."""
    page = login_as("holder1").get("/claims?q=Hari").get_data(as_text=True)
    assert "CLM-1007" not in page and "No records match" in page


def test_reports_are_scoped_per_role(app):
    """Report totals count only the claims the viewer may see."""
    with app.app_context():
        holder = db.session.scalar(select(User).where(User.username == "holder1"))
        admin = db.session.scalar(select(User).where(User.username == "admin"))
        officer = db.session.scalar(select(User).where(User.username == "officer1"))
        assert svc.claims_report(holder)[1]["count"] == 6
        assert svc.claims_report(officer)[1]["count"] == 12  # drafts excluded
        rows, totals = svc.claims_report(admin)
        assert totals["count"] == 13 and totals["paid"] == Decimal("275200.00")


# ------------------------------------------------------------- admin protections
def test_admin_safeguards(app, login_as):
    """Admins cannot lock themselves out; network status is protected by open claims."""
    admin = login_as("admin")
    own = admin.post("/admin/users/1/edit", data={"display_name": "Asha Admin", "role": "administrator", "password": ""})
    assert own.status_code == 422  # is_active unchecked → would deactivate self
    network = admin.post("/admin/hospitals/1/edit", data={"name": "Sunrise General Hospital", "city": "Hyderabad"})
    assert network.status_code == 422 and b"open cashless claim" in network.data


def test_admin_creates_user_with_hashed_password(app, login_as, client):
    """New accounts store a hash and can log in."""
    admin = login_as("admin")
    response = admin.post("/admin/users", data={
        "username": "newholder", "display_name": "New Holder", "role": "policyholder",
        "password": "Strong1234", "is_active": "on"})
    assert response.status_code == 302
    with app.app_context():
        user = db.session.scalar(select(User).where(User.username == "newholder"))
        assert user.password_hash != "Strong1234"
    assert client.post("/login", data={"username": "newholder", "password": "Strong1234"}).status_code == 302
    weak = admin.post("/admin/users", data={"username": "weak", "display_name": "Weak", "role": "policyholder", "password": "short", "is_active": "on"})
    assert weak.status_code == 422


# ------------------------------------------------------------- CSRF & persistence
def test_csrf_protection_blocks_forged_forms():
    """With CSRF enabled, a POST without a token is rejected."""

    class CsrfConfig(TestingConfig):
        """Testing config with CSRF checks switched on."""

        WTF_CSRF_ENABLED = True

    application = create_app(CsrfConfig)
    with application.app_context():
        seed_database()
    response = application.test_client().post("/login", data={"username": "holder1", "password": "Demo@12345"})
    assert response.status_code == 400
    with application.app_context():
        db.drop_all()


def test_data_survives_app_restart(tmp_path):
    """A second application instance on the same database file sees committed changes."""
    uri = f"sqlite:///{tmp_path / 'claims.db'}"

    class FileConfig(TestingConfig):
        """Testing config backed by a file database."""

        SQLALCHEMY_DATABASE_URI = uri

    first = create_app(FileConfig)
    with first.app_context():
        seed_database()
    client = first.test_client()
    client.post("/login", data={"username": "officer1", "password": "Demo@12345"})
    assert client.post("/officer/claims/1007/take").status_code == 302
    with first.app_context():
        db.session.remove()
        db.engine.dispose()

    second = create_app(FileConfig)
    with second.app_context():
        claim = db.session.get(Claim, 1007)
        assert claim.status == "in_review" and claim.officer_user_id == 2
        assert len(claim.history) == 3
        db.session.remove()
        db.engine.dispose()
