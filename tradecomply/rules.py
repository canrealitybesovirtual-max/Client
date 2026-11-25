"""Regulatory data. EVERY value is unverified until a person checks it against the cited source and flips
`verified` to True (with the date checked in `checked_on`). Code reads only from here, so a review is one file."""

DISCLAIMER = (
    "Not legal advice. Regulatory rules change; verify against the official source "
    "before relying on this output for bidding, payment, or legal deadlines."
)

# --- California mechanics lien law (Civil Code Div. 4, Part 6, Title 2) ---
CA_LIEN = {
    "verified": False,
    "checked_on": ("2026-11-24 via search excerpts of leginfo.legislature.ca.gov. The site blocks automated "
                   "reading, so a person must open each section and confirm before setting verified=True."),
    "sources": {
        "preliminary_notice": "Cal. Civ. Code §8200-8216 (20-day window: §8204)",
        "direct_contractor_lien": "Cal. Civ. Code §8412",
        "other_claimant_lien": "Cal. Civ. Code §8414",
        "notice_of_completion": "Cal. Civ. Code §8182 (record on or within 15 days after completion); §8190 (owner "
                                "must send claimants a copy within 10 days)",
        "completion": "Cal. Civ. Code §8180",
        "foreclosure": "Cal. Civ. Code §8460",
    },
    "prelim_notice_days_after_first_furnishing": 20,
    "lien_days_after_completion": 90,                       # both claimant types, no valid NOC
    "lien_days_after_noc": {"direct": 60, "other": 30},     # §8412 vs §8414; same for a notice of cessation
    "noc_valid_within_days_of_completion": 15,
    "foreclosure_days_after_recording": 90,
    # §8200: who must serve a preliminary notice to preserve lien rights.
    "prelim_notice_roles": {"subcontractor", "supplier", "equipment_lessor", "design_professional"},
    "lender_only_prelim_roles": {"direct_contractor"},   # owes notice to the construction lender, if any
    "no_prelim_roles": {"laborer"},
}

# --- Workplace safety. In California, Cal/OSHA (8 CCR) covers most private employers; federal 29 CFR is reference. ---
SAFETY_JURISDICTION_NOTE = (
    "California runs a state plan: Cal/OSHA (Title 8, California Code of Regulations) enforces safety for most "
    "private and state/local public employers. Its rules are at least as strict as federal OSHA and often stricter. "
    "Federal 29 CFR 1926 is shown for reference only."
)

FEDERAL_PENALTY_MAX_USD = {
    "verified": True,
    "checked_on": "2026-11-25, read osha.gov/penalties directly",
    "source": "29 CFR 1903.15(d). The January 2026 adjustment was cancelled (missing CPI data), so 2025 levels carry "
              "forward. Re-check each January.",
    "serious": 16550.0,
    "willful_or_repeat": 165514.0,
}
CAL_OSHA_PENALTY_MAX_USD = {
    "verified": True,
    "checked_on": "2026-11-25, read current text of 8 CCR §336 at dir.ca.gov directly",
    "source": "8 CCR §336; Labor Code §6427-6429; DIR news release 2025-10",
    "general_or_regulatory": 16285.0,
    "serious": 25000.0,
    "willful_or_repeat": 162851.0,
    "willful_minimum": 11632.0,
}
CAL_OSHA_PENALTY_NOTE = ("Cal/OSHA maximums from the current text of 8 CCR §336 (adjusted annually). Base penalties "
                         "are usually lower and adjusted for size, good faith and history.")

SAFETY_TABLE: dict[str, dict] = {
    "fall protection": {
        "aliases": ["fall", "roof edge", "leading edge", "harness", "guardrail"],
        "cal_osha": "8 CCR §1670 (personal fall protection), §1620-1621 (railings)",
        "federal": "29 CFR 1926.501",
        "summary": "Protect workers exposed to falls from edges, openings and elevated work surfaces.",
        "key_requirements": [
            "Cal/OSHA construction trigger is generally more than 7.5 ft (federal: 6 ft)",
            "Use guardrails, safety nets, or a personal fall arrest system",
            "Cover or guard floor openings and holes",
        ],
        "ppe": ["full-body harness", "shock-absorbing lanyard", "anchorage rated for fall arrest"],
        "link": "https://www.dir.ca.gov/title8/1670.html",
        "verified": True,    # §1670 "in excess of 7 1/2 feet"; §1621 railings at 7 1/2 ft or more
    },
    "scaffold": {
        "aliases": ["scaffolding"],
        "cal_osha": "8 CCR §1637 (general requirements) and following sections of Article 21",
        "federal": "29 CFR 1926.451",
        "summary": "Design, erection, use and inspection of scaffolds.",
        "key_requirements": [
            "Firm, level footing; no unstable objects as supports",
            "Guardrails on open sides and ends at the height Cal/OSHA requires",
            "Inspection by a qualified person before use (frequency: check the section; unverified)",
        ],
        "ppe": ["hard hat", "fall protection where guardrails are not provided"],
        "link": "https://www.dir.ca.gov/title8/1637.html",
        "verified": False,   # §1637 confirmed as General Requirements; requirement bullets not yet checked
    },
    "silica": {
        "aliases": ["concrete cutting", "dust", "crystalline silica", "grinding"],
        "cal_osha": "8 CCR §1532.3 (construction); §5204 is general industry",
        "federal": "29 CFR 1926.1153",
        "summary": "Limit exposure to respirable crystalline silica from cutting, drilling and grinding.",
        "key_requirements": [
            "Use the specified engineering controls for the task (wet methods, vacuum dust collection)",
            "Written exposure control plan and a competent person",
            "Respirators where controls do not keep exposure under the limit",
        ],
        "ppe": ["respirator as specified for the task", "eye protection"],
        "link": "https://www.dir.ca.gov/title8/1532_3.html",
        "verified": True,    # §1532.3(a) scope = construction; (c) Table 1 controls
    },
    "excavation": {
        "aliases": ["trench", "trenching", "digging", "cave-in"],
        "cal_osha": "8 CCR §1539-1547 (protective systems: §1541.1); permit: §341, Labor Code §6500",
        "federal": "29 CFR 1926.651-652",
        "summary": "Excavation hazards and cave-in protection.",
        "key_requirements": [
            "Call 811 (DigAlert) to locate underground utilities before digging",
            "Protective system (sloping, shoring, shielding) at 5 ft deep or more, and under 5 ft if a competent "
            "person finds cave-in potential (§1541.1)",
            "Daily inspection by a competent person; Cal/OSHA permit before work in trenches 5 ft or deeper that "
            "a person must descend into (§341)",
        ],
        "ppe": ["hard hat", "high-visibility vest", "safety-toe boots"],
        "link": "https://www.dir.ca.gov/title8/1541_1.html",
        "verified": True,    # §1541.1(a) 5 ft / competent person; §341 permit, "descend" at 5 ft
    },
    "ladder": {
        "aliases": ["ladders", "stepladder", "extension ladder"],
        "cal_osha": "8 CCR §1675 (construction ladders), §1629(c)(3), §3276 (portable ladders)",
        "federal": "29 CFR 1926.1053",
        "summary": "Safe selection, setup and use of ladders.",
        "key_requirements": [
            "Side rails extend at least 3 ft above the landing, or the top is secured and a grab rail provided "
            "(§1629(c)(3), §3276(e)(11))",
            "Maintain three points of contact while climbing",
            "Inspect before use; tag and remove damaged ladders",
        ],
        "ppe": [],
        "link": "https://www.dir.ca.gov/title8/3276.html",
        "verified": True,    # §1629(c)(3) 3 ft; §3276(e)(11) 36 in or secured top
    },
}

# --- California contractor licensing (CSLB) ---
CSLB = {
    "verified": False,
    "checked_on": "2026-11-24 via search excerpts of cslb.ca.gov",
    "lookup_url": "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx",
    "contractor_bond_usd": 25000.0,          # B&P Code §7071.6 (verify current amount)
    "workers_comp_rule": ("B&P Code §7125: workers' comp certificate on file, or a filed exemption only if no "
                          "employees. Some classes must carry coverage even without employees."),
    "wc_required_regardless": {"C-8", "C-20", "C-22", "C-39", "D-49"},
    "minor_work_limit_usd": 1000.0,      # B&P §7048 as amended by AB 2622, effective 2025-01-01
    "data_portal_url": "https://www.cslb.ca.gov/onlineservices/dataportal/",
}

# --- Permit fees. Only jurisdictions with a published, checked schedule produce a number. ---
SAN_DIEGO_FEES = {
    "verified": True,
    "checked_on": "2026-11-25, read IB-501 directly (version in effect after 2026-08-06)",
    "source": "https://www.sandiego.gov/development-services/forms-publications/information-bulletins/501",
    # Table 501A: (base sq ft, plan check base, plan check $/sq ft over base, inspection base, inspection $/sq ft over base)
    "table_501a": {
        "new_single_family":            (3000, 8085.26, 4.10, 8401.90, 4.21),   # Res-SDU/DUP
        "residential_addition_remodel": (500, 3512.92, 6.98, 2228.29, 4.46),    # Res-SDU/DUP Add/Remodel
        "new_multifamily":              (5000, 4627.13, 0.97, 2130.97, 0.48),   # New MDU
        "multifamily_or_commercial_addition": (500, 1199.36, 2.41, 858.50, 1.68),
        "new_commercial":               (3000, 4566.31, 1.57, 1570.80, 0.48),
        "high_rise":                    (50000, 11567.82, 0.24, 10350.16, 0.24),
        "tenant_improvement":           (2000, 2532.77, 1.33, 1315.10, 0.60),
        "tenant_improvement_rapid":     (1000, 840.20, 0.85, 2143.14, 2.17),
        "townhomes":                    (6000, 14100.60, 2.41, 12054.89, 2.05),
        "warehouse":                    (20000, 11202.53, 0.60, 7062.47, 0.36),
        "parking_garage":               (10000, 6331.87, 0.60, 2435.34, 0.24),
    },
    "general_plan_maintenance": 737.00,      # once per project with plan review
    "mapping": 12.16,
    "other_agency_collection": 17.11,        # charged when DSD collects state/other-agency fees
    "lead_hazard_pre_1978": 58.00,
}

# State fees collected by every California building department (quoted in IB-501).
CA_STATE_PERMIT_FEES = {
    "verified": True,
    "checked_on": "2026-11-25, as quoted in San Diego IB-501",
    "smip_per_1000_low_rise_residential": 0.13,   # Public Resources Code §2705: 1-2 story residential
    "smip_per_1000_other": 0.28,                  # 3+ story residential and all nonresidential
    "building_standards_per_25000": 1.00,         # Health & Safety Code §18931.6, min $1
}

OFFICIAL_FEE_TOOLS = {
    "los angeles": "https://08web15.lacity.org/fee_est_dir (LADBS Permit Fee Calculator)",
}
