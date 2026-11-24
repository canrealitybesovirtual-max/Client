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
