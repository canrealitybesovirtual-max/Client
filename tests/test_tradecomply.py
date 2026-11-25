import asyncio
from datetime import date, timedelta

import pytest
from fastmcp import Client

from server import mcp
from tradecomply import logic

T = date(2026, 6, 1)


def call(tool, **args):
    async def run():
        async with Client(mcp) as c:
            return await c.call_tool(tool, args)
    return asyncio.run(run())


# ---- lien deadlines
def test_sub_with_noc_gets_30_days_not_60():
    r = logic.lien_deadlines("subcontractor", "2026-05-01", "2026-04-01", completion_date="2026-05-10",
                             notice_of_completion_date="2026-05-15", today=T)
    assert r.lien_recording_deadline == "2026-06-14"           # NOC + 30
    assert r.preliminary_notice_deadline == "2026-04-21"       # FIRST furnishing + 20
    assert r.foreclosure_deadline_if_recorded_on_last_day == "2026-09-12"
    assert r.next_deadline == "lien recording" and r.days_remaining == 13 and r.risk_level == "critical"


def test_direct_contractor_with_noc_gets_60_and_no_prelim():
    r = logic.lien_deadlines("direct_contractor", "2026-05-01", completion_date="2026-05-10",
                             notice_of_completion_date="2026-05-15", today=T)
    assert r.lien_recording_deadline == "2026-07-14" and r.preliminary_notice_required is False


def test_no_noc_uses_90_days_from_completion():
    r = logic.lien_deadlines("supplier", "2026-04-20", "2026-03-01", completion_date="2026-04-30", today=T)
    assert r.lien_recording_deadline == (date(2026, 4, 30) + timedelta(days=90)).isoformat()
    assert r.risk_level == "normal"


def test_late_noc_is_ignored():
    r = logic.lien_deadlines("subcontractor", "2026-04-01", completion_date="2026-04-01",
                             notice_of_completion_date="2026-05-01", today=T)
    assert r.lien_recording_deadline == "2026-06-30" and any("ineffective" in n for n in r.notes)


def test_unknown_completion_is_conservative_and_explained():
    r = logic.lien_deadlines("subcontractor", "2026-05-01", today=T)
    assert r.lien_recording_deadline == "2026-07-30" and any("last furnishing" in n for n in r.notes)


def test_bad_dates_rejected():
    with pytest.raises(ValueError):
        logic.lien_deadlines("subcontractor", "May 1", today=T)
    with pytest.raises(ValueError):
        logic.lien_deadlines("subcontractor", "2026-05-01", first_furnishing_date="2026-06-01", today=T)


# ---- licenses and subs
def test_license_found_and_missing():
    r = logic.verify_license("123-456")
    assert r.found and r.status == "active" and r.verified is False
    assert not logic.verify_license("000001").found


def test_expired_license_do_not_use():
    r = logic.check_subcontractor("999999", liability_insurance_expiry="2099-01-01")
    assert r.overall_risk == "do_not_use"


def test_wrong_classification_blocks():
    r = logic.check_subcontractor("123456", required_classification="C-10", liability_insurance_expiry="2099-01-01")
    assert r.overall_risk == "do_not_use" and r.classification_match is False


def test_exempt_workers_comp_with_employees_is_high():
    r = logic.check_subcontractor("555000", "C-10", "2099-01-01", will_have_employees_on_site=True)
    assert r.overall_risk == "high" and not r.workers_comp_ok
    ok = logic.check_subcontractor("555000", "C-10", "2099-01-01", will_have_employees_on_site=False)
    assert ok.overall_risk == "low" and ok.safety_history_checked is False


def test_license_csv_source(tmp_path, monkeypatch):
    f = tmp_path / "lic.csv"
    f.write_text("license_number,business_name,status,classifications,expiration_date,workers_comp,"
                 "contractor_bond_on_file,disciplinary_actions\n"
                 "1111111,CSV Roofing,active,C-39 Roofing,2099-01-01,on_file,true,\n")
    monkeypatch.setenv("LICENSE_CSV", str(f))
    r = logic.verify_license("1111111")
    assert r.business_name == "CSV Roofing" and r.data_source == "csv:lic.csv"


# ---- safety and fees
def test_safety_is_cal_osha_first():
    r = logic.lookup_safety_standard("working near a roof edge")
    assert r.topic == "fall protection" and "8 CCR" in r.cal_osha_section and "7.5" in r.key_requirements[0]
    assert logic.lookup_safety_standard("trenching 6 ft").topic == "excavation"
    miss = logic.lookup_safety_standard("crane")
    assert not miss.matched and "ladder" in miss.available_topics


def test_permit_fees_san_diego():
    r = logic.estimate_permit_fees("City of San Diego", "new_single_family", 3500, 700000, stories=2)
    i = r.line_items
    assert i["plan_check"] == round(8085.26 + 500 * 4.10, 2) and i["inspection"] == round(8401.90 + 500 * 4.21, 2)
    assert i["state_smip_seismic"] == 91.0 and i["state_building_standards"] == 28.0
    assert r.supported and r.verified and r.total_estimated_usd == round(sum(i.values()), 2)
    small = logic.estimate_permit_fees("san diego", "tenant_improvement", 1500, 80000, built_before_1978=True)
    assert small.line_items["plan_check"] == 2532.77 and small.line_items["state_smip_seismic"] == 22.4
    assert small.line_items["lead_hazard_prevention"] == 58.0


def test_permit_fees_other_cities_no_guess():
    la = logic.estimate_permit_fees("Los Angeles", "new_commercial", 5000, 1_000_000)
    assert not la.supported and la.total_estimated_usd is None and "lacity.org" in la.official_source
    with pytest.raises(ValueError):
        logic.estimate_permit_fees("San Diego", "solar", 100, 1000)


# ---- MCP surface
def test_mcp_tools_and_resource():
    async def run():
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
            res = await c.read_resource("tradecomply://disclaimer")
            return names, res
    names, res = asyncio.run(run())
    assert {"verify_contractor_license", "estimate_permit_fees", "lookup_safety_standard",
            "calculate_lien_deadline", "check_subcontractor_compliance", "search_california_rules",
            "check_payroll", "project_setup_checklist"} <= names
    assert len(names) >= 30
    assert "UNVERIFIED" in res[0].text


def test_mcp_call_returns_structured_result():
    r = call("check_subcontractor_compliance", license_number="123456", required_classification="C-36",
             liability_insurance_expiry="2099-01-01")
    assert r.structured_content["overall_risk"] == "low"
    assert r.structured_content["disclaimer"].startswith("Not legal advice")


# ---- fixes from the source check
def test_noc_before_completion_ignored():
    r = logic.lien_deadlines("subcontractor", "2026-05-01", completion_date="2026-05-10",
                             notice_of_completion_date="2026-05-05", today=T)
    assert r.lien_recording_deadline == "2026-08-08" and any("before completion" in n for n in r.notes)


def test_design_professional_uses_construction_start():
    r = logic.lien_deadlines("design_professional", "2026-05-01", first_furnishing_date="2026-01-01",
                             construction_start_date="2026-03-01", today=T)
    assert r.preliminary_notice_deadline == "2026-03-21"


def test_roofer_must_carry_workers_comp_even_solo():
    r = logic.check_subcontractor("777000", "C-39", "2099-01-01", will_have_employees_on_site=False)
    assert not r.workers_comp_ok and r.overall_risk == "high"
    assert any("C39" in p for p in r.problems)


def test_silica_cites_construction_section():
    r = logic.lookup_safety_standard("silica")
    assert "1532.3" in r.cal_osha_section and r.cal_osha_penalty_max_usd["serious"] == 25000.0


def test_verified_flags_follow_the_data():
    assert logic.lookup_safety_standard("fall").verified is True
    assert logic.lookup_safety_standard("scaffold").verified is False
    assert logic.lien_deadlines("subcontractor", "2026-05-01", today=T).verified is False


# ---- CSLB master file import
CSLB_HEADER = ("LicenseNo,LastUpdate,BusinessName,BUS-NAME-2,FullBusinessName,MailingAddress,City,State,County,ZIPCode,"
               "country,BusinessPhone,BusinessType,IssueDate,ReissueDate,ExpirationDate,InactivationDate,ReactivationDate,"
               "PendingSuspension,PendingClassRemoval,PendingClassReplace,PrimaryStatus,SecondaryStatus,Classifications(s),"
               "AsbestosReg,WorkersCompCoverageType,WCInsuranceCompany,WCPolicyNumber,WCEffectiveDate,WCExpirationDate,"
               "WCCancellationDate,WCSuspendDate,CBSuretyCompany,CBNumber,CBEffectiveDate,CBCancellationDate,CBAmount,"
               "WBSuretyCompany,WBNumber,WBEffectiveDate,WBCancellationDate,WBAmount,DBSuretyCompany,DBNumber,"
               "DBEffectiveDate,DBCancellationDate,DBAmount,DateRequired,DiscpCaseRegion,DBBondReason")


def _row(num, status="CLEAR", secondary="", classes="C39", wc="Exempt", wc_exp="", exp="01/31/2099", cb_cancel=""):
    vals = {k: "" for k in CSLB_HEADER.split(",")}
    vals.update({"LicenseNo": num, "LastUpdate": "11/20/2026", "BusinessName": f"Co {num}", "City": "FRESNO",
                 "BusinessType": "Sole Owner", "ExpirationDate": exp, "PrimaryStatus": status,
                 "SecondaryStatus": secondary, "Classifications(s)": classes, "WorkersCompCoverageType": wc,
                 "WCExpirationDate": wc_exp, "CBAmount": "25000", "CBCancellationDate": cb_cancel})
    return ",".join(f'"{vals[k]}"' for k in CSLB_HEADER.split(","))


def test_cslb_import_and_lookup(tmp_path, monkeypatch):
    import io
    from tradecomply import cslb
    rows = [_row("1000001", classes="B| C10"),
            _row("1000002", status="Work Comp Susp", secondary="WC Susp Pending", wc="License does not have current W/C"),
            _row("1000003", wc="Workers' Compensation Insurance", wc_exp="01/01/2020", cb_cancel="02/02/2025")]
    rows += [_row(str(2000000 + i)) for i in range(1000)]
    text = CSLB_HEADER + "\n" + "\n".join(rows) + "\n"
    db = tmp_path / "c.sqlite"
    assert cslb.build(io.StringIO(text), str(db)) == 1003 and cslb.is_complete(str(db))
    monkeypatch.setenv("LICENSE_DB", str(db))
    a = logic.verify_license("1000001")
    assert a.found and a.verified and a.status == "active" and a.classifications == ["B", "C10"]
    s = logic.check_subcontractor("1000002", liability_insurance_expiry="2099-01-01")
    assert s.license.status == "suspended" and s.overall_risk == "do_not_use"
    assert any("WC Susp Pending" in f for f in s.license.disciplinary_actions)
    c = logic.verify_license("1000003")
    assert c.workers_comp == "none" and c.contractor_bond_on_file is False      # expired WC, cancelled bond
    assert logic.check_subcontractor("1000001", required_classification="C-10",
                                     liability_insurance_expiry="2099-01-01").classification_match is True
    miss = logic.verify_license("123456")                       # demo fixture must NOT be used once real data exists
    assert not miss.found and "master file" in miss.notes[0]


def test_partial_cslb_copy_never_says_unlicensed(tmp_path, monkeypatch):
    import io
    from tradecomply import cslb
    import csv, json, sqlite3
    rows = [_row("3000000")]
    db = tmp_path / "p.sqlite"         # a partial copy, written directly (real ones need 100k+ rows)
    con = sqlite3.connect(db)
    con.execute("create table licenses (license_number text primary key, rec text not null)")
    con.execute("create table meta (k text primary key, v text)")
    rec = cslb.to_record(next(csv.DictReader(io.StringIO(CSLB_HEADER + "\n" + rows[0]))))
    con.execute("insert into licenses values (?,?)", (rec["license_number"], json.dumps(rec)))
    con.executemany("insert into meta values (?,?)", [("rows", "1"), ("complete", "0")])
    con.commit()
    monkeypatch.setenv("LICENSE_DB", str(db))
    miss = logic.verify_license("4000000")
    assert not miss.found and "does NOT mean" in miss.notes[0]
    r = logic.check_subcontractor("4000000", liability_insurance_expiry="2099-01-01")
    assert r.overall_risk == "high" and "confirm" in r.recommended_action.lower()


def test_truncated_file_flagged_incomplete(tmp_path):
    import io
    from tradecomply import cslb
    rows = [_row(str(5000000 + i)) for i in range(1200)]
    db = tmp_path / "t.sqlite"
    cslb.build(io.StringIO(CSLB_HEADER + "\n" + "\n".join(rows) + "\n5999999,1"), str(db))
    assert not cslb.is_complete(str(db))                       # cut-off file is kept but marked partial
    with pytest.raises(RuntimeError):                          # a smaller cut-off download never replaces it
        cslb.build(io.StringIO(CSLB_HEADER + "\n" + "\n".join(rows[:1100]) + "\n5999999,1"), str(db),
                   truncated=True, only_if_more=True)
    cslb.build(io.StringIO(CSLB_HEADER + "\n" + "\n".join(rows) + "\n"), str(db))
    assert cslb.is_complete(str(db))


def test_seed_snapshot_loads(tmp_path, monkeypatch):
    from tradecomply import cslb
    db = tmp_path / "s.sqlite"
    n = cslb.build_from_seed(str(db))
    assert n > 200000 and cslb.meta(str(db))["snapshot_date"] == "2026-11-24" and not cslb.is_complete(str(db))
    monkeypatch.setenv("LICENSE_DB", str(db))
    r = logic.verify_license("1067972")
    assert r.found and r.business_name == "BRAVO GROUP CONSTRUCTION CO" and "snapshot 2026-11-24" in r.data_source


def test_hosted_never_falls_back_to_demo_records(monkeypatch, tmp_path):
    monkeypatch.setenv("CSLB_AUTO_DOWNLOAD", "1")
    monkeypatch.setenv("LICENSE_DB", str(tmp_path / "missing.sqlite"))
    r = logic.verify_license("123456")                  # a demo fixture number
    assert not r.found and r.data_source == "CSLB data loading"
    s = logic.check_subcontractor("123456")
    assert s.overall_risk == "high"                      # hold and confirm, not "do not use"
