"""Pure compliance logic (no I/O except the license source), so every rule is unit-tested."""
from datetime import date, datetime, timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from . import licenses
from .rules import (CA_LIEN, CAL_OSHA_PENALTY_MAX_USD, CAL_OSHA_PENALTY_NOTE, CSLB, DISCLAIMER, FEDERAL_PENALTY_MAX_USD,
                    CA_STATE_PERMIT_FEES, OFFICIAL_FEE_TOOLS, SAN_DIEGO_FEES, SAFETY_JURISDICTION_NOTE,
                    SAFETY_TABLE)

CA_TZ = ZoneInfo("America/Los_Angeles")
Risk = Literal["expired", "critical", "urgent", "normal"]


def today_ca() -> date:
    """Deadlines are California calendar days, not the server's UTC date."""
    return datetime.now(CA_TZ).date()


def _date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip()[:10])
    except (ValueError, AttributeError):
        raise ValueError(f"{field} must be an ISO date like 2026-03-15, got {value!r}")


def _risk(days: int) -> Risk:
    if days < 0:
        return "expired"
    if days <= 14:
        return "critical"
    if days <= 45:
        return "urgent"
    return "normal"


class Base(BaseModel):
    verified: bool = False
    disclaimer: str = DISCLAIMER


# ---------------------------------------------------------------- licenses
class LicenseResult(Base):
    license_number: str
    found: bool
    business_name: str = "unknown"
    status: Literal["active", "expired", "suspended", "revoked", "inactive", "unknown"] = "unknown"
    classifications: list[str] = Field(default_factory=list)
    expiration_date: Optional[str] = None
    expires_in_days: Optional[int] = None
    workers_comp: Literal["on_file", "exempt", "none", "unknown"] = "unknown"
    workers_comp_detail: Optional[str] = None
    contractor_bond_on_file: bool = False
    bond_amount_usd: Optional[float] = None
    city: Optional[str] = None
    business_type: Optional[str] = None
    cslb_last_update: Optional[str] = None
    disciplinary_actions: list[str] = Field(default_factory=list)
    data_source: str
    official_lookup_url: str = CSLB["lookup_url"]
    notes: list[str] = Field(default_factory=list)


def verify_license(license_number: str, state: str = "CA") -> LicenseResult:
    if state.upper() != "CA":
        raise ValueError("Only California (CA) is supported.")
    rec, source = licenses.find(license_number)
    notes = []
    if source == "demo fixtures":
        notes.append("Demo data, not a live CSLB record. Load CSLB data (python -m tradecomply.cslb --download).")
    if not rec and source == "CSLB master file":
        notes.append("Not in CSLB's current master file, which leaves out cancelled, revoked and long-expired "
                     "licenses. Treat as not licensed until confirmed on the CSLB site.")
    elif not rec and source == "CSLB master file (partial copy)":
        notes.append("Not in our copy of CSLB data, which is incomplete (CSLB's download cuts off partway). This does "
                     "NOT mean the contractor is unlicensed: check the number on the CSLB site.")
    if not rec:
        notes.append("No record found. Check the number on the CSLB site. In California a license is required for "
                     "work of $1,000 or more (labor + materials), any work needing a permit, or any work with employees.")
        return LicenseResult(license_number=license_number, found=False, data_source=source, notes=notes)
    exp_days = None
    if rec.get("expiration_date"):
        exp_days = (_date(rec["expiration_date"], "expiration_date") - today_ca()).days
        if exp_days < 0 and rec["status"] == "active":
            rec["status"] = "expired"
            notes.append("Record says active but the expiration date has passed; treated as expired.")
        elif 0 <= exp_days <= 60:
            notes.append(f"License expires in {exp_days} days.")
    if rec.get("workers_comp") == "exempt":
        notes.append("Workers' comp exemption means the contractor may not have employees on the job.")
        if _wc_mandatory_class(rec.get("classifications", [])):
            notes.append(f"Class {_wc_mandatory_class(rec['classifications'])} must carry workers' comp even without "
                         "employees, so this exemption is a problem.")
    official = source.startswith("CSLB master file")
    if official:
        notes.append("From CSLB's published data snapshot shown in data_source. Status can change daily; confirm "
                     "on the CSLB site before award or payment.")
    return LicenseResult(**rec, found=True, expires_in_days=exp_days, data_source=source, notes=notes,
                         verified=official)  # official CSLB data (daily snapshot); fixtures/CSV stay unverified


def _wc_mandatory_class(classes: list[str]) -> Optional[str]:
    from .cslb import norm_class
    required = {norm_class(c) for c in CSLB["wc_required_regardless"]}
    return next((norm_class(c) for c in classes if norm_class(c) in required), None)


# ---------------------------------------------------------------- permit fees
ProjectType = Literal["new_single_family", "residential_addition_remodel", "new_multifamily",
                      "multifamily_or_commercial_addition", "new_commercial", "high_rise", "tenant_improvement",
                      "tenant_improvement_rapid", "townhomes", "warehouse", "parking_garage"]


class PermitFeeResult(Base):
    jurisdiction: str
    supported: bool
    project_type: str
    line_items: dict[str, float] = Field(default_factory=dict)
    total_estimated_usd: Optional[float] = None
    not_included: list[str] = Field(default_factory=list)
    official_source: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


def _tiered(sqft: float, base_sqft: float, base: float, inc: float) -> float:
    return round(base + max(0.0, sqft - base_sqft) * inc, 2)


def estimate_permit_fees(jurisdiction: str, project_type: str, square_footage: float, valuation_usd: float,
                         stories: int = 1, built_before_1978: bool = False) -> PermitFeeResult:
    if square_footage <= 0 or valuation_usd <= 0:
        raise ValueError("square_footage and valuation_usd must be greater than 0")
    key = " ".join(jurisdiction.strip().lower().replace("city of", "").split())
    if key not in ("san diego",):
        tool = OFFICIAL_FEE_TOOLS.get(key)
        return PermitFeeResult(
            jurisdiction=jurisdiction, supported=False, project_type=project_type, official_source=tool,
            notes=[f"No verified fee schedule loaded for '{jurisdiction}', so no estimate is given."
                   + (f" Use the official calculator: {tool}." if tool else " Ask the local building department.")])
    sd, st = SAN_DIEGO_FEES, CA_STATE_PERMIT_FEES
    if project_type not in sd["table_501a"]:
        raise ValueError(f"project_type must be one of: {', '.join(sd['table_501a'])}")
    base_sqft, pc_base, pc_inc, in_base, in_inc = sd["table_501a"][project_type]
    items = {
        "plan_check": _tiered(square_footage, base_sqft, pc_base, pc_inc),
        "inspection": _tiered(square_footage, base_sqft, in_base, in_inc),
        "general_plan_maintenance": sd["general_plan_maintenance"],
        "mapping": sd["mapping"],
    }
    low_res = project_type in ("new_single_family", "residential_addition_remodel", "new_multifamily",
                               "townhomes") and stories <= 2
    smip_rate = st["smip_per_1000_low_rise_residential"] if low_res else st["smip_per_1000_other"]
    items["state_smip_seismic"] = round(valuation_usd / 1000 * smip_rate, 2)
    items["state_building_standards"] = max(1.0, -(-valuation_usd // 25000) * st["building_standards_per_25000"])
    items["other_agency_collection"] = sd["other_agency_collection"]
    if built_before_1978:
        items["lead_hazard_prevention"] = sd["lead_hazard_pre_1978"]
    return PermitFeeResult(
        jurisdiction="San Diego", supported=True, verified=True, project_type=project_type, line_items=items,
        total_estimated_usd=round(sum(items.values()), 2), official_source=sd["source"],
        not_included=["Development impact fees (use the city's DIF calculator)", "School fees",
                      "Water/sewer fees and capacity charges", "Hourly DIF review (2.5 hours at the plan check rate)",
                      "Mechanical/electrical/plumbing permits (IB-103)", "Fire permits (IB-506)",
                      "Construction debris recycling deposit (refundable)"],
        notes=["Plan check and inspection use IB-501 Table 501A: a base rate, plus a per-square-foot rate above the "
               "base area. Area includes exterior walls and, for houses, attached/detached accessory structures.",
               "Fees change several times a year. Confirm against the current IB-501 before quoting."])


# ---------------------------------------------------------------- safety standards
class SafetyResult(Base):
    matched: bool
    topic: str
    cal_osha_section: Optional[str] = None
    federal_reference: Optional[str] = None
    summary: str = ""
    key_requirements: list[str] = Field(default_factory=list)
    ppe: list[str] = Field(default_factory=list)
    link: str = "https://www.dir.ca.gov/samples/search/query.htm"
    jurisdiction_note: str = SAFETY_JURISDICTION_NOTE
    federal_penalty_max_usd: dict[str, float] = Field(default_factory=dict)
    cal_osha_penalty_max_usd: dict[str, float] = Field(default_factory=dict)
    cal_osha_penalty_note: str = CAL_OSHA_PENALTY_NOTE
    available_topics: list[str] = Field(default_factory=list)


def lookup_safety_standard(hazard: str) -> SafetyResult:
    needle = (hazard or "").strip().lower()
    if not needle:
        raise ValueError("hazard must not be empty")
    best = None
    for key, entry in SAFETY_TABLE.items():
        words = [key, *entry["aliases"]]
        if any(w in needle or needle in w for w in words):
            best = key
            break
    if not best:
        return SafetyResult(matched=False, topic=needle, available_topics=sorted(SAFETY_TABLE),
                            summary="No topic in this table matched. Search Title 8 directly.")
    e = SAFETY_TABLE[best]
    return SafetyResult(matched=True, topic=best, verified=bool(e.get("verified")), cal_osha_section=e["cal_osha"], federal_reference=e["federal"],
                        summary=e["summary"], key_requirements=e["key_requirements"], ppe=e["ppe"], link=e["link"],
                        federal_penalty_max_usd={k: FEDERAL_PENALTY_MAX_USD[k] for k in ("serious", "willful_or_repeat")},
                        cal_osha_penalty_max_usd={k: CAL_OSHA_PENALTY_MAX_USD[k]
                                                  for k in ("general_or_regulatory", "serious", "willful_or_repeat",
                                                            "willful_minimum")})


# ---------------------------------------------------------------- lien deadlines
Role = Literal["direct_contractor", "subcontractor", "supplier", "equipment_lessor", "design_professional", "laborer"]


class LienDeadlineResult(Base):
    claimant_role: str
    preliminary_notice_deadline: Optional[str]
    preliminary_notice_required: bool
    lien_recording_deadline: str
    lien_rule_applied: str
    foreclosure_deadline_if_recorded_on_last_day: str
    next_deadline: str
    next_deadline_date: str
    days_remaining: int
    risk_level: Risk
    citations: list[str]
    notes: list[str] = Field(default_factory=list)


def lien_deadlines(claimant_role: Role, last_furnishing_date: str, first_furnishing_date: Optional[str] = None,
                   completion_date: Optional[str] = None, notice_of_completion_date: Optional[str] = None,
                   today: Optional[date] = None, construction_start_date: Optional[str] = None) -> LienDeadlineResult:
    r, today = CA_LIEN, today or today_ca()
    if claimant_role not in r["prelim_notice_roles"] | r["no_prelim_roles"] | r["lender_only_prelim_roles"]:
        raise ValueError(f"Unknown claimant_role {claimant_role!r}")
    last = _date(last_furnishing_date, "last_furnishing_date")
    first = _date(first_furnishing_date, "first_furnishing_date") if first_furnishing_date else None
    completion = _date(completion_date, "completion_date") if completion_date else None
    noc = _date(notice_of_completion_date, "notice_of_completion_date") if notice_of_completion_date else None
    if first and first > last:
        raise ValueError("first_furnishing_date is after last_furnishing_date")
    notes: list[str] = []
    kind = "direct" if claimant_role == "direct_contractor" else "other"
    cites = [r["sources"]["direct_contractor_lien" if kind == "direct" else "other_claimant_lien"],
             r["sources"]["foreclosure"]]

    # Preliminary notice: 20 days from FIRST furnishing.
    prelim_required = claimant_role in r["prelim_notice_roles"]
    prelim = None
    if prelim_required:
        cites.insert(0, r["sources"]["preliminary_notice"])
        start = first
        if claimant_role == "design_professional":
            notes.append("For design professionals the 20 days run from when construction of the project started.")
            start = _date(construction_start_date, "construction_start_date") if construction_start_date else None
            if not start:
                notes.append("Give construction_start_date to compute the design professional's notice deadline.")
        if start:
            prelim = start + timedelta(days=r["prelim_notice_days_after_first_furnishing"])
            if prelim < today:
                notes.append("The 20-day preliminary notice window has passed. A late notice generally still covers "
                             "work furnished within the 20 days before it is served and after; serve it now.")
        elif claimant_role != "design_professional":
            notes.append("Give first_furnishing_date to compute the preliminary notice deadline (20 days after you "
                         "FIRST furnished labor, services, equipment or materials).")
    elif claimant_role in r["lender_only_prelim_roles"]:
        notes.append("Direct contractors don't serve a preliminary notice on the owner, but must serve one on the "
                     "construction lender if there is one (§8200).")

    # Lien recording deadline: earliest applicable.
    base = completion or last
    if not completion:
        notes.append("Completion date of the whole project not given; used your last furnishing date, which can only "
                     "make the deadline earlier (safer). Under §8180, completion also happens on occupancy or use with "
                     "labor stopped, 60 days of continuous cessation, or 30 days of cessation plus a recorded notice "
                     "of cessation.")
    candidates = [(base + timedelta(days=r["lien_days_after_completion"]),
                   f"90 days after {'completion' if completion else 'last furnishing (completion unknown)'}")]
    if noc:
        cites.append(r["sources"]["notice_of_completion"])
        early = completion is not None and noc < completion
        noc_ok = completion is None or (not early and noc <= completion + timedelta(days=r["noc_valid_within_days_of_completion"]))
        if noc_ok:
            notes.append("The shorter notice-of-completion deadline only applies to you if the owner sent you a copy "
                         "within 10 days of recording (§8190). It is used here to be safe.")
            days = r["lien_days_after_noc"][kind]
            candidates.append((noc + timedelta(days=days), f"{days} days after the notice of completion was recorded"))
            if completion is None:
                notes.append("A notice of completion is only effective if recorded within 15 days after completion. "
                             "Shorter NOC deadline used to be safe.")
        elif early:
            notes.append("The notice of completion was recorded before completion, so it is likely ineffective; the "
                         "90-day rule was used. Confirm with counsel.")
        else:
            notes.append("The notice of completion was recorded more than 15 days after completion, so it is likely "
                         "ineffective; the 90-day rule was used. Confirm with counsel.")
    lien_date, rule = min(candidates, key=lambda c: c[0])
    if kind == "other" and last > lien_date:
        notes.append("Your last furnishing is after the computed deadline. Check the dates.")
    foreclosure = lien_date + timedelta(days=r["foreclosure_days_after_recording"])
    if lien_date.weekday() >= 5:
        notes.append("The lien deadline falls on a weekend. CCP §12a may extend it to the next business day, but "
                     "record on the prior business day to be safe. Court holidays are not checked.")

    upcoming = [(d, n) for d, n in [(prelim, "preliminary notice"), (lien_date, "lien recording")] if d and d >= today]
    nxt_date, nxt_name = min(upcoming) if upcoming else (lien_date, "lien recording")
    days = (nxt_date - today).days
    return LienDeadlineResult(
        claimant_role=claimant_role, preliminary_notice_deadline=prelim.isoformat() if prelim else None,
        preliminary_notice_required=prelim_required, lien_recording_deadline=lien_date.isoformat(),
        lien_rule_applied=rule, foreclosure_deadline_if_recorded_on_last_day=foreclosure.isoformat(),
        next_deadline=nxt_name, next_deadline_date=nxt_date.isoformat(), days_remaining=days,
        risk_level=_risk(days), citations=cites, notes=notes)


# ---------------------------------------------------------------- subcontractor prequalification
class SubComplianceResult(Base):
    license: LicenseResult
    classification_match: Optional[bool]
    liability_insurance_current: bool
    workers_comp_ok: bool
    contractor_bond_ok: bool
    safety_history_checked: bool = False
    overall_risk: Literal["low", "medium", "high", "do_not_use"]
    problems: list[str] = Field(default_factory=list)
    recommended_action: str
    notes: list[str] = Field(default_factory=list)


def check_subcontractor(license_number: str, required_classification: Optional[str] = None,
                        liability_insurance_expiry: Optional[str] = None,
                        will_have_employees_on_site: bool = True) -> SubComplianceResult:
    lic = verify_license(license_number)
    problems, blocking = [], False
    partial_miss = not lic.found and lic.data_source == "CSLB master file (partial copy)"
    if partial_miss:
        problems.append("License not in our partial copy of CSLB data; confirm it on the CSLB site")
    elif not lic.found:
        problems.append("No license record found")
        blocking = True
    elif lic.status != "active":
        problems.append(f"License is {lic.status}")
        blocking = True
    cls_ok = None
    if required_classification:
        from .cslb import norm_class
        code = norm_class(required_classification)
        cls_ok = any(norm_class(c) == code for c in lic.classifications)
        if lic.found and not cls_ok:
            problems.append(f"License does not include {code}")
            blocking = True
    ins_ok = False
    if liability_insurance_expiry:
        ins_ok = _date(liability_insurance_expiry, "liability_insurance_expiry") > today_ca()
        if not ins_ok:
            problems.append("Liability insurance expired")
    else:
        problems.append("No liability insurance certificate provided")
    mandatory = _wc_mandatory_class(lic.classifications)
    wc_ok = lic.workers_comp == "on_file" or (lic.workers_comp == "exempt" and not will_have_employees_on_site
                                              and not mandatory)
    if not wc_ok:
        if lic.workers_comp == "exempt" and mandatory:
            problems.append(f"Class {mandatory} must carry workers' comp even without employees")
        elif lic.workers_comp == "exempt":
            problems.append("Workers' comp exemption on file, but they will have employees on site")
        else:
            problems.append("No workers' comp certificate on file")
    bond_ok = lic.contractor_bond_on_file
    if not bond_ok:
        problems.append(f"Contractor bond (${CSLB['contractor_bond_usd']:,.0f}) not on file")

    if blocking:
        risk, action = "do_not_use", "Do not use. The license is missing, inactive, or wrong for this work."
    elif partial_miss:
        risk, action = "high", "Hold. Confirm the license on the CSLB site before award."
    elif not wc_ok:
        risk, action = "high", "Hold. Get a workers' comp certificate before they start."
    elif len(problems) >= 2:
        risk, action = "high", "Hold. Collect the missing documents before award."
    elif problems:
        risk, action = "medium", "Request the missing document before award."
    else:
        risk, action = "low", "Cleared for the bid list, subject to verifying the source records."
    notes = ["Safety history was not checked. Search the OSHA establishment database for the company name."]
    if lic.disciplinary_actions:
        notes.append("CSLB flags on record: " + "; ".join(lic.disciplinary_actions) + ". Review before award.")
    return SubComplianceResult(license=lic, classification_match=cls_ok, liability_insurance_current=ins_ok,
                               workers_comp_ok=wc_ok, contractor_bond_ok=bond_ok, overall_risk=risk,
                               problems=problems, recommended_action=action, notes=notes)
