"""Legal-accuracy regressions for the professional finder (tradecomply/data/professionals.json,
tradecomply/data/handoffs.json, tradecomply/professionals.py), from the adversarial review of 2026-11-26 that re-read
the official texts (CSLB 2026 and 2022 Law Books on cslb.ca.gov, Title 8 on dir.ca.gov, the eCFR API).

Each test pins one finding: a professional shown as "required by law" where the law only requires one in some cases
(or not at all), a missing exemption, the wrong professional type, or wording that could be read as "you may skip a
required professional"."""
import asyncio
import json
import re

from fastmcp import Client

from tradecomply import professionals as pro

REG = {t["id"]: t for t in pro.registry()["types"]}
HO = {e["id"]: e for e in pro.handoffs()}
PUBLIC_ALTERATION = {"project_name": "S", "city": "Fresno", "county": "Fresno", "owner_type": "public_agency",
                     "is_home_improvement": False, "contract_price_usd": 3000000, "start_date": "2027-03-01",
                     "scope": "alteration", "public_funds": True, "year_built": 1970, "stories": 1,
                     "residential_units": 0}
OLD_HOME = {"project_name": "W", "city": "San Diego", "county": "San Diego", "owner_type": "private_individual",
            "is_home_improvement": True, "residential_units": 1, "contract_price_usd": 150000,
            "start_date": "2027-03-01", "scope": "addition", "stories": 2, "year_built": 1960}
BIG_JOB = {"project_name": "T", "city": "San Diego", "county": "San Diego", "owner_type": "private_company",
           "is_home_improvement": False, "contract_price_usd": 5000000, "start_date": "2027-03-01",
           "valuation_usd": 5000000, "sq_ft": 40000, "stories": 4, "height_ft": 50, "demolition": True,
           "year_built": 1965, "excavation_depth_ft": 22, "disturbed_acres": 2, "scope": "new_building",
           "residential_units": 40}
SWPPP = {"project_type": "nonresidential", "scope": "new_construction", "disturbed_acres": 2.5}
# Phrases that tell a user they may go without a professional. Exemptions are stated as what the law says ("is not
# required to", "exempt"), never as advice to skip.
SKIP = re.compile(r"\b(you )?(don'?t|do not) need (a|an|any|to hire)\b|\bno (attorney|lawyer|engineer|architect|qsd|"
                  r"qsp|consultant|rater|inspector|surveyor|notary|permit)s? (is |are )?(needed|required)\b|\bskip\b",
                  re.I)


def run(tool: str, args: dict) -> dict:
    import server

    async def go():
        async with Client(server.mcp) as c:
            return (await c.call_tool(tool, args, raise_on_error=True)).structured_content
    return asyncio.run(go())


def who(tool: str, args: dict, **kw):
    return pro.who_must_sign(tool, run(tool, args), args, **kw)


def roles(out) -> dict:
    return {h.role_id: h for h in out.handoffs}


def shown_text():
    """Every sentence the finder shows people (developer notes and the never-say lists excluded)."""
    for e in pro.handoffs():
        for k in ("why", "when_plain"):
            yield e["id"] + "." + k, e[k]
        yield e["id"] + ".legal_basis", e.get("legal_basis_plain") or e["legal_basis"]
    for tid, t in pro.types().items():
        yield tid + ".plain_description", t.get("plain_description", "")
        for key in ("when_required", "not_required_when", "optional_when"):
            for b in t.get(key, []):
                yield f"{tid}.{key}", b["situation"]
        for s in t.get("notes", []) + t.get("bring_or_ask", []) + [t.get("verify_license_how", "")]:
            yield tid + ".text", s


# ---------------------------------------------------------------------------- attorney: required vs advisable
def test_a_lawyer_is_required_by_law_only_for_a_corporation_or_llc_in_court():
    att = REG["attorney"]
    for b in att["when_required"]:
        if re.search(r"lien|lawsuit|court", b["situation"], re.I):
            assert "corporation or" in b["situation"].lower() and "llc" in b["situation"].lower(), b["situation"]
    lien = [b for b in att["optional_when"] if "mechanics lien" in b["situation"]]
    assert len(lien) == 1 and "advisable" in lien[0]["situation"] and "sole proprietor" in lien[0]["situation"]
    assert "small claims" in lien[0]["situation"] and lien[0]["status"] == "confirmed"
    small = next(b for b in att["not_required_when"] if b["situation"].startswith("Small claims"))
    assert "individual" in small["situation"] and "116.220" in small["situation"]      # entities get the lower limit


def test_the_lien_handoff_says_who_must_use_a_lawyer():
    out = who("calculate_lien_deadline", {"claimant_role": "subcontractor", "last_furnishing_date": "2026-10-01",
                                          "first_furnishing_date": "2026-06-01"})
    att = roles(out)["attorney"]
    assert att.requirement == "conditional"
    assert "cannot be filed in small claims" in att.why and "sole proprietor or partner" in att.why
    assert "advisable" in att.why


def test_practice_of_law_basis_names_the_legal_document_assistant_limit():
    upl = next(b for b in REG["attorney"]["when_required"] if "6125" in b["legal_basis"])
    assert upl["status"] == "unconfirmed" and "6400" in upl["legal_basis"]
    assert "legal document assistant" in upl["situation"] and "may not give legal advice" in upl["situation"]


# ---------------------------------------------------------------------------- notarization: required vs optional
def test_notarization_is_stated_only_where_the_text_requires_it():
    nota = REG["notary_public"]
    accord = next(b for b in nota["when_required"] if "accord" in b["situation"])
    assert "If you settle by an accord" in accord["situation"]           # only on that route (B&P 7071.11(h), 7071.17)
    lien = next(b for b in nota["not_required_when"] if "mechanics lien" in b["situation"])
    assert "without an acknowledgment" in lien["situation"]
    assert "2015.5" in lien["situation"] and "not read" in lien["situation"]   # the proof of service affidavit


# ---------------------------------------------------------------------------- architect vs engineer
def test_stamped_plans_need_an_architect_or_an_engineer_not_both():
    for hid in ("ps-stamp-architect", "ps-stamp-professional_engineer_civil"):
        assert HO[hid]["requirement"] == "required" and "one, not both" in HO[hid]["why"]
    big = roles(who("project_setup_checklist", BIG_JOB))
    assert big["architect"].requirement == "required" and big["professional_engineer_civil"].requirement == "required"
    for b in REG["architect"]["when_required"] + REG["professional_engineer"]["when_required"]:
        if b["status"] == "confirmed" and "5537" in b["legal_basis"]:
            assert "architect" in b["situation"] and "engineer" in b["situation"], b["situation"]


def test_nonstructural_interior_work_is_not_labelled_required_by_law():
    out = roles(who("project_setup_checklist", PUBLIC_ALTERATION))
    for role in ("architect", "professional_engineer_civil"):
        h = out[role]
        assert h.requirement == "conditional" and h.handoff_id == f"ps-stamp-interior-{role}"
        assert "nonstructural" in h.why and "not yet confirmed" in h.legal_basis
    b5538 = [b for b in REG["architect"]["not_required_when"] if "5538" in b["legal_basis"]]
    assert len(b5538) == 1 and b5538[0]["status"] == "unconfirmed" and "ask it before you rely" in b5538[0]["situation"]


def test_conventional_woodframe_never_reads_as_no_engineer_needed():
    e = HO["ps-woodframe-engineer"]
    assert "must be prepared and stamped" in e["why"] and not SKIP.search(e["why"])
    assert "5537" in e["legal_basis_plain"]


def test_accessibility_does_not_claim_the_law_requires_an_architect():
    for hid in ("ps-accessibility-architect", "at-architect"):
        assert HO[hid]["requirement"] == "recommended", hid
        assert "no rule TradeComply has read requires an architect" in HO[hid]["why"]
    at = roles(who("accessibility_triggers", {"project_type": "alteration", "building_use": "retail"}))["architect"]
    assert at.requirement_label == "Suggested, not required by law"


def test_engineer_bases_keep_their_qualifiers():
    pe = REG["professional_engineer"]["when_required"]
    dmg = next(b for b in pe if b["legal_basis"] == "8 CCR 1541.1(d)(3)")
    assert "damaged" in dmg["situation"]
    mep = next(b for b in pe if "6737.3" in b["legal_basis"])
    assert "for work it will install and supervise" in mep["situation"]
    sis = next(b for b in pe if b["legal_basis"].startswith("California Building Code"))
    assert "architect or engineer" in sis["situation"] and sis["status"] == "unconfirmed"


# ---------------------------------------------------------------------------- asbestos
def test_asbestos_survey_follows_neshap_scope_and_the_paid_consultant_rule():
    e = HO["ps-asbestos-survey"]
    assert e["requirement"] == "conditional"
    assert "4 or fewer units" in e["why"] and "pays someone" in e["why"]
    assert "neshap-facility-excludes-4-or-fewer-units" in e["rule_ids"] and "6501.9" in e["legal_basis_plain"]
    home = roles(who("project_setup_checklist", OLD_HOME))["certified_asbestos_consultant"]
    assert home.requirement == "conditional"


def test_asbestos_consultant_certification_has_no_size_exemption():
    asb = REG["asbestos_consultant"]
    reg = next(b for b in asb["when_required"] if b["legal_basis"].startswith("8 CCR 341.15"))
    assert "sets no size limit" in reg["situation"] and "do not treat a smaller job as exempt" in reg["situation"]
    assert reg["source_url"] == "https://www.dir.ca.gov/title8/341_15.html"
    assert not any("100 square feet" in b["situation"] for b in asb["not_required_when"])
    note = next(b for b in asb["not_required_when"] if b["legal_basis"].startswith("8 CCR 341.15(a), Note"))
    assert "does not replace the asbestos survey" in note["situation"]


# ---------------------------------------------------------------------------- lead
def test_dust_clearance_is_tied_to_the_contract_or_other_law():
    e = HO["cr-dust-clearance"]
    assert e["requirement"] == "conditional" and "contract or another law" in e["why"]
    assert "745.85(c)" in e["legal_basis_plain"]


def test_child_occupied_facility_is_conditional_and_housing_is_required():
    pub = roles(who("project_setup_checklist", PUBLIC_ALTERATION))["epa_rrp_certified_firm"]
    assert pub.requirement == "conditional" and "child-occupied facility" in pub.why
    home = roles(who("project_setup_checklist", OLD_HOME))["epa_rrp_certified_firm"]
    assert home.requirement == "required"


def test_a_tool_status_left_open_is_never_shown_as_required_by_law():
    open_facts = {"tasks": ["panel upgrade", "scrape and repaint trim", "crane pick"]}
    got = {h.role_id: h for h in who("certifications_required", open_facts).handoffs}
    result = run("certifications_required", open_facts)
    tool_status = {r["trade"]: r["status"] for r in result["requirements"]}
    assert tool_status["electrical"] == "conditional" and tool_status["lead_rrp"] == "conditional"
    for role in ("dir_certified_electrician", "epa_rrp_certified_firm", "cdph_lead_professional"):
        assert got[role].requirement == "conditional", role
        assert got[role].requirement_label.startswith("Required by law if this applies"), role
    known = {**open_facts, "built_before_1978": True, "building_type": "single_family",
             "employer_license_classes": ["C-10"], "lead_exposure_ug_m3": 60}
    got = {h.role_id: h for h in who("certifications_required", known).handoffs}
    assert all(got[r].requirement == "required" for r in ("dir_certified_electrician", "epa_rrp_certified_firm",
                                                         "cdph_lead_professional", "certified_crane_operator"))


def test_conditional_if_fails_safe():
    entry = {**HO["cr-electrician"]}
    assert pro.effective_requirement(entry, {"requirements": "not a list"}) == "conditional"
    assert pro.effective_requirement({**entry, "id": "no-such-entry"}, {"requirements": []}) == "conditional"
    assert pro.effective_requirement({**entry, "requirement": "recommended"}, {}) == "recommended"
    assert pro.effective_requirement(HO["clo-surety"], {}) == "required"          # no conditional_if


# ---------------------------------------------------------------------------- stormwater
def test_erosivity_waiver_makes_the_qsd_conditional():
    waiver = roles(who("calgreen_and_stormwater", {**SWPPP, "rainfall_erosivity_r_factor": 3}))["qsd_qsp"]
    assert waiver.requirement == "conditional" and waiver.handoff_id == "stormwater-qsd-qsp-waiver"
    assert "waiver" in waiver.why and "QSD" in waiver.why
    full = roles(who("calgreen_and_stormwater", {**SWPPP, "rainfall_erosivity_r_factor": 8}))["qsd_qsp"]
    assert full.requirement == "required" and full.handoff_id == "stormwater-qsd-qsp"
    w = [b for b in REG["qsd_qsp"]["not_required_when"] if "122.26(b)(15)(i)" in b["legal_basis"]]
    assert len(w) == 1 and w[0]["status"] == "confirmed" and "Without a valid waiver" in w[0]["situation"]


# ---------------------------------------------------------------------------- licensing exemptions
def test_his_and_fire_protection_exemptions_are_stated():
    assert "qualifier listed on the license record" in HO["vhic-his"]["why"]
    assert "7152(d)" in HO["vhic-his"]["legal_basis"]
    assert "owner-builder" in HO["cfs-fire-protection"]["why"] and "7026.12(b)" in HO["cfs-fire-protection"]["legal_basis"]
    assert "Self-Insurance" in HO["clo-wc-insurer"]["why"]


def test_safety_timing_matches_the_text():
    assert "rainstorm" in HO["ps-excavation-competent"]["why"] and "every day" in HO["ps-excavation-competent"]["why"]
    assert "last 3 years" in HO["gsp-silica-plhcp"]["when_plain"]


# ---------------------------------------------------------------------------- never "skip", never "nobody needed"
def test_no_shown_text_tells_the_user_to_skip_a_professional():
    hits = [(where, text) for where, text in shown_text() if SKIP.search(text or "")]
    assert hits == []


def test_an_empty_result_is_never_presented_as_no_professional_needed():
    small = pro.who_must_sign("calgreen_and_stormwater", run("calgreen_and_stormwater", {**SWPPP, "disturbed_acres": 0.2}))
    assert small.handoffs == [] and "does not mean no licensed or certified person is needed" in small.note
    unmapped = pro.who_must_sign("not_mapped", {})
    assert "No licensed-professional handoffs are mapped" in unmapped.note and "does not mean" in unmapped.note
    some = pro.who_must_sign("calgreen_and_stormwater", run("calgreen_and_stormwater", SWPPP))
    assert some.handoffs and some.note == pro.NEUTRAL_NOTE
    skipped = pro.who_must_sign("calgreen_and_stormwater", {"unexpected": 1})
    assert "some could not be checked" in skipped.note

    import server

    async def doc():
        async with Client(server.mcp) as c:
            return {t.name: t.description for t in await c.list_tools()}["who_must_sign"]
    assert "empty list means no licensed person is needed" in asyncio.run(doc())


def test_required_labels_follow_the_legal_basis_status():
    """A 'required' entry is labelled verified only when every rule it cites is confirmed and nothing in its basis
    is unconfirmed; the review's new entries carry unconfirmed parts and must say so."""
    for hid in ("ps-stamp-interior-architect", "ps-stamp-interior-professional_engineer_civil",
                "stormwater-qsd-qsp-waiver"):
        e = HO[hid]
        assert pro.entry_verified(e) is False, hid
        assert pro.requirement_label(e["requirement"], False).endswith("(legal basis not yet verified)")
    assert json.loads(json.dumps(pro.handoff_data()))["fields"]["conditional_if"]


def test_product_plan_matches_the_handoff_map():
    import os
    doc = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "product.md"),
               encoding="utf-8").read()
    assert f"handoff map ({len(pro.handoffs())} entries" in doc
    table = doc.split("### 4.2")[1].split("### 4.3")[0]
    row16 = next(line for line in table.splitlines() if line.startswith("| 16 |"))
    assert "Moved to 4.3" in row16                       # accessibility is not a legal architect requirement
    row20 = next(line for line in table.splitlines() if line.startswith("| 20 |"))
    assert "4 or fewer units" in row20 and "conditional" in row20
    assert "Accessibility work on a nonresidential or public permit set" in doc.split("### 4.3")[1]
