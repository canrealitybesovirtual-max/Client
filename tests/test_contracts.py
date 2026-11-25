"""Home improvement contracts: validator, generator, change orders, cancellation deadlines.

Expected values are computed by hand in the comments. Calendar facts used below:
2026-11-26 is Thanksgiving (Thu); 2026-07-04 is a Saturday; 2026-12-25 is a Friday; 2028-01-01 is a Saturday;
2026-01-19 is MLK Day (NOT a Civil Code 1689.5 holiday).
"""
import asyncio
from datetime import date, timedelta

import pytest
from fastmcp import Client, FastMCP

from tradecomply import contracts as c
from tradecomply import core

AS_OF = "2026-11-25"
ALL_IN = {k: True for k in c.ContractIncludes.model_fields}


def P(amount, work, value=None):
    return c.ProgressPayment(amount_usd=amount, for_work=work, value_of_work_usd=value)


def by_item(res, text):
    return [f for f in res.findings if text in f.item]


KITCHEN = [P(12000, "Demolition and rough framing complete"),
           P(15000, "Rough plumbing and electrical complete and passed inspection"),
           P(12000, "Cabinets delivered and installed"),
           P(8000, "Final completion and walkthrough")]  # 12k+15k+12k+8k = 47,000; + 1,000 down = 48,000


# ---------------------------------------------------------------- values come from the rulebook
def test_constants_are_rulebook_values():
    assert c.DOWN_CAP_USD == core.rule("downpayment-cap-1000-or-10-percent").param("max_downpayment_dollars") == 1000
    assert c.DOWN_CAP_PCT == 10 and c.HIC_THRESHOLD_USD == 500 and c.SR_MAX_USD == 750
    assert c.HIC_DAYS == {"three_day": 3, "five_day_senior": 5, "seven_day_disaster": 7}
    assert c.AB1327_FROM == date(2026, 1, 1) and c.SB517_FROM == date(2026, 1, 1)


# ---------------------------------------------------------------- down payment
@pytest.mark.parametrize("price,down,cap,excess", [
    (8000, 1000, 800.00, 200.00),      # 10% of 8,000 = 800 < 1,000 -> cap 800; 1,000 - 800 = 200 over
    (25000, 1000, 1000.00, 0.00),      # 10% of 25,000 = 2,500 > 1,000 -> cap 1,000
    (10000, 1000, 1000.00, 0.00),      # 10% of 10,000 = 1,000 = fixed cap -> exactly allowed
    (9999.99, 1000, 999.99, 0.01),     # 10% = 999.999 -> rounded DOWN to 999.99; 1,000 is 0.01 over
])
def test_down_payment_cap(price, down, cap, excess):
    d = c.down_payment_limit(price, down)
    assert d.max_allowed_usd == cap and d.excess_usd == excess and d.ok is (excess == 0)
    assert "lesser of $1,000.00" in d.math


def test_oversized_down_payment_is_a_violation_with_penalty_note():
    r = c.validate_home_improvement_contract(8000, 1000, [P(7000, "Fence installed and gate hung")],
                                             includes=ALL_IN, start_date="2026-12-07", completion_date="2026-12-11",
                                             subcontractors_will_be_used=False, as_of=AS_OF)
    f = by_item(r, "Down payment cap")[0]
    assert f.status == "violation" and "$200.00 over" in f.detail and f.citation == "B&P 7159.5(a)(3)"
    assert not r.compliant and any("$100.00 to $5,000.00" in n for n in r.notes)


def test_bonded_contractor_exempt_from_cap_and_schedule():
    # 4,000 down on 40,000 is 3,000 over the 1,000 cap, and "upon signing" is ahead of work, but a
    # registrar-approved bond / joint control exempts both (B&P 7159.5(a)(8)).
    r = c.validate_home_improvement_contract(40000, 4000, [P(36000, "Upon signing")],
                                             contractor_has_payment_bond_or_joint_control=True, as_of=AS_OF)
    assert r.down_payment.applies is False and r.down_payment.ok
    assert by_item(r, "Down payment cap")[0].status == "not_applicable"
    assert by_item(r, "Mechanics Lien Warning")[0].status == "not_applicable"
    assert by_item(r, "12-pt bold 'THE DOWNPAYMENT")[0].status == "not_applicable"
    assert all(ln.status == "not_applicable" for ln in r.payment_schedule)


# ---------------------------------------------------------------- cancellation deadlines (business days)
@pytest.mark.parametrize("received,senior,disaster,last", [
    # Mon 11/23: Tue 24 (1), Wed 25 (2), Thu 26 Thanksgiving skipped, Fri 27 (3)
    ("2026-11-23", False, False, "2026-11-27"),
    # senior 5: ... Fri 27 (3), Sat 28 (4, Saturdays count), Sun 29 skipped, Mon 30 (5)
    ("2026-11-23", True, False, "2026-11-30"),
    # Thu 7/2: Fri 7/3 (July 4 observed, skipped to be safe), Sat 7/4 holiday, Sun 7/5, then Mon 6, Tue 7, Wed 8
    ("2026-07-02", False, False, "2026-07-08"),
    # senior from Mon 12/21: Tue 22, Wed 23, Thu 24, Fri 25 Christmas skipped, Sat 26 (4), Sun skipped, Mon 28 (5)
    ("2026-12-21", True, False, "2026-12-28"),
    # disaster 7 from Mon 1/12: 13,14,15,16,17(Sat)=5, Sun 18 skipped, Mon 19 MLK counts (6), Tue 20 (7)
    ("2026-01-12", False, True, "2026-01-20"),
    # Sunday receipt 11/29: Mon 30 (1), Tue 12/1 (2), Wed 12/2 (3)
    ("2026-11-29", False, False, "2026-12-02"),
    # Wed 12/29/2027: Thu 30 (1), Fri 31 (New Year's 2028 observed) skipped, Sat 1/1 holiday, Sun 1/2, Mon 3 (2), Tue 4 (3)
    ("2027-12-29", False, False, "2028-01-04"),
])
def test_cancellation_deadline(received, senior, disaster, last):
    r = c.cancellation_deadline(received, buyer_age_65_or_older=senior, disaster_repair=disaster, as_of=AS_OF)
    p = r.cancellation
    assert p.last_day_to_cancel == last
    assert p.earliest_safe_start_date == (date.fromisoformat(last) + timedelta(days=1)).isoformat()
    assert r.verified is False and r.unverified_sources[0]["source"].startswith("https://leginfo")


def test_cancellation_explains_days_and_status():
    p = c.cancellation_deadline("2026-11-23", as_of=AS_OF).cancellation
    assert p.kind == "three_day" and p.notice_title == "Three-Day Right to Cancel" and p.business_days == 3
    assert p.days_skipped == ["2026-11-26 Thu (Thanksgiving Day)"] and len(p.days_counted) == 3
    assert p.open_as_of is True  # as of 11/25 the buyer can still cancel through 11/27
    assert p.refund_due_days_after_notice == 10 and p.goods_pickup_days_after_notice == 20
    assert c.cancellation_deadline("2026-11-23", as_of="2026-11-28").cancellation.open_as_of is False


def test_senior_five_day_only_for_contracts_from_2021():
    # Tue 2020-12-01, senior buyer, but the five-day rule covers contracts entered on/after 2021-01-01:
    # three days -> Wed 2 (1), Thu 3 (2), Fri 4 (3)
    p = c.cancellation_deadline("2020-12-01", buyer_age_65_or_older=True, as_of="2020-12-02").cancellation
    assert p.kind == "three_day" and p.last_day_to_cancel == "2020-12-04"


def test_place_of_business_needs_no_three_day_notice_but_disaster_still_does():
    p = c.cancellation_deadline("2026-11-23", negotiated_at_contractor_place_of_business=True).cancellation
    assert p.applies is False and p.kind == "none"
    p = c.cancellation_deadline("2026-01-12", negotiated_at_contractor_place_of_business=True,
                                disaster_repair=True).cancellation
    assert p.kind == "seven_day_disaster" and p.last_day_to_cancel == "2026-01-20"


# ---------------------------------------------------------------- full validator
def _kitchen(**kw):
    args = dict(includes=ALL_IN, start_date="2026-12-07", completion_date="2027-02-26",
                subcontractors_will_be_used=True, as_of=AS_OF)
    args.update(kw)
    return c.validate_home_improvement_contract(48000, 1000, KITCHEN, **args)


def test_compliant_kitchen_remodel():
    r = _kitchen()
    assert r.contract_type == "home_improvement" and r.compliant and r.violation_count == 0 and r.warning_count == 0
    assert r.down_payment.max_allowed_usd == 1000.0          # 10% of 48,000 = 4,800 -> capped at 1,000
    assert by_item(r, "Payments vs. contract price")[0].status == "ok"  # 1,000 + 47,000 = 48,000
    assert [ln.cumulative_progress_paid_usd for ln in r.payment_schedule] == [12000, 27000, 39000, 47000]
    assert r.verified is True and all(x.verified for x in r.citations)  # no cancellation date -> no unverified rule
    assert any("attorney" in p for p in r.required_professionals)


def test_cancellation_date_makes_result_unverified_and_flags_early_start():
    # Received Mon 11/30: Tue 12/1, Wed 12/2, Thu 12/3 -> last day 12/3; start 12/2 is inside the window
    r = _kitchen(buyer_received_signed_copy_on="2026-11-30", start_date="2026-12-02")
    assert r.cancellation.last_day_to_cancel == "2026-12-03" and r.cancellation.earliest_safe_start_date == "2026-12-04"
    f = by_item(r, "Start date vs. cancellation period")[0]
    assert f.status == "warning" and "2026-12-04" in f.detail
    assert r.verified is False and r.unverified_sources


def test_missing_items_are_violations_and_unchecked_items_are_reported():
    inc = dict(ALL_IN, mechanics_lien_warning=False, first_page_cancellation_address_and_email=False)
    del inc["cslb_information_notice"]
    r = _kitchen(includes=inc)
    assert by_item(r, "Mechanics Lien Warning")[0].status == "violation"
    assert by_item(r, "name, address and email")[0].citation.startswith("B&P 7159(c)(3)(B)(ii)")
    assert by_item(r, "CSLB information notice")[0].status == "not_checked"
    assert r.violation_count == 2 and r.not_checked_count == 1 and not r.compliant


def test_ab1327_email_items_not_required_before_2026():
    # Contract received 2025-12-15, before AB 1327 took effect 2026-01-01
    inc = dict(ALL_IN, first_page_cancellation_address_and_email=False, first_page_cancellation_help_phone=False,
               subcontractor_yes_no=False)
    r = _kitchen(includes=inc, buyer_received_signed_copy_on="2025-12-15", start_date="2026-01-12",
                 completion_date="2026-03-01", as_of="2025-12-16")
    assert by_item(r, "First page: phone")[0].status == "not_applicable"          # phone is new in AB 1327
    addr = by_item(r, "First page: name, address")[0]
    # email is new, but the older text still wanted name + address on page 1: a warning, not a violation
    assert addr.status == "warning" and "name and address" in addr.detail
    assert by_item(r, "Subcontractors [__] Yes")[0].status == "not_applicable"
    assert r.violation_count == 0


def test_contract_signed_on_drives_date_rules_without_a_receipt_date():
    # Reviewing a 2025 contract in 2026: without a date the 2026 rules would be applied; with contract_signed_on they are not
    inc = dict(ALL_IN, first_page_cancellation_help_phone=False)
    r = _kitchen(includes=inc)
    assert by_item(r, "First page: phone")[0].status == "violation"
    assert any("No contract date given" in n for n in r.notes)
    r = _kitchen(includes=inc, contract_signed_on="2025-10-06")
    assert by_item(r, "First page: phone")[0].status == "not_applicable"
    assert not any("No contract date given" in n for n in r.notes)


def test_subcontractor_disclaimer_unknown_box_is_a_warning():
    # Box answer unknown: a missing disclaimer is only required if the box is Yes -> warning, not violation
    r = _kitchen(includes=dict(ALL_IN, subcontractor_disclaimer=False), subcontractors_will_be_used=None)
    f = by_item(r, "Subcontractor-list disclaimer")[0]
    assert f.status == "warning" and r.violation_count == 0
    r = _kitchen(includes=dict(ALL_IN, subcontractor_disclaimer=False), subcontractors_will_be_used=True)
    assert by_item(r, "Subcontractor-list disclaimer")[0].status == "violation"


def test_salesperson_needs_his_number():
    r = _kitchen(has_salesperson=True)
    assert by_item(r, "HIS registration")[0].status == "violation"
    r = _kitchen(has_salesperson=True, salesperson_his_number="123456")
    assert by_item(r, "HIS registration")[0].status == "ok" and r.compliant


def test_front_loaded_schedule_is_ahead_of_work():
    # 30,000 job, 1,000 down. #1 pays 14,000 for work worth 9,000 -> 5,000 ahead (violation).
    # After #2: 14,000 + 15,000 = 29,000 paid vs 9,000 + 21,000 = 30,000 of work -> fine. 1,000 + 29,000 = 30,000.
    r = c.validate_home_improvement_contract(30000, 1000, [P(14000, "Demolition and framing complete", 9000),
                                                           P(15000, "Final completion", 21000)], as_of=AS_OF)
    first, second = r.payment_schedule
    assert first.status == "violation" and "$5,000.00 ahead" in first.detail
    assert second.status == "ok" and second.cumulative_value_of_work_usd == 30000
    assert by_item(r, "Progress payment #1")[0].citation == "B&P 7159.5(a)(5)"


def test_schedule_wording_and_totals():
    # 20,000 job: 1,000 + 10,000 + 10,000 = 21,000 -> 1,000 more than the price (violation)
    r = c.validate_home_improvement_contract(20000, 1000, [P(10000, "Upon signing"), P(10000, "")], as_of=AS_OF)
    lines = r.payment_schedule
    assert lines[0].status == "warning" and "before the work" in lines[0].detail      # "signing" = advance payment
    assert lines[1].status == "violation"                                             # no work referenced
    tot = by_item(r, "Payments vs. contract price")[0]
    assert tot.status == "violation" and "$1,000.00 more" in tot.detail
    # 20,000 job: 1,000 + 15,000 = 16,000 scheduled -> 4,000 unscheduled; CSLB worksheet guidance (unchecked)
    r = c.validate_home_improvement_contract(20000, 1000, [P(15000, "Roof replaced")], as_of=AS_OF)
    tot = by_item(r, "Payments vs. contract price")[0]
    assert tot.status == "warning" and "$4,000.00" in tot.detail and r.verified is False
    r = c.validate_home_improvement_contract(20000, 1000, [P(19000, "March 1")], as_of=AS_OF)
    assert r.payment_schedule[0].status == "warning" and "date" in r.payment_schedule[0].detail


def test_arbitration_clause_7191():
    r = _kitchen(includes_arbitration_clause=True, residential_units=2)
    f = by_item(r, "Arbitration")[0]
    assert f.status == "violation" and "anyone but you" in f.detail and f.rule_id == "noncompliant-arbitration-clause-unenforceable"
    assert any("7085(a)(6)" in n for n in r.notes) and any("arbitration provision" in p for p in r.required_professionals)
    r = _kitchen(includes_arbitration_clause=True, residential_units=6)
    assert by_item(r, "Arbitration")[0].status == "not_applicable"     # 7191 covers 4 or fewer units
    r = _kitchen(includes_arbitration_clause=True, arbitration_formatted_per_7191=True, residential_units=4)
    assert by_item(r, "Arbitration")[0].status == "ok" and r.compliant
    assert {"residential-arbitration-clause-title-and-format", "residential-arbitration-notice-and-initials"} <= {
        x.rule_id for x in r.citations}                                  # the ok finding relies on both 7191(a), (b)


def test_dates_and_pool():
    r = _kitchen(start_date="2027-03-01", completion_date="2027-02-01")
    assert by_item(r, "Dates")[0].status == "violation"
    r = _kitchen(is_swimming_pool=True, includes=dict(ALL_IN, pool_plan_and_scale_drawing=False))
    f = by_item(r, "Swimming pool")[0]
    assert f.status == "violation" and "void" in f.detail and f.citation == "B&P 7167"


@pytest.mark.parametrize("price,expected", [(500, "under_7159_threshold"), (500.01, "home_improvement")])
def test_500_dollar_threshold_is_exceeds(price, expected):
    assert c.validate_home_improvement_contract(price, as_of=AS_OF).contract_type == expected


def test_small_job_still_capped():
    # $400 job, $100 down: 7159 form not required, but 7159.5 still caps the down payment at 10% = $40 -> $60 over
    r = c.validate_home_improvement_contract(400, 100, as_of=AS_OF)
    assert r.contract_type == "under_7159_threshold" and r.down_payment.max_allowed_usd == 40.0
    assert r.down_payment.excess_usd == 60.0 and not r.compliant
    assert r.verified is False  # relies on the B&P 7151.2 definition rule, which is unchecked
    assert any("7159.5(a)(1)" in n and "writing" in n for n in r.notes)    # small jobs still need a written contract


def test_small_job_cancellation_is_labelled_as_civil_code_not_7159():
    # $400 job signed at the home Mon 11/23: 7159 notice not required; the period is shown as the Civil Code right
    r = c.validate_home_improvement_contract(400, buyer_received_signed_copy_on="2026-11-23", as_of=AS_OF)
    assert r.cancellation.explanation[0].startswith("B&P 7159 does not require its cancellation notice")
    assert r.cancellation.last_day_to_cancel == "2026-11-27"   # Tue 24 (1), Wed 25 (2), Thu 26 skipped, Fri 27 (3)


# ---------------------------------------------------------------- service and repair
def test_conforming_service_and_repair():
    r = c.validate_home_improvement_contract(750, is_service_and_repair=True, buyer_initiated_contact=True,
                                             only_work_needed_for_the_problem=True, includes=ALL_IN,
                                             buyer_received_signed_copy_on="2026-11-24", as_of=AS_OF)
    assert r.contract_type == "service_and_repair" and r.compliant            # $750 is "or less"
    # Tue 11/24: Wed 25 (1), Thu 26 Thanksgiving skipped, Fri 27 (2), Sat 28 (3) -> 2026-11-28
    assert r.cancellation.kind == "service_and_repair" and r.cancellation.last_day_to_cancel == "2026-11-28"
    assert by_item(r, "'Service and Repair'")[0].status == "ok"
    assert not by_item(r, "Home Improvement' (10-pt")                         # no 7159 checklist
    assert by_item(r, "16 CCR 872(a)")[0].status == "ok"                      # CGL form covers every HIC (7151.2)
    # refund/pickup periods come from the service and repair notice itself, not the 7159(e)(6) notice
    ids = {x.rule_id for x in r.citations}
    assert "service-repair-right-to-cancel-notice" in ids and "cancellation-refund-10-days-goods-20-days" not in ids
    assert r.cancellation.refund_due_days_after_notice == 10 and r.cancellation.goods_pickup_days_after_notice == 20


def test_service_and_repair_extra_checks():
    # 872 form missing on a service and repair contract: warning (B&P 7159.10(c) limits only the 7159 notices)
    r = c.validate_home_improvement_contract(600, is_service_and_repair=True, buyer_initiated_contact=True,
                                             only_work_needed_for_the_problem=True, service_charge_usd=200,
                                             includes=dict(ALL_IN, cgl_disclosure_form_872=False), as_of=AS_OF)
    assert r.contract_type == "service_and_repair" and r.compliant
    assert by_item(r, "16 CCR 872(a)")[0].status == "warning"
    # $600 + $200 service charge = $800 > $750 -> warning that the job may not qualify
    f = by_item(r, "Price plus service charge")[0]
    assert f.status == "warning" and "$800.00" in f.detail
    # $600 + $150 = $750 exactly -> no warning ("$750 or less")
    r = c.validate_home_improvement_contract(600, is_service_and_repair=True, buyer_initiated_contact=True,
                                             only_work_needed_for_the_problem=True, service_charge_usd=150,
                                             includes=ALL_IN, as_of=AS_OF)
    assert not by_item(r, "Price plus service charge")


def test_service_and_repair_falls_back_to_full_hic():
    r = c.validate_home_improvement_contract(800, is_service_and_repair=True, buyer_initiated_contact=True,
                                             only_work_needed_for_the_problem=True, as_of=AS_OF)
    assert r.contract_type == "home_improvement"
    assert any(f.status == "violation" and "$750.00" in f.detail for f in r.findings)
    assert by_item(r, "Contract form")[0].rule_id == "service-repair-nonconforming-full-hic-rules"
    # payment before completion also disqualifies, and the $50 down is over 10% of $450 = $45
    r = c.validate_home_improvement_contract(450, 50, is_service_and_repair=True, buyer_initiated_contact=True,
                                             only_work_needed_for_the_problem=True, as_of=AS_OF)
    assert r.contract_type == "home_improvement" and r.down_payment.excess_usd == 5.0
    # unconfirmed buyer-initiated contact is not good enough
    r = c.validate_home_improvement_contract(300, is_service_and_repair=True, only_work_needed_for_the_problem=True,
                                             as_of=AS_OF)
    assert r.contract_type == "home_improvement"


def test_workers_comp_exemption_claim_checked_against_license_class_and_2028():
    # A C-39 roofer can never claim the no-employee exemption (B&P 7125, version through 2027)
    r = _kitchen(workers_comp_notice_says_no_employees=True, license_classifications=["B", "C-39 Roofing"])
    f = by_item(r, "Workers' comp statement")[0]
    assert f.status == "violation" and "C-39" in f.detail and f.rule_id == "workers-comp-exemption-until-2028"
    # a B-licensee with no employees may, but a job finishing 2028-01-04 runs past the 2028-01-01 end -> warning
    r = _kitchen(workers_comp_notice_says_no_employees=True, license_classifications=["B"],
                 completion_date="2028-01-04")
    assert by_item(r, "Workers' comp exemption ends")[0].status == "warning" and r.compliant
    # contract signed after 2028-01-01: the statement is simply false -> violation under wc-exemption-ends-2028
    r = _kitchen(workers_comp_notice_says_no_employees=True, license_classifications=["B"],
                 contract_signed_on="2028-02-01", start_date="2028-03-01", completion_date="2028-04-01",
                 as_of="2028-02-01")
    assert by_item(r, "Workers' comp statement")[0].rule_id == "wc-exemption-ends-2028"
    assert c._license_codes(["c39", "C-61/D-49", "B"]) == {"C-39", "C-61", "D-49"}


# ---------------------------------------------------------------- generator
BASE = dict(contractor_name="Sierra Remodel Inc.", contractor_business_address="1200 J St, Sacramento, CA 95814",
            contractor_license_number="1098765", contractor_email="office@sierraremodel.example",
            contractor_phone="916-555-0142", buyer_names=["Maria Lopez", "Jose Lopez"],
            project_address="44 Elm Way, Sacramento, CA 95819", contract_price_usd=48000,
            project_description="Kitchen remodel", materials_and_equipment="Maple cabinets; quartz counters",
            down_payment_usd=1000, progress_payments=KITCHEN, start_date="2026-12-07", completion_date="2027-02-26",
            cgl_insurer_name="Golden State Mutual", cgl_insurer_phone="800-555-0199", subcontractors_will_be_used=True,
            signing_date="2026-11-23")


def test_generated_contract_has_every_item_in_order():
    g = c.generate_home_improvement_contract(BASE, as_of=AS_OF)
    md = g.markdown
    order = ["# Home Improvement", "CSLB License No. 1098765", "Date buyer signed this contract",
             "may be sent to the contractor at the address or email address", "office@sierraremodel.example",
             "You are entitled to a completely filled in copy", "## Contract Price", "$48,000.00",
             "## Description of the Project and Description of the Significant Materials",
             "## Downpayment", "THE DOWNPAYMENT MAY NOT EXCEED $1,000 OR 10 PERCENT",
             "## Schedule of Progress Payments", "IT IS AGAINST THE LAW FOR A CONTRACTOR",
             "Substantial commencement of work", "## Approximate Start Date", "## Approximate Completion Date",
             "## Note About Extra Work and Change Orders", "[X] Yes", "One or more subcontractors will be used",
             "full and unconditional release", "Attachment B is incorporated",
             "## Commercial General Liability Insurance (CGL)", "Golden State Mutual at 800-555-0199",
             "## Workers’ Compensation Insurance", "written authorization", "MECHANICS LIEN WARNING:",
             "Information about the Contractors State License Board (CSLB)", "[[ATTORNEY:",
             "right to require the contractor to have a performance and payment bond", "## Signatures",
             "Three-Day Right to Cancel", "Owner acknowledges receipt", "# Attachment A-1: Notice of Cancellation",
             "# Attachment A-2: Notice of Cancellation", "# Attachment B: Change Order Form",
             "# Attachment C: Information About Commercial General Liability Insurance"]
    pos = [md.find(s) for s in order]
    assert -1 not in pos, [s for s, p in zip(order, pos) if p == -1]
    assert pos == sorted(pos)
    # Notice of Cancellation is completed: signed Mon 11/23 -> deadline Fri 11/27 (Thanksgiving skipped)
    assert md.count("not later than midnight of 2026-11-27") == 2 and "[enter" not in md and "[name of seller]" not in md
    assert md.count("One or more subcontractors will be used") == 2       # contract + change-order form
    assert "| 3 | Cabinets delivered and installed | $12,000.00 |" in md
    assert g.problems == [] and g.cancellation.last_day_to_cancel == "2026-11-27"
    assert any(t.section == "Entitled-to-copy statement" and t.minimum == "12-pt bold" for t in g.type_size_notes)
    assert "heading at least 14-pt, questions at least 12-pt, comments in italics at least 11-pt" in md  # 16 CCR 872(a)
    assert any(p.startswith("[[ATTORNEY:") for p in g.placeholders)
    assert g.verified is False   # the cancellation deadline uses the unverified business-day definition


def test_generated_senior_bonded_arbitration_contract():
    g = c.generate_home_improvement_contract(dict(BASE, buyer_age_65_or_older=True, down_payment_usd=4000,
                                                  contractor_has_payment_bond_or_joint_control=True,
                                                  include_arbitration_clause=True), as_of=AS_OF)
    md = g.markdown
    assert "**Five-Day Right to Cancel**" in md and "fifth business day" in md and "third business day" not in md
    assert md.count("within five business days from the above date") == 2
    assert md.count("not later than midnight of 2026-11-30") == 2   # Fri 27 (3), Sat 28 (4), Mon 30 (5)
    assert "MECHANICS LIEN WARNING" not in md and "THE DOWNPAYMENT MAY NOT EXCEED" not in md
    assert "IT IS AGAINST THE LAW" not in md and "## Downpayment" in md and "$4,000.00" in md
    notice = core.rule("residential-arbitration-notice-and-initials").param("notice_text")
    assert notice in md and md.index("## ARBITRATION OF DISPUTES") < md.index(notice) < md.index("Buyer's initials")
    assert g.problems == []  # bonded: the 4,000 down and the schedule are allowed


def test_generator_flags_problems_and_special_cases():
    g = c.generate_home_improvement_contract(dict(BASE, contract_price_usd=8000, down_payment_usd=1000,
                                                  progress_payments=[P(7000, "Upon signing")]), as_of=AS_OF)
    assert "[[FIX BEFORE USE:" in g.markdown and any("$200.00 over" in p for p in g.problems)
    assert any("before the work" in p for p in g.problems)
    g = c.generate_home_improvement_contract(dict(BASE, negotiated_at_contractor_place_of_business=True,
                                                  sales_language="Spanish"), as_of=AS_OF)
    assert "Right to Cancel" not in g.markdown and "Attachment A-1" not in g.markdown
    assert g.markdown.startswith("[[TRANSLATE:") and any("Spanish translator" in p for p in g.required_professionals)
    g = c.generate_home_improvement_contract(dict(BASE, workers_comp_status="no_employees_exempt"), as_of=AS_OF)
    assert "has no employees and is exempt" in g.markdown and any("2028-01-01" in n for n in g.notes)
    g = c.generate_home_improvement_contract(dict(BASE, workers_comp_status="no_employees_exempt",
                                                  license_classifications=["C-39"]), as_of=AS_OF)
    assert "[[FIX BEFORE USE: License classification C-39" in g.markdown and any("C-39" in p for p in g.problems)
    g = c.generate_home_improvement_contract(dict(BASE, workers_comp_status="no_employees_exempt",
                                                  license_classifications=["B"], completion_date="2028-02-15"),
                                             as_of=AS_OF)
    assert any("past 2028-01-01" in p for p in g.problems)
    g = c.generate_home_improvement_contract(dict(BASE, workers_comp_status="no_employees_exempt",
                                                  signing_date="2028-02-01", start_date="2028-03-01",
                                                  completion_date="2028-05-01"), as_of="2028-02-01")
    assert any("2028-01-01" in p for p in g.problems)
    g = c.generate_home_improvement_contract(dict(BASE, disaster_repair=True, signing_date="2026-01-12"), as_of=AS_OF)
    assert "Seven-Day Right to Cancel" in g.markdown and "within seven business days from the above date" in g.markdown
    assert g.cancellation.last_day_to_cancel == "2026-01-20"


def test_generated_service_and_repair_form():
    sr = dict(BASE, contract_price_usd=385, down_payment_usd=0, progress_payments=[], service_and_repair=True,
              buyer_initiated_contact=True, only_work_needed_for_the_problem=True, service_charge_usd=89,
              buyer_age_65_or_older=True, signing_date="2026-11-24", cgl_status="none")
    g = c.generate_home_improvement_contract(sr, as_of=AS_OF)
    md = g.markdown
    assert g.contract_type == "service_and_repair" and md.startswith("# Service and Repair")
    for s in ["The price must be no more than seven hundred and fifty dollars ($750)",
              "You are entitled to a completely filled in and signed copy", "## Contract Price", "$385.00",
              "OK for contractor to take replaced parts", "## Amount of Service Charge", "$89.00",
              "Sierra Remodel Inc. does not carry commercial general liability insurance.",
              "YOUR RIGHTS TO CANCEL BEFORE WORK BEGINS", "within five business days of signing"]:
        assert s in md, s
    assert "Home Improvement\n" not in md and "Notice of Cancellation\n\n" not in md
    # senior, signed Tue 11/24: Wed 25, Fri 27, Sat 28, Mon 30, Tue 12/1 -> 5th business day 2026-12-01
    assert g.cancellation.last_day_to_cancel == "2026-12-01"
    g = c.generate_home_improvement_contract(dict(sr, contract_price_usd=900), as_of=AS_OF)
    assert g.contract_type == "home_improvement" and "over $750.00" in g.problems[0]
    # $700 + $89 service charge = $789 > $750 -> flagged
    g = c.generate_home_improvement_contract(dict(sr, contract_price_usd=700), as_of=AS_OF)
    assert g.contract_type == "service_and_repair" and any("$789.00" in p for p in g.problems)


# ---------------------------------------------------------------- change orders
def test_good_change_order_and_revised_price():
    # 48,000 original + 1,200 earlier change orders + 2,450 now = 51,650
    r = c.check_change_order("Add 24-inch pantry cabinet", 2450, "Adds $2,450 to payment 3; completion moves to "
                             "2027-03-03", "2026-12-14", "2026-12-14", "2026-12-15", 48000, 1200, True, True,
                             as_of="2026-12-14")
    assert r.enforceable_against_buyer and r.compliant and r.revised_contract_price_usd == 51650.0
    assert r.math == "$48,000.00 original + $1,200.00 prior change orders + $2,450.00 this order = $51,650.00"
    assert r.verified is True


def test_deductive_change_order():
    # Owner drops the backsplash: 48,000 - 1,800 = 46,200
    r = c.check_change_order("Delete tile backsplash", -1800, "Reduces final payment by $1,800; no date change",
                             "2026-12-14", "2026-12-14", "2026-12-20", 48000, contract_says_subcontractors_used=False)
    assert r.enforceable_against_buyer and r.revised_contract_price_usd == 46200.0


def test_change_order_problems():
    r = c.check_change_order("Extra outlet", 350, "No change", "2026-12-16", "2026-12-16", "2026-12-15")
    assert not r.enforceable_against_buyer                                  # signed a day after the work began
    assert any("7031" in n for n in r.notes)
    r = c.check_change_order("Extra outlet", None, "", "2026-12-14", "2026-12-14", "2026-12-15")
    assert not r.enforceable_against_buyer and sum(f.status == "violation" for f in r.findings) == 2
    r = c.check_change_order("Extra outlet", 350, "No change", "2026-12-14", "2026-12-14", "2026-12-15",
                             contract_says_subcontractors_used=True, includes_subcontractor_disclaimer=False,
                             payment_requested_before_work_usd=350)
    assert r.enforceable_against_buyer and not r.compliant                  # discipline issues, still enforceable
    assert {f.item for f in r.findings if f.status == "violation"} == {"Subcontractor disclaimer",
                                                                       "Payment before the changed work"}
    r = c.check_change_order("Extra outlet", 350, "No change", "2026-12-14", "2026-12-14", "2026-12-15",
                             payment_requested_before_work_usd=350, contractor_has_payment_bond_or_joint_control=True)
    assert by_item(r, "Payment before")[0].status == "not_applicable"
    # service and repair: $600 + $200 change = $800 > $750 -> no longer service and repair
    r = c.check_change_order("Second valve", 200, "No change", "2026-12-14", "2026-12-14", "2026-12-14", 600,
                             is_service_and_repair=True)
    assert by_item(r, "Service and repair limit")[0].status == "violation"
    # 2025 change order: SB 517 disclaimer not yet required
    r = c.check_change_order("Extra outlet", 350, "No change", "2025-06-02", "2025-06-02", "2025-06-03",
                             contract_says_subcontractors_used=True)
    assert by_item(r, "Subcontractor disclaimer")[0].status == "not_applicable"


# ---------------------------------------------------------------- MCP
def test_mcp_tools_register_and_run():
    mcp = FastMCP("t")
    c.register(mcp)

    async def run():
        async with Client(mcp) as cl:
            names = {t.name for t in await cl.list_tools()}
            v = await cl.call_tool("validate_home_improvement_contract", {
                "contract_price_usd": 8000, "down_payment_usd": 1000, "as_of": AS_OF,
                "progress_payments": [{"amount_usd": 7000, "for_work": "Fence installed and gate hung"}],
                "buyer_received_signed_copy_on": "2026-11-23"})
            g = await cl.call_tool("generate_home_improvement_contract", {"contract": dict(
                BASE, progress_payments=[p.model_dump() for p in KITCHEN]), "as_of": AS_OF})
            d = await cl.call_tool("home_improvement_cancellation_deadline",
                                   {"received_signed_copy_on": "2026-11-23", "buyer_age_65_or_older": True})
            wc = await cl.call_tool("validate_home_improvement_contract", {
                "contract_price_usd": 18500, "as_of": AS_OF, "workers_comp_notice_says_no_employees": True,
                "license_classifications": ["C-39"], "contract_signed_on": "2026-11-20"})
            co = await cl.call_tool("check_change_order", {"scope_of_work": "Extra outlet", "amount_change_usd": 350,
                                                           "effect_on_payments_or_completion": "No change",
                                                           "buyer_signed_on": "2026-12-14",
                                                           "contractor_signed_on": "2026-12-14",
                                                           "changed_work_starts_on": "2026-12-15"})
            return (names, v.structured_content, g.structured_content, d.structured_content, co.structured_content,
                    wc.structured_content)

    names, v, g, d, co, wc = asyncio.run(run())
    assert names == {"validate_home_improvement_contract", "generate_home_improvement_contract",
                     "check_change_order", "home_improvement_cancellation_deadline"}
    assert v["down_payment"]["max_allowed_usd"] == 800.0 and v["compliant"] is False   # 10% of 8,000 = 800
    assert v["cancellation"]["last_day_to_cancel"] == "2026-11-27" and v["verified"] is False
    assert "# Home Improvement" in g["markdown"]
    assert d["cancellation"]["last_day_to_cancel"] == "2026-11-30"
    assert co["enforceable_against_buyer"] is True
    assert any(f["item"] == "Workers' comp statement is not correct" and f["status"] == "violation"
               for f in wc["findings"])                                     # roofer (C-39) cannot claim exemption
