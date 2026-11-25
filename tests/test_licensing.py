"""Licensing module: CSLB classification, sub prequalification, license obligations and unlicensed exposure.

Demo license fixtures (tradecomply/licenses.py), used because conftest points LICENSE_DB at a missing file:
  123456 Example Plumbing  C-36, active, exp 2027-06-30, WC on file, bond on file
  999999 Lapsed Builders   B, expired 2024-01-15, no WC, no bond
  777000 Solo Roofing      C-39, active, exp 2027-03-31, WC exempt, bond on file
  555000 Solo Electric     C-10, active, exp 2026-12-31, WC exempt, bond on file
"""
import asyncio
import json
import sqlite3

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core, licensing as L

AS_OF = "2026-11-25"


def opt(res, code):
    return next(o for o in res.options if o.code == code)


def unverified(res):
    return {c.rule_id for c in res.citations if not c.verified}


# ================================================================ classification_for_scope
def test_keyword_table_is_built_from_rulebook():
    t = L.class_table()
    assert len(t) == len(core.rule("specialty-classification-list").param("classes")) == 43
    assert t["C-7"].rule_id == "class-c07-low-voltage-91-volt-limit"
    assert t["C-36"].name == "Plumbing"
    assert "metal roofing systems" in t["C-39"].keywords         # from the rulebook's C-39 materials list
    assert "telephone systems" in t["C-7"].keywords               # from the rulebook's C-7 scope list
    assert L.classification_for_scope("new metal roofing system").trade_classes == ["C-39", "C-43"]   # both list it
    assert L._carpentry_codes() == {"C-5", "C-6"}                 # 'Framing and Rough Carpentry', 'Finish Carpentry'


def test_two_trades_go_to_b_and_specialists_need_incidental_test():
    # Water heater (C-36) + new outlets (C-10): 2 unrelated trades besides framing/carpentry -> B prime OK (7057(b)).
    r = L.classification_for_scope("replace water heater, add 2 outlets", "residential_existing", "prime")
    assert r.trade_classes == ["C-10", "C-36"] and r.unrelated_trades_excluding_framing_carpentry == 2
    assert opt(r, "B").status == "yes" and r.can_perform == ["B"]
    assert opt(r, "C-36").status == "conditional" and "incidental and supplemental" in opt(r, "C-36").conditions[0]
    assert opt(r, "B-2").status == "no"                            # 2 < 3 trades for B-2
    assert opt(r, "A").status == "no"
    assert "b-prime-contract-two-trade-rule" in opt(r, "B").rule_ids
    assert r.verified is False and "class-c36-plumbing-scope" in unverified(r)   # class scopes are 'unchecked'


def test_single_trade_b_prime_can_sub_out_but_b_subcontract_cannot():
    sub = L.classification_for_scope("electrical rough and finish", "commercial", "subcontract")
    assert sub.trade_classes == ["C-10"] and sub.unmatched_segments == []
    assert opt(sub, "C-10").status == "yes"
    # B&P 7057(b): a B may take a single-trade subcontract only if it holds that class itself (then it works as a
    # C-10); unlike a prime, subcontracting the work out does not cure it, so B alone is 'no'.
    b = opt(sub, "B")
    assert b.status == "no" and "does not cure" in b.basis and b.conditions == []
    assert "b-subcontract-two-trade-rule" in b.rule_ids
    prime = L.classification_for_scope("electrical rough and finish", "commercial", "prime")
    assert "subcontract the C-10 work" in opt(prime, "B").conditions[0]


def test_framing_alone_is_b_work_and_carpentry_never_counts():
    r = L.classification_for_scope("framing only", "residential_new", "subcontract")
    assert r.unrelated_trades_excluding_framing_carpentry == 0
    assert opt(r, "B").status == "yes" and opt(r, "C-5").status == "yes"
    # Framing + one trade: still only 1 counted trade, so a B prime needs C-10 or a C-10 sub.
    r = L.classification_for_scope("framing and electrical", "commercial", "prime")
    assert r.unrelated_trades_excluding_framing_carpentry == 1 and opt(r, "B").status == "conditional"


def test_b2_needs_three_trades_and_licensed_mep_for_system_work():
    # Kitchen: cabinets C-6, backsplash C-54, sink plumbing C-36, recessed lights C-10, drywall C-9, paint C-33 = 6.
    r = L.classification_for_scope("Kitchen remodel: new cabinets, tile backsplash, relocate sink plumbing, recessed "
                                   "lights, drywall patch and paint", "residential_existing", "prime")
    assert r.trade_classes == ["C-6", "C-9", "C-10", "C-33", "C-36", "C-54"]
    b2 = opt(r, "B-2")
    assert b2.status == "conditional" and "b2-mep-system-restriction" in b2.rule_ids
    assert opt(r, "B").status == "yes"
    # Fixtures only (faucet, light fixture) + paint + flooring = 4 trades, no system work -> B-2 yes.
    r = L.classification_for_scope("replace faucet, replace light fixture, paint, new vinyl plank flooring",
                                   "residential_existing")
    assert len(r.trade_classes) == 4 and not any(t.mep_system_work for t in r.trades)
    assert opt(r, "B-2").status == "yes"
    assert opt(L.classification_for_scope("paint, flooring, tile", "residential_new"), "B-2").status == "no"


def test_fire_sprinklers_only_c16_and_b_must_hold_or_sub():
    r = L.classification_for_scope("fire sprinklers, framing, drywall", "commercial")
    assert opt(r, "B").status == "conditional" and any("C-16" in c for c in opt(r, "B").conditions)
    c16 = opt(r, "C-16")
    assert c16.rule_ids[0] == "fire-protection-system-installer" and "7026.12" in c16.basis
    assert any("C-16 fire protection contractor" in h for h in r.licensed_humans)


def test_solar_battery_80_kwh_limit_for_c46():
    # 13.5 kWh <= 80 kWh -> C-46 may install the battery as incidental to PV (16 CCR 832.46(c)).
    r = L.classification_for_scope("install 8 kW solar panels and a 13.5 kWh battery", "residential_existing",
                                   battery_kwh=13.5)
    assert {"A", "B", "C-10", "C-46"} <= set(r.can_perform)       # 832.62: solar counts as A and B work
    big = L.classification_for_scope("install solar panels and a battery", "commercial", battery_kwh=100)
    c46 = opt(big, "C-46")
    assert c46.status == "conditional" and "over the 80 kWh" in c46.conditions[0]
    assert opt(big, "C-10").status == "yes"
    unknown = L.classification_for_scope("install solar panels and a battery", "commercial")
    assert opt(unknown, "C-46").status == "conditional" and "battery_kwh" in opt(unknown, "C-46").conditions[0]


def test_low_voltage_c7_or_c10():
    r = L.classification_for_scope("security cameras and network cabling", "commercial")
    assert r.trade_classes == ["C-7"] and set(r.can_perform) == {"C-7", "C-10"}
    assert "91 volts" in opt(r, "C-7").basis


def test_civil_work_is_class_a_not_b():
    r = L.classification_for_scope("street paving, storm drain and curb and gutter", "civil_infrastructure")
    assert r.trade_classes == ["C-8", "C-12", "C-42"]
    assert opt(r, "A").status == "yes" and opt(r, "B").status == "no"


def test_adu_whole_build_is_b_or_manufactured_c47_but_a_sub_can_do_its_trade():
    r = L.classification_for_scope("Build a 600 sq ft detached ADU", "adu", "prime")
    assert r.can_perform == ["B"] and r.can_perform_with_conditions == ["C-47"]
    assert "manufactured home built off site" in opt(r, "C-47").conditions[0]
    sub = L.classification_for_scope("ADU: electrical subpanel", "adu", "subcontract")
    assert opt(sub, "C-10").status == "yes"


def test_asbestos_landscape_pool_and_unrecognized_scopes():
    r = L.classification_for_scope("asbestos abatement, drywall and paint", "commercial", asbestos_sq_ft=80)
    assert any("C-22" in c for c in opt(r, "B").conditions)
    assert "not triggered" in opt(r, "C-22").basis                 # 80 < 100 sq ft (B&P 7058.5)
    assert any("DOSH" in h for h in r.licensed_humans)
    pool = L.classification_for_scope("landscape with irrigation and a pool", "residential_existing")
    assert "single licensed C-53" in opt(pool, "C-27").conditions[0]
    none = L.classification_for_scope("make the widget thingamajig")
    assert none.options == [] and none.answer.startswith("No trade recognized")
    assert "class-c61-limited-specialty-scope" in {c.rule_id for c in none.citations}
    with pytest.raises(ValueError):
        L.classification_for_scope("  ")


# ================================================================ bulk_check_subcontractors
SUB_CSV = """License #,Company,Scope of Work,Class,COI Expiration,Employees
123456,Example Plumbing,repipe and water heater,,2027-03-01,yes
999999,Lapsed Builders,framing,,2027-01-01,yes
777000,Solo Roofing,reroof,C-39,12/10/2026,no
555000,Solo Electric,electrical,,2027-05-01,no
424242,Unknown Co,drywall,,2027-01-01,yes
,No Number Inc,paint,,,
123456,Example Plumbing,electrical panel upgrade,,2027-03-01,yes
"""


def test_bulk_csv_risk_table_counts_and_actions():
    r = L.bulk_check_subcontractors(csv_text=SUB_CSV, as_of=AS_OF)
    rows = {(x.row): x for x in r.rows}
    assert r.total == 7
    # Row 7 (plumber for an electrical panel upgrade) is a likely out-of-class job: held, not merely 'medium'.
    assert r.counts_by_risk == {"do_not_use": 1, "high": 4, "medium": 0, "low": 2}
    assert [x.risk for x in r.rows] == sorted((x.risk for x in r.rows), key=L.RISK_ORDER.get)   # worst first
    assert rows[1].risk == "low" and rows[1].classification_source == "inferred_from_scope" and rows[1].classification_match
    assert rows[2].risk == "do_not_use" and "License expired 2024-01-15 (as of 2026-11-25)" in rows[2].problems
    assert rows[2].action == "Do not use or pay: the license expired 2024-01-15."
    # C-39 must carry WC even with no employees; COI 12/10/2026 is 15 days out (<= 30-day reminder window).
    assert rows[3].risk == "high" and rows[3].coi_status == "expiring_soon" and not rows[3].workers_comp_ok
    assert rows[3].coi_expiry == "2026-12-10"
    assert not any("exemption, which ends" in w for w in rows[3].warnings)   # its exemption is already invalid
    # 555000 works alone with an exemption: low now, warned about 2028-01-01 (402 days from 2026-11-25).
    assert rows[4].risk == "low" and any("402 days" in w for w in rows[4].warnings)
    assert rows[4].license_expires_in_days == 36                    # 2026-12-31 - 2026-11-25
    assert rows[5].license_lookup == "no_license_data_loaded" and rows[5].risk == "high"
    assert "does NOT mean" in rows[5].problems[0]
    assert rows[6].license_lookup == "no_license_number"
    assert rows[7].classification_match is False and rows[7].risk == "high" and "C-10" in rows[7].action
    # B&P 7117.6 citation range from 2026-07-01: $500 (B&P 7099.2(b)(1) floor) to $8,000.
    assert any("7117.6" in w and "$500-$8,000" in w for w in rows[7].warnings)
    assert r.top_actions[0].startswith("Do not use") and "#999999" in r.top_actions[0]
    assert any(a.startswith("Confirm on the CSLB site") and "#424242" in a for a in r.top_actions)
    assert "| 3 | 777000 | Solo Roofing |" in r.table_markdown
    assert r.license_data_official is False and r.verified is False             # demo fixtures
    assert {"contracting-with-unlicensed-contractor", "civpen-7118-contracting-with-unlicensed"} <= {
        c.rule_id for c in r.citations}
    assert any("Listed more than once: 123456" in n for n in r.notes)


def test_bulk_rows_follow_as_of_for_license_and_insurance_dates():
    row = {"license_number": "555000", "required_classification": "C-10", "coi_expiry": "2027-01-01",
           "wc_required": False}
    soon = L.bulk_check_subcontractors(rows=[row], as_of="2026-12-15").rows[0]
    assert soon.risk == "low" and soon.license_expires_in_days == 16 and any("expires 2026-12-31" in w for w in soon.warnings)
    late = L.bulk_check_subcontractors(rows=[row], as_of="2027-01-05").rows[0]
    assert late.risk == "do_not_use" and "License expired 2026-12-31 (as of 2027-01-05)" in late.problems
    assert late.coi_status == "expired"                              # 2027-01-01 <= 2027-01-05
    wrong = L.bulk_check_subcontractors(rows=[{"license_number": "123456", "required_classification": "C-10",
                                               "coi_expiry": "2027-03-01"}], as_of=AS_OF).rows[0]
    assert wrong.risk == "do_not_use" and "License does not include C-10 (holds C-36 Plumbing)" in wrong.problems
    assert wrong.action == "Do not use or pay: no C-10 on the license; use a sub holding C-10."


def test_bulk_result_does_not_depend_on_the_real_clock(monkeypatch):
    # verify_license marks a record 'expired' against today; the bulk check must judge expiry by as_of instead.
    from datetime import date
    from tradecomply import logic
    monkeypatch.setattr(logic, "today_ca", lambda: date(2028, 6, 1))          # after 555000 expires 2026-12-31
    row = {"license_number": "555000", "required_classification": "C-10", "coi_expiry": "2027-01-01",
           "wc_required": False}
    r = L.bulk_check_subcontractors(rows=[row], as_of="2026-12-15").rows[0]
    assert (r.risk, r.license_status, r.license_expires_in_days) == ("low", "active", 16)    # 12/31 - 12/15


def test_bulk_2028_workers_comp_exemption_ends(tmp_path, monkeypatch):
    f = tmp_path / "subs.csv"
    f.write_text("license_number,business_name,status,classifications,expiration_date,workers_comp,"
                 "contractor_bond_on_file,disciplinary_actions\n"
                 "3141592,Solo Painting,active,C-33 Painting,2029-06-30,exempt,true,\n")
    monkeypatch.setenv("LICENSE_CSV", str(f))
    row = {"license_number": "3141592", "required_classification": "C-33", "coi_expiry": "2029-01-01",
           "wc_required": False}
    before = L.bulk_check_subcontractors(rows=[row], as_of="2027-12-31").rows[0]
    assert before.risk == "low" and any("1 days" in w for w in before.warnings)
    after = L.bulk_check_subcontractors(rows=[row], as_of="2028-01-01").rows[0]
    assert after.risk == "high" and not after.workers_comp_ok
    assert any("no longer valid from 2028-01-01" in p for p in after.problems)


def _cslb_db(path, complete: bool):
    from tradecomply import cslb
    row = {"LicenseNo": "3000000", "BusinessName": "Real Electric Inc", "PrimaryStatus": "CLEAR",
           "ExpirationDate": "06/30/2028", "Classifications(s)": "C10", "WorkersCompCoverageType":
           "Workers' Compensation Insurance", "WCExpirationDate": "01/01/2028", "CBAmount": "25000"}
    rec = cslb.to_record(row)
    con = sqlite3.connect(path)
    con.execute("create table licenses (license_number text primary key, rec text not null)")
    con.execute("create table meta (k text primary key, v text)")
    con.execute("insert into licenses values (?,?)", (rec["license_number"], json.dumps(rec)))
    con.executemany("insert into meta values (?,?)", [("rows", "1"), ("complete", "1" if complete else "0"),
                                                       ("snapshot_date", "2026-11-24")])
    con.commit()
    con.close()


def test_partial_cslb_copy_never_reported_unlicensed(tmp_path, monkeypatch):
    db = tmp_path / "partial.sqlite"
    _cslb_db(db, complete=False)
    monkeypatch.setenv("LICENSE_DB", str(db))
    r = L.bulk_check_subcontractors(rows=[{"license_number": "4000000", "coi_expiry": "2027-06-01"},
                                          {"license_number": "3000000", "scope_of_work": "electrical service upgrade",
                                           "coi_expiry": "2027-06-01"}], as_of=AS_OF)
    miss = next(x for x in r.rows if x.license_number == "4000000")
    assert miss.license_lookup == "not_in_partial_copy" and miss.risk == "high"
    assert miss.problems == ["Not in our partial copy of CSLB data. This does NOT mean the sub is unlicensed: check "
                             "the number on the CSLB site"]
    assert "unlicensed" not in miss.action.replace("not unlicensed", "")
    found = next(x for x in r.rows if x.license_number == "3000000")
    assert found.risk == "low" and found.classification_match is True
    assert r.license_data_official is True and found.data_source == "CSLB master file (snapshot 2026-11-24)"


def test_complete_cslb_file_miss_is_do_not_use(tmp_path, monkeypatch):
    db = tmp_path / "complete.sqlite"
    _cslb_db(db, complete=True)
    monkeypatch.setenv("LICENSE_DB", str(db))
    row = L.bulk_check_subcontractors(rows=[{"license_number": "4000000"}], as_of=AS_OF).rows[0]
    assert row.license_lookup == "not_in_complete_cslb_file" and row.risk == "do_not_use"
    assert "Treat as not licensed until confirmed" in row.problems[0]


def test_bulk_input_validation():
    with pytest.raises(ValueError):
        L.bulk_check_subcontractors(csv_text=SUB_CSV, rows=[{"license_number": "1"}])
    with pytest.raises(ValueError):
        L.bulk_check_subcontractors()
    with pytest.raises(ValueError):
        L.bulk_check_subcontractors(csv_text="name,scope\nA,paint\n")
    with pytest.raises(ValueError):
        L.SubInput(license_number="1", wc_required="maybe")
    assert L.SubInput(license_number=" 123456 ", wc_required="N").wc_required is False


# ================================================================ company_license_obligations
def test_llc_bonds_insurance_wc_and_renewal():
    r = L.company_license_obligations(
        "llc", employee_count=12, member_count_for_llc=8, classifications=["B", "C-10"],
        license_expiration_date="2027-01-31", has_qualifier=True, qualifier_is_rme_or_rmo=True, as_of=AS_OF,
        qualifier_ownership_percent=5)
    bonds = {b.item: b for b in r.bonds}
    # 25,000 contractor + 25,000 qualifier (RMO owns 5% < 10%) + 100,000 LLC worker bond = 150,000
    assert bonds["Contractor's bond"].amount_usd == 25000 and bonds["Bond of qualifying individual"].required
    assert bonds["LLC employee/worker bond"].amount_usd == 100000 and r.total_bonds_usd == 150000
    ins = r.insurance[0]
    assert ins.amount_usd == 1_300_000                              # 1,000,000 + (8 - 5) x 100,000
    assert "$1,800,000" in ins.detail                               # literal reading: 1,000,000 + 8 x 100,000
    assert r.workers_comp.requirement == "certificate_required"
    rn = r.renewal
    assert (rn.renewal_fee_usd, rn.c10_surcharge_usd, rn.on_time_total_usd) == (700, 20, 720)     # entity active
    assert (rn.delinquency_fee_usd, rn.late_total_usd) == (350, 1070)                             # 50% x 700
    assert rn.retroactive_renewal_deadline == "2027-05-01"           # 2027-01-31 + 90 days
    assert rn.next_expiration_if_renewed == "2029-01-31" and rn.renewal_window_ends == "2032-01-31"
    assert rn.days_until_expiration == 67 and rn.status == "current"
    assert r.verified is True


def test_llc_insurance_cap_and_corporation_rmo_ten_percent_exemption():
    big = L.company_license_obligations("llc", member_count_for_llc=50, as_of=AS_OF)
    assert big.insurance[0].amount_usd == 5_000_000                # 1,000,000 + 45 x 100,000 = 5.5M -> capped
    corp = L.company_license_obligations("corporation", employee_count=3, qualifier_is_rme_or_rmo=True,
                                         qualifier_ownership_percent=25, as_of=AS_OF)
    q = next(b for b in corp.bonds if b.item == "Bond of qualifying individual")
    assert q.required is False and "exempt" in q.math and corp.total_bonds_usd == 25000
    assert corp.insurance[0].required is False                      # CGL not required by license law (non-LLC)
    llc = L.company_license_obligations("llc", as_of=AS_OF)        # no ownership info: bond assumed needed
    assert llc.total_bonds_usd == 150000 and any("treated as an RMO" in n for n in llc.notes)


def test_sole_owner_exemption_and_late_renewal():
    r = L.company_license_obligations("sole_owner", classifications=["C-33"], license_expiration_date="2026-09-30",
                                      as_of=AS_OF)
    wc = r.workers_comp
    assert wc.requirement == "exemption_allowed" and wc.days_until_exemption_ends == 402   # to 2028-01-01
    assert any("within 90 days of the hire" in a for a in wc.actions)
    rn = r.renewal
    # Expired 56 days ago: inside the 90-day retroactive window. 450 + 50% x 450 = 675.
    assert rn.status == "expired_retroactive_window" and rn.days_until_expiration == -56 and rn.late_total_usd == 675
    assert rn.renew_by == "2026-12-29"                              # 2026-09-30 + 90 days: last day with no gap
    assert r.deadlines[0].due_date == "2026-12-29" and r.deadlines[0].days_remaining == 34
    assert r.deadlines[-1].due_date == "2028-01-01"
    assert r.total_bonds_usd == 25000 and r.verified is True


def test_workers_comp_mandatory_class_rme_and_2028():
    roof = L.company_license_obligations("sole_owner", classifications=["C-39"], as_of=AS_OF)
    assert roof.workers_comp.requirement == "certificate_required" and roof.workers_comp.mandatory_classes_held == ["C-39"]
    rme = L.company_license_obligations("sole_owner", classifications=["C-10"], qualifier_is_rme_or_rmo=True,
                                        as_of=AS_OF)
    assert rme.workers_comp.requirement == "certificate_required"
    assert next(b for b in rme.bonds if b.item == "Bond of qualifying individual").required    # RME: no exemption
    assert "wc-exemption-barred-rme-or-employees" in unverified(rme) and rme.verified is False
    assert any("32 hours" in n for n in rme.notes)
    later = L.company_license_obligations("sole_owner", classifications=["C-33"], as_of="2028-01-15")
    assert later.workers_comp.requirement == "certificate_required"
    assert "From 2028-01-01" in later.workers_comp.reasons[0]


def test_inactive_license_needs_no_bonds_or_wc():
    r = L.company_license_obligations("partnership", license_status="inactive", license_expiration_date="2027-08-31",
                                      as_of=AS_OF)
    assert r.total_bonds_usd == 0 and r.workers_comp.requirement == "not_required_while_inactive"
    assert r.renewal.renewal_fee_usd == 500                         # entity inactive renewal
    assert "inactive-license-no-bond-qualifier-wc" in {c.rule_id for c in r.citations}
    corp = L.company_license_obligations("corporation", license_status="inactive", as_of=AS_OF)
    assert any("good standing" in n for n in corp.notes)            # B&P 7076.2 has no active-only limit


def test_change_notice_deadlines_and_missing_qualifier():
    r = L.company_license_obligations(
        "corporation", employee_count=4, qualifier_is_rme_or_rmo=True, has_qualifier=False, as_of=AS_OF,
        changes=[{"change": "qualifier_left", "date": "2026-10-15"}, {"change": "mailing_address", "date": "2026-11-10"},
                 {"change": "business_name", "date": "2026-09-01"}, {"change": "personnel", "date": "2026-11-01"}])
    by = {d.item: d for d in r.deadlines}
    name = next(d for d in r.deadlines if "Business name" in d.item)
    assert name.due_date == "2026-11-30" and name.days_remaining == 5 and name.status == "due_soon"   # 9/1 + 90
    assert "$100" in name.consequence and "$500-$1,000" in name.consequence
    mail = next(d for d in r.deadlines if "Mailing address" in d.item)
    assert mail.due_date == "2026-12-10"                            # 30 days (B&P 136), not 90
    q = [d for d in r.deadlines if "qualifier" in d.item]
    assert {d.due_date for d in q} == {"2027-01-13"}                # 2026-10-15 + 90 days
    assert any("$230" in d.item for d in q)
    pers = next(d for d in r.deadlines if "Personnel" in d.item)
    assert pers.due_date == "2027-01-30" and "$125" in pers.consequence
    assert [d.due_date for d in r.deadlines] == sorted(d.due_date for d in r.deadlines)
    assert any("extension" in n for n in r.notes) and len(by) == 5
    no_q = L.company_license_obligations("sole_owner", has_qualifier=False, as_of=AS_OF)
    assert no_q.actions[0].startswith("No qualifier")
    with pytest.raises(ValueError):
        L.company_license_obligations("joint_venture")


# ================================================================ unlicensed_contracting_exposure
def test_minor_work_exemption_under_1000():
    r = L.unlicensed_contracting_exposure(950, "replace faucet and toilet", False, requires_building_permit=False,
                                          as_of=AS_OF)
    assert r.minor_work_exemption == "applies" and r.license_required is False
    assert r.exposure == [] and r.total_high_usd == 0 and r.verified is True
    exact = L.unlicensed_contracting_exposure(1000, "replace faucet and toilet", False, requires_building_permit=False,
                                              as_of=AS_OF)
    assert exact.minor_work_exemption == "does_not_apply" and exact.exemption_tests[0].passed is False
    # 1,000 contract value + citation 1,500-15,000 + first-offense fine 0-5,000
    assert (exact.total_low_usd, exact.total_high_usd) == (2500, 21000)
    unknown = L.unlicensed_contracting_exposure(900, "fence repair", False, as_of=AS_OF)
    assert unknown.minor_work_exemption == "depends" and unknown.license_required is None
    permit = L.unlicensed_contracting_exposure(600, "water heater swap", False, requires_building_permit=True,
                                               as_of=AS_OF)
    assert permit.license_required is True                         # a permit kills the exemption at any price
    helpers = L.unlicensed_contracting_exposure(500, "paint", False, requires_building_permit=False,
                                                advertises_as_contractor=True, has_employees_or_helpers=True, as_of=AS_OF)
    assert [t.passed for t in helpers.exemption_tests] == [True, True, True, False, False]
    ad = next(e for e in helpers.exposure if "advertising" in e.item)
    assert (ad.low_usd, ad.high_usd) == (700, 1000)


def test_unlicensed_bathroom_remodel_first_offense():
    r = L.unlicensed_contracting_exposure(18500, "bathroom remodel", False, amount_paid_usd=12000, as_of=AS_OF,
                                          last_activity_date="2026-10-01")
    items = {e.item.split(" (")[0]: e for e in r.exposure}
    assert items["Unpaid balance you cannot sue to collect"].high_usd == 6500        # 18,500 - 12,000
    assert items["Payments the customer can sue to recover"].high_usd == 12000
    cit = next(e for e in r.exposure if "7028.7" in e.item)
    assert (cit.low_usd, cit.high_usd) == (1500, 15000)                             # operative 2026-07-01
    # low = 6,500 + 12,000 + 1,500 + 0 = 20,000; high = 6,500 + 12,000 + 15,000 + 5,000 = 38,500
    assert (r.total_low_usd, r.total_high_usd) == (20000, 38500)
    assert r.prosecution_deadline == "2030-10-01"                                   # 4 years after last activity
    assert any("liens" in o.lower() for o in r.other_consequences)
    assert any("$50,000 bond" in o for o in r.other_consequences)
    assert r.verified is True


def test_repeat_offense_fines():
    # 2nd: greater of 20% x 60,000 = 12,000, 20% x 40,000 = 8,000, or 5,000 -> 12,000 (fixed).
    r2 = L.unlicensed_contracting_exposure(60000, "kitchen remodel", False, amount_paid_usd=40000,
                                           prior_unlicensed_convictions=1, as_of=AS_OF)
    fine = next(e for e in r2.exposure if e.kind == "criminal")
    assert (fine.low_usd, fine.high_usd) == (12000, 12000)
    assert (r2.total_low_usd, r2.total_high_usd) == (73500, 87000)   # 20k + 40k + 1.5k|15k + 12k
    # 3rd: 5,000 up to greater of 10,000, 20% x 30,000 = 6,000, 20% x 10,000 = 2,000 -> 10,000.
    r3 = L.unlicensed_contracting_exposure(30000, "roof", False, amount_paid_usd=10000, prior_unlicensed_convictions=2,
                                           as_of=AS_OF)
    fine = next(e for e in r3.exposure if e.kind == "criminal")
    assert (fine.low_usd, fine.high_usd) == (5000, 10000)
    assert any("90 days to 1 year" in o for o in r3.other_consequences)


def test_citation_minimum_before_july_2026_and_disaster():
    old = L.unlicensed_contracting_exposure(5000, "deck", False, as_of="2026-06-15")
    cit = next(e for e in old.exposure if "7028.7" in e.item)
    assert cit.low_usd == 200 and cit.rule_ids == ["unlicensed-citation-civil-penalty-pre-july-2026"]
    dis = L.unlicensed_contracting_exposure(25000, "fire damage repair", False, declared_disaster_repair=True,
                                            public_agency_bid=True, as_of=AS_OF)
    d = next(e for e in dis.exposure if "7028.16" in e.item)
    assert d.high_usd == 10000 and any("void" in o for o in dis.other_consequences)


def test_hiring_contractor_perspective_and_licensed_party():
    r = L.unlicensed_contracting_exposure(8000, "drywall", False, perspective="hiring_contractor", as_of=AS_OF)
    assert len(r.exposure) == 1 and (r.exposure[0].low_usd, r.exposure[0].high_usd) == (1500, 30000)
    assert any("2 years probation" in o for o in r.other_consequences)
    assert "lc-2750-5-license-required-for-ic-status" in unverified(r) and r.verified is False
    old = L.unlicensed_contracting_exposure(8000, "drywall", False, perspective="hiring_contractor", as_of="2026-06-01")
    assert old.exposure[0].low_usd == 500
    ok = L.unlicensed_contracting_exposure(8000, "drywall", True, as_of=AS_OF)
    assert ok.minor_work_exemption == "not_applicable_licensed" and ok.exposure == []
    lapsed = L.unlicensed_contracting_exposure(8000, "drywall", True, license_lapsed_during_work=True,
                                               previously_licensed_in_california=True, amount_paid_usd=8000, as_of=AS_OF)
    assert lapsed.exposure[0].high_usd == 0 and lapsed.exposure[1].high_usd == 8000    # 0 unpaid, 8,000 paid back
    assert any("Substantial compliance" in n for n in lapsed.notes) and "substantial-compliance-doctrine" in unverified(lapsed)
    with pytest.raises(ValueError):
        L.unlicensed_contracting_exposure(1000, "x", False, amount_paid_usd=2000)


# ================================================================ law-accuracy review fixes
def test_asbestos_and_underground_tank_wording():
    r = L.classification_for_scope("asbestos abatement and drywall", "commercial", asbestos_sq_ft=250)
    h = next(x for x in r.licensed_humans if "Asbestos" in x)
    # B&P 7180.5 binds the owner only when it hires a consultant; DOSH registration is B&P 7058.6.
    assert "if the owner hires an asbestos consultant" in h and "registered with DOSH" in h
    assert {"asbestos-cert-dosh-registration-90-days", "asbestos-100-sqft-c22-or-certification"} <= {
        c.rule_id for c in r.citations}
    ust = L.classification_for_scope("remove underground storage tank and backfill", "commercial")
    assert any("only if a certified contractor performs it" in n for n in ust.notes)   # B&P 7058.7(e)(2)
    assert any("B&P 7117.6" in n for n in ust.notes)


def _csv_license(tmp_path, monkeypatch, *lines):
    f = tmp_path / "subs.csv"
    f.write_text("license_number,business_name,status,classifications,expiration_date,workers_comp,"
                 "contractor_bond_on_file,disciplinary_actions\n" + "".join(l + "\n" for l in lines))
    monkeypatch.setenv("LICENSE_CSV", str(f))


def test_bulk_multi_class_sub_and_c10_covers_low_voltage(tmp_path, monkeypatch):
    _csv_license(tmp_path, monkeypatch, "2718281,Multi Trade Inc,active,C-10 Electrical;C-36 Plumbing,2028-03-31,"
                                        "on_file,true,")
    rows = L.bulk_check_subcontractors(rows=[
        {"license_number": "2718281", "scope_of_work": "repipe and add outlets", "coi_expiry": "2027-06-01"},
        {"license_number": "2718281", "required_classification": "C-7", "coi_expiry": "2027-06-01"},
        {"license_number": "2718281", "scope_of_work": "repipe, add outlets and hvac ductwork",
         "coi_expiry": "2027-06-01"}], as_of=AS_OF)
    by = {x.row: x for x in rows.rows}
    # C-36 + C-10 held covers both trades (16 CCR 830(b)), though no single class is 'yes' for the 2-trade scope.
    assert by[1].classification_match is True and by[1].risk == "low"
    # A C-10 does electrical work 'in any form' (16 CCR 832.10), so a C-7 requirement is met, not 'do not use'.
    assert by[2].classification_match is True and by[2].risk == "low" and by[2].problems == []
    assert "class-c10-electrical-scope" in unverified(rows)
    # HVAC (C-20) is the only uncovered trade: conditional -> medium, and the warning names only C-20.
    assert by[3].classification_match is None and by[3].risk == "medium"
    w = by[3].warnings[0]
    assert w.startswith("Not in its classes: hvac ductwork (C-20)") and "C-36" not in w
    assert by[3].action.startswith("Before award")


def test_bulk_cslb_data_loading_is_not_unlicensed(monkeypatch):
    monkeypatch.setenv("CSLB_AUTO_DOWNLOAD", "1")                  # hosted server, database not built yet
    r = L.bulk_check_subcontractors(rows=[{"license_number": "123456", "coi_expiry": "2027-06-01"}], as_of=AS_OF)
    row = r.rows[0]
    assert row.license_lookup == "cslb_data_loading" and row.risk == "high"
    assert "does NOT mean the sub is unlicensed" in row.problems[0]
    assert any(a.startswith("Confirm on the CSLB site") for a in r.top_actions)


def test_citation_minimums_follow_7099_2_operative_date():
    # B&P 7099.2(b)(1) floors start 2026-07-01: 7118 $500 -> $1,500 and 7083 $100 -> $500 (16 CCR 884 before).
    row = [{"license_number": "123456", "required_classification": "C-36", "coi_expiry": "2027-06-01"}]
    assert "$500-$30,000" in L.bulk_check_subcontractors(rows=row, as_of="2026-06-15").notes[0]
    after = L.bulk_check_subcontractors(rows=row, as_of=AS_OF)
    assert "$1,500-$30,000" in after.notes[0] and "civil-penalty-range-from-july-2026" in {
        c.rule_id for c in after.citations}
    ch = [{"change": "business_address", "date": "2026-06-01"}]
    before = L.company_license_obligations("sole_owner", changes=ch, as_of="2026-06-15").deadlines
    assert "$100-$1,000" in next(d for d in before if "address" in d.item).consequence
    now = L.company_license_obligations("sole_owner", changes=ch, as_of=AS_OF).deadlines
    assert "$500-$1,000" in next(d for d in now if "address" in d.item).consequence


def test_mailing_address_and_qualifier_bond_exemption_notices():
    r = L.company_license_obligations(
        "corporation", qualifier_is_rme_or_rmo=True, qualifier_ownership_percent=20, as_of=AS_OF,
        changes=[{"change": "mailing_address", "date": "2026-11-10"},
                 {"change": "qualifier_bond_exemption", "date": "2026-11-01"}])
    mail = next(d for d in r.deadlines if "Mailing" in d.item)
    # Shown at 30 days (B&P 136), but B&P 7083(a) 'notwithstanding any other law' gives 90: both are explained.
    assert mail.due_date == "2026-12-10" and "B&P 7083(a)" in mail.consequence and "90 days" in mail.consequence
    q = next(d for d in r.deadlines if "bond exemption" in d.item)
    assert q.due_date == "2027-01-30"                                   # 2026-11-01 + 90 days (B&P 7083)
    assert q.consequence.startswith("File the $25,000 qualifier bond now if ownership fell below 10%")
    assert "qualifier-bond-10-percent-owner-exemption" in q.rule_ids
    assert r.verified is True


def test_no_qualifier_inactive_vs_active():
    inactive = L.company_license_obligations("partnership", license_status="inactive", has_qualifier=False, as_of=AS_OF)
    assert not any(a.startswith("No qualifier") for a in inactive.actions)        # B&P 7076.5(d)
    assert any("No qualifier is required while the license is inactive" in n for n in inactive.notes)
    active = L.company_license_obligations("sole_owner", has_qualifier=False, as_of=AS_OF)
    q = next(b for b in active.bonds if b.item == "Bond of qualifying individual")
    assert q.required is False and q.math.startswith("no qualifier on file now")


def test_mandatory_wc_class_removal_d49_and_2028_rules():
    multi = L.company_license_obligations("sole_owner", classifications=["C-39", "C-10"], as_of=AS_OF)
    assert any("removes C-39" in a for a in multi.workers_comp.actions)          # B&P 7125(f)
    assert "c39-classification-removed-without-wc" in unverified(multi) and multi.verified is False
    single = L.company_license_obligations("sole_owner", classifications=["C-39"], as_of=AS_OF)
    assert not any("removes" in a for a in single.workers_comp.actions) and single.verified is True
    tree = L.company_license_obligations("sole_owner", classifications=["C-61/D-49"], as_of=AS_OF)
    assert tree.workers_comp.mandatory_classes_held == ["D-49"] and tree.workers_comp.requirement == "certificate_required"
    later = L.company_license_obligations("sole_owner", classifications=["C-33"], as_of="2028-02-01")
    assert "workers-comp-exemption-until-2028" not in later.workers_comp.rule_ids   # repealed 2028-01-01


def test_renewal_grace_and_retroactive_window_explained():
    r = L.company_license_obligations("llc", license_expiration_date="2027-01-31", as_of=AS_OF)
    assert any("within 30 days after expiration" in m for m in r.renewal.math)     # B&P 7141(b)
    renew = next(d for d in r.deadlines if d.item.startswith("Renew the license"))
    assert "within 90 days" in renew.consequence and "B&P 7141.5" in renew.consequence
    assert any("personnel of record (B&P 7028.5)" in n for n in r.notes)


def test_disaster_fine_is_an_alternative_not_added():
    # 25,000 job, nothing paid, first offense, declared disaster:
    # 7031 25,000 + citation 1,500-15,000 + larger of 7028 fine (0-5,000) and 7028.16 fine (0-10,000) = 10,000
    # low = 25,000 + 1,500 + 0 = 26,500; high = 25,000 + 15,000 + 10,000 = 50,000 (was 55,000 when both were added)
    r = L.unlicensed_contracting_exposure(25000, "fire damage repair", False, declared_disaster_repair=True, as_of=AS_OF)
    assert (r.total_low_usd, r.total_high_usd) == (26500, 50000)
    f7028 = next(e for e in r.exposure if e.item.startswith("Criminal fine"))
    f16 = next(e for e in r.exposure if "7028.16" in e.item)
    assert f7028.counted_in_total is False and f16.counted_in_total is True
    assert r.verified is False and any("Penal Code 654" in n for n in r.notes)     # UNVERIFIED source
    # Second offense: 20% x 60,000 = 12,000 > 10,000, so the 7028 fine counts and totals match the no-disaster case.
    r2 = L.unlicensed_contracting_exposure(60000, "kitchen", False, amount_paid_usd=40000, prior_unlicensed_convictions=1,
                                           declared_disaster_repair=True, public_agency_bid=True, as_of=AS_OF)
    assert (r2.total_low_usd, r2.total_high_usd) == (73500, 87000)
    assert any("separate misdemeanor" in o for o in r2.other_consequences)       # B&P 7028.15(a)


def test_lapsed_licensee_and_hiring_contractor_notes():
    lapsed = L.unlicensed_contracting_exposure(8000, "drywall", True, license_lapsed_during_work=True, as_of=AS_OF)
    assert any("B&P 7117.5: $500-$8,000" in o and "Revocation, stayed, 2 years probation" in o
               for o in lapsed.other_consequences)
    old = L.unlicensed_contracting_exposure(8000, "drywall", True, license_lapsed_during_work=True, as_of="2026-06-01")
    assert any("B&P 7117.5: $200-$8,000" in o for o in old.other_consequences)   # 16 CCR 884 before 7099.2(b)(1)
    gc = L.unlicensed_contracting_exposure(800, "patch drywall", False, perspective="hiring_contractor",
                                           requires_building_permit=False, as_of=AS_OF)
    assert any("B&P 7048(b)" in n for n in gc.notes)
    hired = L.unlicensed_contracting_exposure(8000, "drywall", False, perspective="hiring_contractor", as_of=AS_OF)
    assert any("cannot sue you for any unpaid balance" in o for o in hired.other_consequences)


# ================================================================ code + realism review regressions
def test_non_load_bearing_wall_is_not_structural():
    # The regex had a literal backspace instead of \b, so 'non-load bearing' was flagged structural (B-2 'no').
    r = L.classification_for_scope("remove non-load bearing wall, patch drywall, paint", "residential_existing")
    assert not any(t.structural for t in r.trades) and opt(r, "B-2").status == "yes"     # C-5 + C-9 + C-33 = 3
    assert L.classification_for_scope("remove load bearing wall", "residential_existing").trades[0].structural


def test_contractor_vocabulary_demo_fan_doorbell_hpwh():
    assert L.classification_for_scope("demo kitchen").trade_classes == ["C-21"]
    assert L.classification_for_scope("install doorbell camera").trade_classes == ["C-7"]
    assert L.classification_for_scope("replace bath exhaust fan").trade_groups == [["C-10", "C-20"]]   # either
    assert set(L.classification_for_scope("heat pump water heater").can_perform) == {"C-20", "C-36"}


def test_civil_only_work_needs_class_a_not_the_specialty(tmp_path, monkeypatch):
    # 'underground utilities' matches no specialty: it is Class A fixed work (B&P 7056), so C-12 is conditional.
    r = L.classification_for_scope("site grading and underground utilities", "civil_infrastructure")
    assert opt(r, "A").status == "yes" and opt(r, "C-12").status == "conditional"
    assert "Class A fixed work" in opt(r, "C-12").conditions[0]
    _csv_license(tmp_path, monkeypatch, "5555555,Dirt Co,active,C-12,2028-01-31,on_file,true,",
                 "6666666,Eng Co,active,A,2028-01-31,on_file,true,")
    rows = L.bulk_check_subcontractors(rows=[
        {"license_number": n, "scope_of_work": "site grading and underground utilities", "coi_expiry": "2027-06-01",
         "project_type": "civil_infrastructure"} for n in ("5555555", "6666666")], as_of=AS_OF).rows
    by = {x.license_number: x for x in rows}
    assert by["5555555"].risk == "medium" and "underground utilities (A)" in by["5555555"].warnings[0]
    assert by["6666666"].risk == "low" and by["6666666"].classification_match is True


def test_bulk_required_class_formats_names_and_b_coverage(tmp_path, monkeypatch):
    _csv_license(tmp_path, monkeypatch, "1111111,Tree Co,active,C-61/D-49,2028-01-31,on_file,true,",
                 "2222222,Concrete Co,active,C-8 Concrete,2028-01-31,on_file,true,",
                 "3333333,Volt Co,active,C-10,2028-01-31,on_file,true,",
                 "7777777,GC Inc,active,B,2028-01-31,on_file,true,")
    coi = "2027-06-01"
    reqs = [("1111111", "D-49"), ("2222222", "C-08"), ("3333333", "Electrical"), ("3333333", "C-46"),
            ("7777777", "C-10, C-36"), ("7777777", "C-61/D-12"), ("7777777", "B-2")]
    res = L.bulk_check_subcontractors(rows=[{"license_number": n, "required_classification": c, "coi_expiry": coi}
                                            for n, c in reqs], as_of=AS_OF)
    by = {x.row: x for x in res.rows}
    # Used to be do_not_use: D-49 inside 'C-61/D-49', 'C-08' vs 'C-8', and a class name instead of a code.
    assert [by[i].risk for i in (1, 2, 3)] == ["low", "low", "low"] and by[3].acceptable_classifications == ["C-10"]
    # C-10 covers PV but not solar thermal (16 CCR 832.10): conditional, not do_not_use.
    assert by[4].classification_match is None and by[4].risk == "medium" and "PV only" in by[4].warnings[0]
    # B&P 7057(b): a B may take one subcontract with 2 unrelated trades without holding either class.
    assert by[5].risk == "medium" and "B&P 7057(b)" in by[5].warnings[0]
    # 'C-61/D-12' means the D-12 subcategory; a B does not cover it.
    assert by[6].risk == "do_not_use" and by[6].action == "Do not use or pay: no D-12 on the license; use a sub holding D-12."
    assert by[7].risk == "medium" and by[7].action.startswith("Before award: confirm 2+ of the trades")
    # Not a class code: read as the work description and inferred (plumber -> C-36, which 123456 holds).
    guess = L.bulk_check_subcontractors(rows=[{"license_number": "123456", "required_classification": "plumber",
                                               "coi_expiry": coi}], as_of=AS_OF).rows[0]
    assert guess.classification_source == "inferred_from_scope" and guess.classification_match is True
    assert "not a CSLB class code" in guess.warnings[0]


def test_bulk_csv_pasted_from_spreadsheet():
    # Tab-separated with a BOM, 'TBD' insurance, head counts in an Employees column and a pipe in a name.
    tsv = ("\ufeffLicense #\tCompany Name\tTrade\tCOI Exp\tEmployees\n"
           "555000\tSolo Electric\telectrical\tTBD\t2\n\n"
           "555000\tSolo Electric\tlighting\t2027-06-01\t3\n"
           "424242\tA | B Drywall\tdrywall\t2027-06-01\t0\n")
    r = L.bulk_check_subcontractors(csv_text=tsv, as_of=AS_OF)
    by = {x.row: x for x in r.rows}
    assert r.total == 3 and by[1].coi_status == "missing"
    assert "Liability insurance expiry 'TBD' is not a date: get the current certificate" in by[1].problems
    assert by[2].workers_comp_ok is False                  # 3 employees with only an exemption on file
    assert "| A \\| B Drywall |" in r.table_markdown        # pipe escaped so the markdown table keeps 9 columns
    wc = next(a for a in r.top_actions if a.startswith("Get a workers' comp"))
    assert wc.endswith("(2 rows): #555000 Solo Electric")  # one sub listed twice is named once
    with pytest.raises(ValueError, match=r"row 2 \(CSV line 3\): wc_required"):
        L.bulk_check_subcontractors(csv_text="license_number,employees\n123456,yes\n555000,maybe\n")
    with pytest.raises(ValueError, match="row 1: value"):
        L.bulk_check_subcontractors(rows=["123456"])
    assert L.bulk_check_subcontractors(rows=[{"license_number": 123456}], as_of=AS_OF).rows[0].license_lookup == "found"


def test_python_callers_get_clear_errors_for_bad_choices():
    for call in (lambda: L.classification_for_scope("paint", project_type="house"),
                 lambda: L.classification_for_scope("paint", contract_role="sub"),
                 lambda: L.classification_for_scope("solar and battery", battery_kwh=-1),
                 lambda: L.company_license_obligations("sole_owner", license_status="suspended"),
                 lambda: L.company_license_obligations("corporation", qualifier_ownership_percent=150),
                 lambda: L.unlicensed_contracting_exposure(float("nan"), "deck", False),
                 lambda: L.unlicensed_contracting_exposure(100, "deck", False, perspective="owner")):
        with pytest.raises(ValueError):
            call()
    # A comma string of classes is split, so the C-10 surcharge is found: entity active 700 + 20 = 720.
    r = L.company_license_obligations("corporation", classifications="B, C-10", license_expiration_date="2027-06-30",
                                      as_of=AS_OF)
    assert r.renewal.on_time_total_usd == 720


def test_exposure_math_in_whole_cents():
    # 12,345.675 -> 12,345.68 and 1,000.335 -> 1,000.34 first, so the printed math adds up:
    # unpaid 12,345.68 - 1,000.34 = 11,345.34; low = 11,345.34 + 1,000.34 + 1,500 + 0 = 13,845.68;
    # high = 11,345.34 + 1,000.34 + 15,000 + 5,000 = 32,345.68
    r = L.unlicensed_contracting_exposure(12345.675, "deck", False, amount_paid_usd=1000.335, as_of=AS_OF)
    assert r.contract_price_usd == 12345.68 and r.amount_paid_usd == 1000.34
    assert r.exposure[0].math == "$12,345.68 - $1,000.34 paid = $11,345.34"
    assert (r.total_low_usd, r.total_high_usd) == (13845.68, 32345.68)


def test_7028_prosecution_window_passed_and_citation_limit():
    # Last activity 2021-06-01 + 4 years = 2025-06-01, before 2026-11-25: the 7028 fine is shown, not counted.
    # low = 5,000 + 1,500 = 6,500; high = 5,000 + 15,000 = 20,000 (was 25,000 with the barred 0-5,000 fine)
    r = L.unlicensed_contracting_exposure(5000, "deck", False, last_activity_date="2021-06-01", as_of=AS_OF)
    fine = next(e for e in r.exposure if e.item.startswith("Criminal fine"))
    assert fine.counted_in_total is False and (r.total_low_usd, r.total_high_usd) == (6500, 20000)
    assert r.prosecution_deadline == "2025-06-01" and any("which has passed" in n for n in r.notes)
    assert any("18 months after a complaint" in n for n in r.notes)          # B&P 7028.9
    assert "unlicensed-citation-limitations" in {c.rule_id for c in r.citations}
    # Disaster job, 7028 barred: the 7028.16 fine (not covered by 7028(g)) counts. 25,000 + 1,500|15,000 + 0|10,000.
    d = L.unlicensed_contracting_exposure(25000, "fire repair", False, declared_disaster_repair=True,
                                          last_activity_date="2021-06-01", as_of=AS_OF)
    assert (d.total_low_usd, d.total_high_usd) == (26500, 50000)
    assert next(e for e in d.exposure if "7028.16" in e.item).counted_in_total is True
    gc = L.unlicensed_contracting_exposure(5000, "drywall", True, perspective="hiring_contractor", as_of=AS_OF)
    assert gc.notes[0].startswith("The sub is licensed: confirm on the CSLB site")


def test_renewal_after_expiry_and_weekend_deadlines():
    # Expired 2024-11-30, renewed late: keeps the cycle (B&P 7141(c)), so it expires again 2026-11-30, 5 days out.
    gap = L.company_license_obligations("sole_owner", classifications=["C-33"], license_expiration_date="2024-11-30",
                                        as_of=AS_OF).renewal
    assert gap.status == "expired_gap" and gap.next_expiration_if_renewed == "2026-11-30"
    assert any("expires 2026-11-30 (5 days" in m for m in gap.math)
    old = L.company_license_obligations("sole_owner", license_expiration_date="2020-01-31", as_of=AS_OF).renewal
    assert old.status == "expired_over_5_years" and old.next_expiration_if_renewed is None and old.renew_by is None
    assert any("can no longer be renewed" in m for m in old.math)
    retro = L.company_license_obligations("sole_owner", license_expiration_date="2026-09-30", as_of=AS_OF)
    # 450 + 50% x 450 = 675, due 2026-09-30 + 90 days = 2026-12-29
    assert any("$675" in a and "by 2026-12-29" in a for a in retro.actions)
    # 2027-01-31 is a Sunday: act by Friday 2027-01-29. 2026-10-03 + 90 = 2027-01-01, a Friday: unchanged.
    r = L.company_license_obligations("sole_owner", license_expiration_date="2027-01-31", as_of=AS_OF,
                                      changes=[{"change": "business_address", "date": "2026-10-03"}])
    renew = next(d for d in r.deadlines if d.item.startswith("Renew the license"))
    assert (renew.weekday, renew.act_by) == ("Sunday", "2027-01-29") and "by Friday 2027-01-29" in renew.consequence
    addr = next(d for d in r.deadlines if "address" in d.item)
    assert (addr.due_date, addr.weekday, addr.act_by) == ("2027-01-01", "Friday", "2027-01-01")
    wc = next(d for d in r.deadlines if d.item.startswith("Workers' comp exemption ends"))
    assert (wc.weekday, wc.act_by) == ("Saturday", "2027-12-31")


# ================================================================ MCP
def test_mcp_tools_register_and_run():
    mcp = FastMCP("t")
    L.register(mcp)

    async def run():
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
            bulk = await c.call_tool("bulk_check_subcontractors", {"rows": [
                {"license_number": "777000", "required_classification": "C-39", "coi_expiry": "2027-06-01",
                 "wc_required": False}], "as_of": AS_OF})
            cls = await c.call_tool("classification_for_scope", {"scope_of_work": "replace water heater, add 2 outlets",
                                                                 "project_type": "residential_existing"})
            obl = await c.call_tool("company_license_obligations", {
                "entity_type": "llc", "employee_count": 2, "member_count_for_llc": 3, "as_of": AS_OF,
                "qualifier_is_rme_or_rmo": True, "qualifier_ownership_percent": 50,
                "changes": [{"change": "business_address", "date": "2026-11-01"}]})
            exp = await c.call_tool("unlicensed_contracting_exposure", {"contract_price_usd": 18500, "scope": "bath",
                                                                        "has_license": False, "amount_paid_usd": 12000,
                                                                        "as_of": AS_OF})
            # What agents really send: a numeric license number, 'TBD' insurance, classes as one string.
            loose = await c.call_tool("bulk_check_subcontractors", {"rows": [
                {"license_number": 123456, "required_classification": "Plumbing", "coi_expiry": "TBD"}],
                "as_of": AS_OF})
            obl2 = await c.call_tool("company_license_obligations", {"entity_type": "sole_owner",
                                                                     "classifications": "C-10, C-36", "as_of": AS_OF})
            bad = await c.call_tool("bulk_check_subcontractors", {}, raise_on_error=False)
            return (names, bulk.structured_content, cls.structured_content, obl.structured_content,
                    exp.structured_content, loose.structured_content, obl2.structured_content, bad)

    names, bulk, cls, obl, exp, loose, obl2, bad = asyncio.run(run())
    assert names == {"bulk_check_subcontractors", "classification_for_scope", "company_license_obligations",
                     "unlicensed_contracting_exposure"}
    assert bulk["rows"][0]["risk"] == "high" and bulk["counts_by_risk"]["high"] == 1   # C-39 needs WC
    assert cls["can_perform"] == ["B"]
    # 50% owner-qualifier is exempt from the qualifier bond: 25,000 + 100,000. 3 personnel <= 5 -> $1M aggregate.
    assert obl["total_bonds_usd"] == 125000 and obl["insurance"][0]["amount_usd"] == 1_000_000
    assert obl["deadlines"][0]["due_date"] == "2027-01-30"                                     # 11/1 + 90 days
    assert exp["total_high_usd"] == 38500
    row = loose["rows"][0]
    assert row["classification_match"] is True and row["coi_status"] == "missing" and row["risk"] == "medium"
    assert obl2["renewal"]["c10_surcharge_usd"] == 20                                   # 'C-10' found in the string
    assert bad.is_error and "exactly one of csv_text or rows" in bad.content[0].text
