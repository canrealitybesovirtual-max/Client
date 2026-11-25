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


def test_permit_fees():
    r = logic.estimate_permit_fees("Los Angeles", "residential", 100000, 1000)
    assert r.total_estimated_usd == round(400 + 800 + 350 + 180 + 550, 2) and r.schedule_used == "los angeles"
    assert logic.estimate_permit_fees("Fresno", "solar", 20000).schedule_used == "default"
    with pytest.raises(ValueError):
        logic.estimate_permit_fees("Fresno", "solar", 0)


# ---- MCP surface
def test_mcp_tools_and_resource():
    async def run():
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
            res = await c.read_resource("tradecomply://disclaimer")
            return names, res
    names, res = asyncio.run(run())
    assert names == {"verify_contractor_license", "estimate_permit_fees", "lookup_safety_standard",
                     "calculate_lien_deadline", "check_subcontractor_compliance"}
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
    text = CSLB_HEADER + "\n" + "\n".join(rows) + "\n2999999,11/2"      # truncated last line is skipped
    db = tmp_path / "c.sqlite"
    assert cslb.build(io.StringIO(text), str(db)) == 1003
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
