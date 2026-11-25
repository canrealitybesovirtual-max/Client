"""Payments module: prompt payment, retention caps, lien waiver selection and stop payment notice deadlines.

Every expected value below is computed by hand in the comment next to it (calendar days, 2% per month with the partial
month prorated by its actual length, legal-rate interest = principal x 10% x days / 365).
"""
import asyncio
from datetime import date

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core
from tradecomply import payments as pm

AS_OF = "2026-11-25"


def ids(res):
    return {c.rule_id for c in res.citations}


# ---------------------------------------------------------------- prompt payment: B&P 7108.5 (GC -> sub)
def test_sub_paid_late_penalty_bp_7108_5():
    # GC received the owner's progress payment 2026-09-01 -> sub due 09-01 + 7 = 2026-09-08. Paid 2026-11-20.
    # Days late: Sep 8->Oct 8 (30) + Oct 8->Nov 8 (31) + Nov 8->Nov 20 (12) = 73.
    # Months: 2 full (to Nov 8) + 12 / 30 (Nov 8 -> Dec 8 is 30 days) = 2.4. Penalty = 10,000 x 2% x 2.4 = $480.00.
    # Every-started-month reading: 3 months x 2% x 10,000 = $600.00.
    r = pm.prompt_payment_check("private", "direct_contractor", "subcontractor", 10_000, None, "2026-09-01",
                                "2026-11-20", as_of=AS_OF)
    ob = r.obligations[0]
    assert ob.governing_law == "Business and Professions Code 7108.5"
    assert (ob.due_date, ob.status, ob.days_late) == ("2026-09-08", "paid_late", 73)
    assert ob.late_charge_usd == 480.00 and ob.late_charge_if_each_started_month_counts_usd == 600.00
    assert ob.late_charge_type == "penalty_percent_per_month"
    assert "2 full month(s) + 12/30" in ob.late_charge_math
    assert r.total_late_charges_usd == 480.00
    assert r.verified and ob.verified                     # 7108.5 deadline + penalty are confirmed rules
    assert {"bp-7108-5-pay-subs-7-days", "bp-7108-5-penalty-2pct"} <= ids(r)
    assert any("CSLB" in a and "$500.00 to $2,000.00" in a for a in r.what_to_send)
    assert all(li.rule_id or li.item.endswith("days late") for li in r.line_items)


def test_sub_good_faith_dispute_150_percent_and_not_yet_due():
    # $20,000 billed, $4,000 disputed: hold back min(20,000, 150% x 4,000 = 6,000) = $6,000, pay $14,000.
    # Received 2026-11-20 -> due 11-27; as of 11-25 -> not yet due, 2 days left.
    r = pm.prompt_payment_check("private", "direct_contractor", "subcontractor", 20_000, None, "2026-11-20", None,
                                disputed_amount_usd=4_000, as_of=AS_OF)
    ob = r.obligations[0]
    assert (ob.max_withhold_usd, ob.must_pay_by_due_date_usd) == (6_000.00, 14_000.00)
    assert (ob.status, ob.due_date, ob.days_until_due, ob.late_charge_usd) == ("not_yet_due", "2026-11-27", 2, 0.0)
    assert "bp-7108-5-dispute-150" in ob.rule_ids and r.verified
    # $15,000 disputed: 150% = 22,500 > 20,000 billed, so the whole $20,000 may be held.
    r2 = pm.prompt_payment_check("private", "subcontractor", "subcontractor", 20_000, None, "2026-11-20", None,
                                 disputed_amount_usd=15_000, as_of=AS_OF)
    assert (r2.obligations[0].max_withhold_usd, r2.obligations[0].must_pay_by_due_date_usd) == (20_000.0, 0.0)


def test_penalty_month_end_clamp():
    # Received 2027-01-24 -> due 01-31. Paid 2027-03-15. Jan 31 + 1 month = Feb 28 (clamped) <= Mar 15, so 1 full
    # month; Feb 28 -> Mar 15 = 15 days of the Feb 28 -> Mar 31 month (31 days). 1 + 15/31 = 1.483871 months.
    # $1,000 x 2% x 1.483871 = $29.68. Days late: Jan 31 -> Mar 15 = 28 + 15 = 43.
    r = pm.prompt_payment_check("public", "direct_contractor", "subcontractor", 1_000, None, "2027-01-24",
                                "2027-03-15", as_of="2027-03-20")
    ob = r.obligations[0]
    assert (ob.due_date, ob.days_late, ob.late_charge_usd) == ("2027-01-31", 43, 29.68)
    assert "1 full month(s) + 15/31" in ob.late_charge_math


def test_written_agreement_date_overrides_7_days():
    # Subcontract says pay 14 days after receipt (2026-10-15); statute alone would be 10-01 + 7 = 10-08. Paid 10-14.
    r = pm.prompt_payment_check("private", "direct_contractor", "subcontractor", 8_000, "2026-10-15", "2026-10-01",
                                "2026-10-14", as_of=AS_OF)
    ob = r.obligations[0]
    assert (ob.statutory_due_date, ob.due_date, ob.status) == ("2026-10-08", "2026-10-15", "paid_on_time")
    assert "agreed in writing" in ob.due_date_basis and ob.late_charge_usd == 0


def test_state_contract_sub_uses_pcc_10262_no_penalty():
    # State agency job: PCC 10262 displaces B&P 7108.5. Received 11-10 -> due 11-17; unpaid at 11-25 -> 8 days late.
    r = pm.prompt_payment_check("public", "direct_contractor", "subcontractor", 5_000, None, "2026-11-10", None,
                                as_of=AS_OF, public_agency="state")
    ob = r.obligations[0]
    assert ob.governing_law == "Public Contract Code 10262"
    assert (ob.due_date, ob.status, ob.days_late, ob.late_charge_usd, ob.late_charge_type) == (
        "2026-11-17", "overdue", 8, 0.0, "none")
    assert "state-contract-pay-subs-7-days" in ids(r) and "bp-7108-5-penalty-2pct" not in ids(r)
    assert any("Contractors State License Board" in n for n in ob.notes)
    # A subcontractor paying its own sub on the same state job stays under B&P 7108.5 (PCC 10262 governs the prime).
    lower = pm.prompt_payment_check("public", "subcontractor", "subcontractor", 5_000, None, "2026-11-10", None,
                                    as_of=AS_OF, public_agency="state").obligations[0]
    assert lower.governing_law == "Business and Professions Code 7108.5" and lower.due_date == "2026-11-17"


def test_supplier_has_no_statutory_deadline():
    r = pm.prompt_payment_check("private", "direct_contractor", "supplier", 3_000, "2026-11-01", None, None,
                                as_of=AS_OF)
    ob = r.obligations[0]
    assert ob.governing_law == "purchase-order / credit terms"
    assert (ob.status, ob.days_late, ob.late_charge_usd) == ("overdue", 24, 0.0)   # Nov 1 -> Nov 25 = 24
    assert any("not material suppliers" in n for n in ob.notes)


def test_missing_trigger_date_gives_unknown_status():
    r = pm.prompt_payment_check("private", "direct_contractor", "subcontractor", 5_000, as_of=AS_OF)
    ob = r.obligations[0]
    assert ob.status == "due_date_unknown" and ob.due_date is None
    assert any("payment_received_by_payer_date" in n for n in ob.notes)


# ---------------------------------------------------------------- prompt payment: owner -> direct contractor
def test_private_owner_civ_8800_overdue_with_dispute():
    # Pay app (payment demand) 2026-10-01 -> due 10-01 + 30 = 2026-10-31 (a Saturday).
    # $50,000 billed, $8,000 disputed: hold min(50,000, 12,000) = 12,000; pay 38,000. Unpaid at 11-25: 25 days late.
    # Months: 0 full (Oct 31 + 1 month = Nov 30 > Nov 25) + 25/30 = 0.8333. 38,000 x 2% x 25/30 = $633.33.
    r = pm.prompt_payment_check("private", "owner", "direct_contractor", 50_000, disputed_amount_usd=8_000,
                                as_of=AS_OF, payment_request_date="2026-10-01")
    ob = r.obligations[0]
    assert ob.governing_law == "Civil Code 8800"
    assert (ob.due_date, ob.max_withhold_usd, ob.must_pay_by_due_date_usd) == ("2026-10-31", 12_000.0, 38_000.0)
    assert (ob.status, ob.days_late, ob.late_charge_usd) == ("overdue", 25, 633.33)
    assert ob.late_charge_if_each_started_month_counts_usd == 760.00            # 1 month x 2% x 38,000
    assert any("Saturday" in n for n in ob.notes) and any("8802" in n for n in ob.notes)
    assert not r.verified and "private-owner-progress-payment-30-days" in r.unverified_rule_ids


def test_private_owner_retention_civ_8812():
    # Completion 2026-08-14 -> retention due 08-14 + 45 = 2026-09-28. Unpaid at 11-25: Sep 28 -> Oct 28 (30) +
    # Oct 28 -> Nov 25 (28) = 58 days. Months: 1 full + 28/31 (Oct 28 -> Nov 28 is 31 days) = 1.903226.
    # 20,000 x 2% x 1.903226 = $761.29.
    r = pm.prompt_payment_check("private", "owner", "direct_contractor", retention_amount_usd=20_000,
                                completion_date="2026-08-14", as_of=AS_OF)
    ob = r.obligations[0]
    assert (ob.kind, ob.governing_law, ob.due_date) == ("retention", "Civil Code 8812", "2026-09-28")
    assert (ob.status, ob.days_late, ob.late_charge_usd) == ("overdue", 58, 761.29)
    assert "private-retention-release-owner-45-days" in ids(r)
    assert core.rule("private-retention-release-owner-45-days").verified
    assert "private-retention-penalty-2pct" in r.unverified_rule_ids


def test_retention_completion_from_notice_of_completion():
    # No completion date; NOC recorded 2026-09-10 -> latest due 09-10 + 45 = 10-25.
    # NOC is recorded within 15 days after completion -> earliest due (09-10 - 15 = 08-26) + 45 = 2026-10-10.
    r = pm.prompt_payment_check("private", "owner", "direct_contractor", retention_amount_usd=10_000,
                                noc_recorded_date="2026-09-10", as_of="2026-10-01")
    ob = r.obligations[0]
    assert (ob.due_date, ob.status, ob.days_until_due) == ("2026-10-25", "not_yet_due", 24)
    assert "LATEST possible" in ob.trigger
    assert any("2026-10-10" in n for n in ob.notes)


def test_public_local_progress_interest_and_retention_7107():
    # Progress: agency received a proper request 09-15 -> due 09-15 + 30 = 10-15; 41 days late at 11-25
    # (Oct 15 -> Nov 15 = 31, + 10). Interest: 100,000 x 10% x 41/365 = $1,123.29.
    # Retention: acceptance 2026-07-15 -> due + 60 = 2026-09-13; 73 days late (30 + 31 + 12).
    # Months: 2 full (to Nov 13) + 12/30 = 2.4 -> 50,000 x 2% x 2.4 = $2,400.00. Total = $3,523.29.
    r = pm.prompt_payment_check("public", "owner", "direct_contractor", 100_000, retention_amount_usd=50_000,
                                completion_date="2026-07-15", as_of=AS_OF, payment_request_date="2026-09-15")
    prog, ret = r.obligations
    assert (prog.governing_law, prog.due_date, prog.days_late) == ("Public Contract Code 20104.50", "2026-10-15", 41)
    assert (prog.late_charge_type, prog.late_charge_usd) == ("interest_legal_rate", 1_123.29)
    assert (ret.governing_law, ret.due_date, ret.days_late, ret.late_charge_usd) == (
        "Public Contract Code 7107(c)", "2026-09-13", 73, 2_400.00)
    assert r.total_late_charges_usd == 3_523.29
    assert not r.verified and r.unverified_sources[0]["key"] == "ccp-685.010-legal-rate"
    assert r.payer_role == "owner" and prog.payer == "public entity"


def test_public_state_retention_flags_90_day_alternative():
    r = pm.prompt_payment_check("public", "owner", "direct_contractor", retention_amount_usd=5_000,
                                completion_date="2026-07-15", as_of=AS_OF, public_agency="state")
    assert "pcc-7107-public-retention-60-days" in ids(r)
    assert any("90 days" in n for n in r.obligations[0].notes)


def test_retention_pass_through_private_10_days_and_public_7_days():
    # Private (Civ 8814): received 11-01 -> due 11-11; paid 11-10 -> on time.
    r = pm.prompt_payment_check("private", "direct_contractor", "subcontractor", retention_amount_usd=12_000,
                                as_of=AS_OF, retention_received_by_payer_date="2026-11-01",
                                retention_paid_date="2026-11-10")
    ob = r.obligations[0]
    assert (ob.governing_law, ob.due_date, ob.status, ob.late_charge_usd) == (
        "Civil Code 8814", "2026-11-11", "paid_on_time", 0.0)
    # Public (PCC 7107(d)): received 11-01 -> due 11-08; unpaid at 11-25 -> 17 days late.
    # 17/30 of a month (Nov 8 -> Dec 8 = 30 days): 12,000 x 2% x 17/30 = $136.00; started-month reading $240.00.
    r2 = pm.prompt_payment_check("public", "direct_contractor", "subcontractor", retention_amount_usd=12_000,
                                 as_of=AS_OF, retention_received_by_payer_date="2026-11-01")
    ob2 = r2.obligations[0]
    assert (ob2.governing_law, ob2.due_date, ob2.days_late) == ("Public Contract Code 7107(d)", "2026-11-08", 17)
    assert (ob2.late_charge_usd, ob2.late_charge_if_each_started_month_counts_usd) == (136.00, 240.00)


def test_prompt_payment_input_errors():
    with pytest.raises(ValueError, match="direct contractor"):
        pm.prompt_payment_check("private", "owner", "subcontractor", 1_000, as_of=AS_OF)
    with pytest.raises(ValueError, match="payer_role=owner"):
        pm.prompt_payment_check("private", "direct_contractor", "direct_contractor", 1_000, as_of=AS_OF)
    with pytest.raises(ValueError, match="amount_due_usd"):
        pm.prompt_payment_check("private", "direct_contractor", "subcontractor", as_of=AS_OF)
    with pytest.raises(ValueError, match="cannot exceed"):
        pm.prompt_payment_check("private", "direct_contractor", "subcontractor", 1_000, None, "2026-11-01",
                                disputed_amount_usd=2_000, as_of=AS_OF)
    with pytest.raises(ValueError, match="ISO date"):
        pm.prompt_payment_check("private", "direct_contractor", "subcontractor", 1_000, None, "11/01/2026",
                                as_of=AS_OF)


def test_as_of_accepts_date_objects():
    r = pm.prompt_payment_check("private", "direct_contractor", "subcontractor", 1_000, None, date(2026, 11, 1),
                                as_of=date(2026, 11, 3))
    assert r.as_of == "2026-11-03" and r.obligations[0].days_until_due == 5     # due 11-08


# ---------------------------------------------------------------- retention cap
def test_private_subcontract_over_cap_with_flowdown_violation():
    # Subcontract $1,200,000 signed 2026-03-02 (after SB 61's 2026-01-01 start), 10% retention, prime holds 5%.
    # Cap total = 5% x 1,200,000 = $60,000; requested 10% = $120,000; excess $60,000.
    # This pay app $80,000 with $58,000 already held: min(5% x 80,000 = 4,000, 60,000 - 58,000 = 2,000) = $2,000.
    r = pm.retention_cap_check(1_200_000, 10, "2026-03-02", "private", as_of=AS_OF, contract_tier="subcontract",
                               prime_contract_retention_pct=5, payment_amount_usd=80_000,
                               retention_held_to_date_usd=58_000)
    assert r.cap_applies and r.cap_pct == 5 and not r.compliant
    assert (r.max_total_retention_usd, r.requested_total_retention_usd, r.excess_retention_usd) == (
        60_000.0, 120_000.0, 60_000.0)
    assert (r.max_retention_this_payment_usd, r.requested_retention_this_payment_usd) == (2_000.0, 8_000.0)
    assert r.flowdown_ok is False
    assert "private-retention-5pct-cap" in ids(r)
    assert r.unverified_rule_ids == ["private-retention-no-waiver"]   # only the anti-waiver point is unverified


def test_private_prime_at_cap_is_compliant_and_verified():
    # $800,000 x 5% = $40,000 cap; 5% requested = $40,000 -> compliant.
    r = pm.retention_cap_check(800_000, 5, "2026-01-01", "private", as_of=AS_OF)
    assert r.compliant and r.cap_applies and r.max_total_retention_usd == 40_000.0 and r.excess_retention_usd == 0
    assert r.verified


def test_private_contract_before_2026_has_no_cap():
    r = pm.retention_cap_check(500_000, 10, "2025-12-15", "private", as_of=AS_OF)
    assert not r.cap_applies and r.compliant and r.requested_total_retention_usd == 50_000.0


def test_residential_exemption_needs_stories_within_limit():
    ok = pm.retention_cap_check(900_000, 10, "2026-02-01", "private", ["residential_not_mixed_use"], AS_OF, stories=3)
    assert ok.exemptions_applied == ["residential_not_mixed_use"] and not ok.cap_applies and ok.compliant
    assert "private-retention-cap-exemptions" in ids(ok) and ok.verified
    tall = pm.retention_cap_check(900_000, 10, "2026-02-01", "private", ["residential_not_mixed_use"], AS_OF, stories=5)
    assert tall.exemptions_rejected == ["residential_not_mixed_use"] and tall.cap_applies and not tall.compliant
    unknown = pm.retention_cap_check(900_000, 10, "2026-02-01", "private", ["residential_not_mixed_use"], AS_OF)
    assert unknown.exemptions_rejected and any("give stories" in n for n in unknown.notes)


def test_public_cap_and_substantially_complex_exemption():
    # Public $2,000,000 at 10%: cap 5% = $100,000; requested $200,000.
    r = pm.retention_cap_check(2_000_000, 10, "2026-05-01", "public", as_of=AS_OF)
    assert r.cap_applies and not r.compliant and r.max_total_retention_usd == 100_000.0
    assert "public-retention-5pct-cap" in ids(r) and r.verified
    ex = pm.retention_cap_check(2_000_000, 10, "2026-05-01", "public", ["substantially_complex_finding"], AS_OF)
    assert ex.exemptions_applied == ["substantially_complex_finding"] and ex.compliant
    assert any("bid documents" in a for a in ex.what_to_do)
    wrong = pm.retention_cap_check(2_000_000, 10, "2026-05-01", "public", ["residential_not_mixed_use"], AS_OF,
                                   stories=2)
    assert wrong.exemptions_rejected == ["residential_not_mixed_use"] and not wrong.compliant


def test_bond_failure_exemption_only_for_subcontracts():
    prime = pm.retention_cap_check(400_000, 10, "2026-04-01", "public",
                                   ["sub_failed_to_furnish_bond_after_written_notice"], AS_OF)
    assert prime.exemptions_rejected and not prime.compliant
    sub = pm.retention_cap_check(400_000, 10, "2026-04-01", "public",
                                 ["sub_failed_to_furnish_bond_after_written_notice"], AS_OF, contract_tier="subcontract")
    assert sub.exemptions_applied and any("4108(c)" in n for n in sub.notes)


def test_retention_cap_input_errors():
    with pytest.raises(ValueError):
        pm.retention_cap_check(100_000, 120, "2026-02-01", "private")
    with pytest.raises(ValueError):
        pm.retention_cap_check(0, 5, "2026-02-01", "private")
    with pytest.raises(ValueError, match="unknown exemption"):
        pm.retention_cap_check(100_000, 5, "2026-02-01", "private", ["owner_said_so"])


# ---------------------------------------------------------------- lien waivers
def test_conditional_progress_waiver():
    r = pm.select_lien_waiver("progress", "conditional", as_of=AS_OF)
    assert (r.civil_code_section, r.form_name) == ("Civil Code 8132", "Conditional Waiver and Release on Progress Payment")
    assert r.safe_to_sign_now and not r.full_text_in_rulebook
    assert r.official_form_url.endswith("ConditionalWaiverAndReleaseOnProgressPayment.pdf")
    assert "Through Date" in r.fields_to_fill and "Retentions" in r.exceptions_on_form
    assert r.notice_text_excerpt.startswith("NOTICE: THIS DOCUMENT WAIVES")
    assert any("exactly as printed" in w for w in r.warnings)
    assert {"waiver-form-conditional-progress", "waiver-statutory-form-required"} <= ids(r)


def test_unconditional_before_payment_is_not_safe():
    r = pm.select_lien_waiver("progress", "unconditional", payment_received=False, as_of=AS_OF)
    assert r.civil_code_section == "Civil Code 8134" and r.safe_to_sign_now is False
    assert r.recommended_form_instead == "Conditional Waiver and Release on Progress Payment (Civil Code 8132)"
    assert r.warnings[0].startswith("DO NOT SIGN")


def test_final_unconditional_lists_disputed_extras():
    r = pm.select_lien_waiver("final", "unconditional", payment_received=True, open_change_order_claims_usd=3_500,
                              as_of=AS_OF)
    assert r.civil_code_section == "Civil Code 8138" and r.safe_to_sign_now is True
    assert any("$3,500.00" in w and "Disputed claims for extras" in w for w in r.warnings)
    assert r.sequence[0].startswith("1. With the pay request: Conditional Waiver and Release on Final Payment")


def test_final_conditional_home_improvement_rules():
    r = pm.select_lien_waiver("final", "conditional", home_improvement=True, as_of=AS_OF)
    assert r.civil_code_section == "Civil Code 8136"
    assert {"hic-lien-release-on-payment", "hic-contract-lien-release-clause"} <= ids(r)
    assert not r.verified           # the waiver form rules are unchecked in the rulebook
    with pytest.raises(ValueError):
        pm.select_lien_waiver("partial", "conditional")


# ---------------------------------------------------------------- stop payment notices: private
def test_private_sub_spn_after_notice_of_completion():
    # Completion 10-20, NOC recorded 10-28 (within 15 days -> effective).
    # Candidates: 10-20 + 90 = 2027-01-18; 10-28 + 30 = 2026-11-27 -> deadline 11-27, 2 days left (critical).
    # Prelim: first furnishing 06-01 -> due 06-21; served 06-30 (late) -> covers work from 06-30 - 20 = 06-10.
    # Lender bond: 125% x 40,000 = 50,000. Suit latest: 11-27 + 90 = 2027-02-25. Bond suit: 10-20 + 6 mo = 2027-04-20.
    r = pm.stop_payment_notice_deadline("private", "subcontractor", "2026-10-20", "2026-10-28", "2026-10-15", AS_OF,
                                        first_furnishing_date="2026-06-01",
                                        preliminary_notice_served_date="2026-06-30", claim_amount_usd=40_000,
                                        construction_lender=True, payment_bond_recorded=True)
    assert (r.deadline, r.days_remaining, r.risk_level) == ("2026-11-27", 2, "critical")
    assert r.notice_of_completion_effective is True and "30 days after" in r.deadline_rule_applied
    pn = r.preliminary_notice
    assert (pn.deadline, pn.on_time, pn.covers_work_from) == ("2026-06-21", False, "2026-06-10")
    assert r.bonded_notice_amount_usd == 50_000.0
    assert r.enforcement_suit_latest == "2027-02-25"
    assert any("2027-04-20" in b for b in r.payment_bond)
    assert r.may_give_to == ["owner (or the owner's architect)", "construction lender (bonded notice)"]
    assert not r.verified and r.unverified_sources[0]["key"] == "civ-8508-spn-timing"
    assert {"lien-deadline-subcontractor-supplier", "spn-bonded-lender-125", "spn-enforcement-timing"} <= ids(r)


def test_private_late_noc_is_ineffective():
    # NOC recorded 08-20, 19 days after completion 08-01 (> 15) -> ineffective -> 08-01 + 90 = 2026-10-30.
    r = pm.stop_payment_notice_deadline("private", "supplier", "2026-08-01", "2026-08-20", as_of="2026-09-01")
    assert (r.deadline, r.notice_of_completion_effective) == ("2026-10-30", False)
    assert r.days_remaining == 59                                   # Sep 1 -> Oct 30
    assert any("2026-09-19" in n and "meet that date" in n for n in r.notes)   # safe date if upheld: 08-20 + 30
    # The supplier gave the owner a preliminary notice, the property is not a 1-4 unit residence, and the owner never
    # sent it a copy (Civ 8190) -> the NOC does not shorten its period: 08-01 + 90 = 10-30.
    r2 = pm.stop_payment_notice_deadline("private", "supplier", "2026-08-01", "2026-08-05", as_of="2026-09-01",
                                         noc_copy_received=False, preliminary_notice_served_date="2026-06-10",
                                         residential_4_or_fewer_units=False)
    assert (r2.deadline, r2.notice_of_completion_effective) == ("2026-10-30", False)
    assert any("2026-09-04" in n for n in r2.notes)                 # the owner's position: 08-05 + 30


def test_missing_owner_copy_only_helps_claimants_owed_one():
    # Civ 8190 copy goes only to the direct contractor and claimants that gave the owner a preliminary notice.
    # No preliminary notice -> no copy owed -> NOC still shortens: 08-05 + 30 = 2026-09-04.
    r = pm.stop_payment_notice_deadline("private", "supplier", "2026-08-01", "2026-08-05", as_of="2026-09-01",
                                        noc_copy_received=False)
    assert (r.deadline, r.notice_of_completion_effective) == ("2026-09-04", True)
    assert any("does not extend your deadline" in n for n in r.notes)
    # Preliminary notice given, but the property is (or may be) a residence of 4 or fewer units: the owner may be
    # exempt from the copy rule, so the shorter date is kept.
    for small in (True, None):
        r2 = pm.stop_payment_notice_deadline("private", "subcontractor", "2026-08-01", "2026-08-05", as_of="2026-09-01",
                                             noc_copy_received=False, preliminary_notice_served_date="2026-06-10",
                                             residential_4_or_fewer_units=small)
        assert r2.deadline == "2026-09-04" and r2.notice_of_completion_effective is True
        assert "noc-copy-to-claimants-10-days" in ids(r2)


def test_notice_of_cessation_sets_completion():
    # Labor stopped 2026-08-20; owner recorded a notice of cessation 2026-09-25 (36 days later, >= 30): completion is
    # the recording date (Civ 8180). Deadline = 09-25 + 30 = 2026-10-25 (a Sunday), not 08-20 + 60 + 90 = 2027-01-17.
    # As of 10-01: Oct 1 -> Oct 25 = 24 days. Bond: suit 09-25 + 6 months = 2027-03-25; no prelim -> bond notice
    # 09-25 + 15 = 2026-10-10.
    r = pm.stop_payment_notice_deadline("private", "subcontractor", as_of="2026-10-01",
                                        cessation_of_labor_date="2026-08-20", noc_recorded_date="2026-09-25",
                                        payment_bond_recorded=True)
    assert (r.completion_date_used, r.deadline, r.days_remaining) == ("2026-09-25", "2026-10-25", 24)
    assert r.notice_of_completion_effective is True
    assert any("2027-03-25" in b for b in r.payment_bond) and any("2026-10-10" in b for b in r.payment_bond)
    assert any(li.item == "Completion by a recorded notice of cessation" for li in r.line_items)


def test_possible_cessation_notice_recorded_late_uses_shorter_date():
    # Labor stopped 06-01 -> completion by 60 days' cessation 07-31. Notice recorded 08-25 (> 07-31 + 15) but 85 days
    # after labor stopped, so it may be a notice of cessation: use 08-25 + 30 = 09-24 (safe), not 07-31 + 90 = 10-29.
    r = pm.stop_payment_notice_deadline("private", "supplier", as_of="2026-09-01", cessation_of_labor_date="2026-06-01",
                                        noc_recorded_date="2026-08-25")
    assert (r.completion_date_used, r.deadline, r.notice_of_completion_effective) == ("2026-07-31", "2026-09-24", None)
    assert any("2026-10-29" in n for n in r.notes)


def test_notice_recorded_before_given_completion_uses_shorter_date():
    # Claimant says completion 10-20; the owner recorded a notice 10-10. Possibly premature (ineffective -> 10-20 + 90
    # = 2027-01-18) but it may be valid: use 10-10 + 30 = 2026-11-09 to be safe. As of 10-25: Oct 25 -> Oct 31 (6)
    # + 9 = 15 days left (urgent: more than 14).
    r = pm.stop_payment_notice_deadline("private", "supplier", "2026-10-20", "2026-10-10", as_of="2026-10-25")
    assert (r.deadline, r.days_remaining, r.risk_level) == ("2026-11-09", 15, "urgent")
    assert r.notice_of_completion_effective is None
    assert any("2027-01-18" in n for n in r.notes)


def test_private_spn_without_completion_uses_last_furnishing():
    # No completion: last furnishing 09-15 + 90 = 2026-12-14 (Sep 15 -> Oct 15 30, -> Nov 15 31, + 29).
    r = pm.stop_payment_notice_deadline("private", "subcontractor", last_furnishing_date="2026-09-15", as_of=AS_OF,
                                        spn_given_date="2026-11-20")
    assert (r.deadline, r.days_remaining, r.risk_level) == ("2026-12-14", 19, "urgent")
    assert r.enforcement_suit_earliest == "2026-11-30"              # 11-20 + 10
    assert any("last furnishing" in n for n in r.notes)
    assert r.what_to_send[0].startswith("If you have not served a preliminary notice")


def test_direct_contractor_lender_notice_after_cessation():
    # Labor stopped 08-20 -> completion by 60 days' cessation = 10-19 -> + 90 = 2027-01-17 (a Sunday).
    # Bonded lender notice: 125% x 80,000 = 100,000.
    r = pm.stop_payment_notice_deadline("private", "direct_contractor", as_of=AS_OF,
                                        cessation_of_labor_date="2026-08-20", construction_lender=True,
                                        claim_amount_usd=80_000)
    assert (r.completion_date_used, r.deadline) == ("2026-10-19", "2027-01-17")
    assert r.may_give_to == ["construction lender (bonded notice)"] and r.bonded_notice_amount_usd == 100_000.0
    assert any("Sunday" in n for n in r.notes)
    assert "civ-8530-lender-spn" in {u["key"] for u in r.unverified_sources}
    assert "lien-deadline-direct-contractor" in ids(r)


def test_laborer_needs_no_preliminary_notice():
    r = pm.stop_payment_notice_deadline("private", "laborer", "2026-10-01", as_of=AS_OF)
    assert r.preliminary_notice.required is False and r.deadline == "2026-12-30"   # Oct 1 + 90


# ---------------------------------------------------------------- stop payment notices: public
def test_public_supplier_state_project():
    # Acceptance 09-30, no notice recorded -> 09-30 + 90 = 2026-12-29; 34 days left at 11-25 (urgent).
    # Prelim full-coverage date: 03-02 + 20 = 03-22. No prelim -> bond claim notice: 09-30 + 75 = 2026-12-14.
    # Bond suit: 12-29 + 6 months = 2027-06-29. Suit on notice: 11-20 + 10 = 11-30 through 12-29 + 90 = 2027-03-29.
    r = pm.stop_payment_notice_deadline("public", "supplier", "2026-09-30", as_of=AS_OF,
                                        first_furnishing_date="2026-03-02", spn_given_date="2026-11-20",
                                        public_agency="state")
    assert (r.deadline, r.days_remaining, r.risk_level) == ("2026-12-29", 34, "urgent")
    assert r.preliminary_notice.deadline == "2026-03-22"
    assert any("2026-12-14" in b for b in r.payment_bond) and any("2027-06-29" in b for b in r.payment_bond)
    assert (r.enforcement_suit_earliest, r.enforcement_suit_latest) == ("2026-11-30", "2027-03-29")
    assert r.notify_public_entity_within_days_of_filing == 5
    assert r.where_to_serve == ["File with: director of the department that initiated/awarded the contract"]
    assert r.required_contents[0] == "signed and verified by the claimant"
    assert "public-spn-deadline" in ids(r)


def test_public_notice_recorded_shortens_and_expires():
    # Acceptance 09-30, notice of completion recorded 10-05 -> 10-05 + 30 = 11-04 (earlier than 12-29).
    # As of 11-25 that is 21 days past -> expired.
    r = pm.stop_payment_notice_deadline("public", "subcontractor", "2026-09-30", "2026-10-05", as_of=AS_OF)
    assert (r.deadline, r.days_remaining, r.risk_level) == ("2026-11-04", -21, "expired")
    assert any("passed 21 days ago" in n for n in r.notes)
    assert any("controller" in w for w in r.where_to_serve)


def test_public_direct_contractor_not_applicable():
    r = pm.stop_payment_notice_deadline("public", "direct_contractor", "2026-09-30", as_of=AS_OF)
    assert r.applicable is False and r.deadline is None and not r.verified
    assert r.unverified_sources[0]["key"] == "civ-9100-public-claimant"


def test_spn_input_errors():
    with pytest.raises(ValueError, match="completion_date"):
        pm.stop_payment_notice_deadline("private", "subcontractor", as_of=AS_OF)
    with pytest.raises(ValueError, match="first_furnishing_date"):
        pm.stop_payment_notice_deadline("private", "subcontractor", last_furnishing_date="2026-05-01",
                                        first_furnishing_date="2026-06-01", as_of=AS_OF)


def test_unverified_dict_entries_have_sources():
    for key, entry in pm.UNVERIFIED.items():
        assert entry["source"].startswith("https://") and entry["citation"], key


# ---------------------------------------------------------------- review fixes
def test_public_direct_contractor_overdue_not_sent_to_stop_notice():
    # A prime on public works has no stop payment notice or payment bond claim of its own (Civ 9100).
    r = pm.prompt_payment_check("public", "owner", "direct_contractor", 100_000, as_of=AS_OF,
                                payment_request_date="2026-09-15")
    assert any("Unpaid direct contractor on public works" in a for a in r.what_to_send)
    assert not any("stop payment notice to the public entity" in a for a in r.what_to_send)
    assert "civ-9100-public-claimant" in {u["key"] for u in r.unverified_sources}
    # Private prime: mechanics lien plus a bonded notice to the construction lender (not the owner).
    p = pm.prompt_payment_check("private", "owner", "direct_contractor", 50_000, as_of=AS_OF,
                                payment_request_date="2026-10-01")
    assert any("mechanics lien" in a and "construction lender" in a for a in p.what_to_send)


def test_state_contract_dispute_uses_150_percent_and_flags_10262_5():
    # $5,000 billed, $1,000 disputed: hold min(5,000, 150% x 1,000 = 1,500) = $1,500; pay $3,500.
    r = pm.prompt_payment_check("public", "direct_contractor", "subcontractor", 5_000, None, "2026-11-10", None,
                                disputed_amount_usd=1_000, as_of=AS_OF, public_agency="state")
    ob = r.obligations[0]
    assert (ob.max_withhold_usd, ob.must_pay_by_due_date_usd) == (1_500.0, 3_500.0)
    assert "pcc-10262-state-subs" in ob.rule_ids and not r.verified       # 10262 dispute rule is unverifiable
    assert any("10262.5" in n for n in ob.notes) and ob.late_charge_usd == 0


def test_supplier_dispute_has_no_statutory_percentage():
    # $3,000 invoice, $500 disputed: no statutory dispute rule, so the undisputed $2,500 is shown as due.
    r = pm.prompt_payment_check("private", "direct_contractor", "supplier", 3_000, "2026-12-01", None, None,
                                disputed_amount_usd=500, as_of=AS_OF)
    ob = r.obligations[0]
    assert (ob.max_withhold_usd, ob.must_pay_by_due_date_usd, ob.status) == (500.0, 2_500.0, "not_yet_due")
    assert ob.max_withhold_basis.startswith("disputed amount left out")
    assert any("outside the deadline" in a for a in r.what_to_send)
    assert not any("may hold back at most" in a for a in r.what_to_send)
    assert "failure-to-pay-suppliers-and-services" in ids(r)          # B&P 7120 discipline note


def test_cslb_citation_minimum_before_july_2026():
    # Received 05-01 -> due 05-08; paid 06-20: May 8 -> Jun 8 (31) + 12 = 43 days late.
    # 1 full month + 12/30 (Jun 8 -> Jul 8 = 30 days) = 1.4 months; 10,000 x 2% x 1.4 = $280.00.
    # Citation range before the 2026-07-01 statutory floor: $200 to $2,000.
    r = pm.prompt_payment_check("private", "direct_contractor", "subcontractor", 10_000, None, "2026-05-01",
                                "2026-06-20", as_of="2026-06-25")
    ob = r.obligations[0]
    assert (ob.days_late, ob.late_charge_usd) == (43, 280.00)
    assert any("$200.00 to $2,000.00" in a for a in r.what_to_send)


def test_public_sub_tier_retention_pass_through_flagged():
    # Sub received retention 11-01 -> pays its sub by 11-08 (PCC 7107(d), 7 days); flagged as applied by analogy.
    r = pm.prompt_payment_check("public", "subcontractor", "subcontractor", retention_amount_usd=4_000, as_of=AS_OF,
                                retention_received_by_payer_date="2026-11-01", retention_paid_date="2026-11-06")
    ob = r.obligations[0]
    assert (ob.due_date, ob.status) == ("2026-11-08", "paid_on_time")
    assert any("original (prime) contractor" in n for n in ob.notes)


def test_final_waiver_warns_partial_payment_and_unconditional_timing():
    r = pm.select_lien_waiver("final", "conditional", as_of=AS_OF)
    assert any("whole remaining balance" in w for w in r.warnings)
    assert any("check details" in w for w in r.warnings)
    u = pm.select_lien_waiver("progress", "unconditional", payment_received=True, as_of=AS_OF)
    assert "signs and delivers" in u.takes_effect and u.safe_to_sign_now is True
    assert not any("whole remaining balance" in w for w in u.warnings)


def test_direct_contractor_lender_prelim_and_no_bond_rights():
    # Prime with a construction lender must give the lender a preliminary notice; its own payment bond protects
    # the people below it, so no bond deadlines. Completion 10-20 + 90 = 2027-01-18.
    r = pm.stop_payment_notice_deadline("private", "direct_contractor", "2026-10-20", as_of=AS_OF,
                                        construction_lender=True, payment_bond_recorded=True)
    assert r.deadline == "2027-01-18" and r.payment_bond == []
    assert r.preliminary_notice.required is True
    assert r.what_to_send[0].startswith("If you have not served a preliminary notice, serve it today on construction")
    assert any("protects the subcontractors" in n for n in r.notes)
    no_lender = pm.stop_payment_notice_deadline("private", "direct_contractor", "2026-10-20", as_of=AS_OF)
    assert no_lender.preliminary_notice.required is False
    assert not any("preliminary notice, serve it today" in a for a in no_lender.what_to_send)


def test_prelim_served_without_first_furnishing_date():
    # Served 07-01, first furnishing unknown: on-time unknown; if late it covers work from 07-01 - 20 = 06-11.
    r = pm.stop_payment_notice_deadline("private", "subcontractor", "2026-10-20", as_of=AS_OF,
                                        preliminary_notice_served_date="2026-07-01")
    pn = r.preliminary_notice
    assert (pn.on_time, pn.covers_work_from) == (None, "2026-06-11")
    assert "first_furnishing_date" in pn.note
    assert any(li.item.startswith("If served late") for li in r.line_items)


def test_public_payment_bond_threshold_note():
    r = pm.stop_payment_notice_deadline("public", "supplier", "2026-09-30", as_of=AS_OF)
    assert "public-payment-bond-required-25000" in ids(r)
    assert any("$25,000.00" in n for n in r.notes)


# ---------------------------------------------------------------- MCP wiring
def test_mcp_tools_round_trip():
    mcp = FastMCP("t")
    pm.register(mcp)

    async def run():
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
            pay = await c.call_tool("prompt_payment_check", {
                "project_type": "private", "payer_role": "direct_contractor", "payee_role": "subcontractor",
                "amount_due_usd": 10000, "payment_received_by_payer_date": "2026-09-01", "paid_date": "2026-11-20",
                "as_of": AS_OF})
            cap = await c.call_tool("retention_cap_check", {
                "contract_price_usd": 800000, "retention_pct": 10, "contract_date": "2026-02-01",
                "project_type": "private", "as_of": AS_OF})
            waiver = await c.call_tool("select_lien_waiver", {"payment_type": "final", "payment_status": "conditional"})
            spn = await c.call_tool("stop_payment_notice_deadline", {
                "project_type": "public", "claimant_role": "subcontractor", "completion_date": "2026-09-30",
                "as_of": AS_OF, "residential_4_or_fewer_units": False})
            return names, pay.structured_content, cap.structured_content, waiver.structured_content, \
                spn.structured_content

    names, pay, cap, waiver, spn = asyncio.run(run())
    assert {"prompt_payment_check", "retention_cap_check", "select_lien_waiver",
            "stop_payment_notice_deadline"} <= names
    assert pay["obligations"][0]["late_charge_usd"] == 480.0 and pay["verified"] is True   # same as the first test
    assert cap["compliant"] is False and cap["max_total_retention_usd"] == 40000.0          # 800,000 x 5%
    assert waiver["civil_code_section"] == "Civil Code 8136"
    assert spn["deadline"] == "2026-12-29"                                                  # 09-30 + 90
