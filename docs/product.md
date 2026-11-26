# TradeComply CA: product plan

Written 2026-11-26 from the code in this repository (71 MCP tools in `server.py` and `tradecomply/*.py`, the web app
in `app/`, the rulebook in `tradecomply/data/rulebook.json`). This is a product plan, not legal advice. Wherever a
legal fact is stated, it names the rulebook rule id and its verification status, or it says "unconfirmed".

Rulebook status today: 3,056 rules. 1,759 confirmed and 345 corrected (these count as verified), 192 unverifiable
(the official text could not be read, for example leginfo.legislature.ca.gov blocks automated access) and 760
unchecked (outside the verifier's scope). A result is `verified: true` only when every rule it cites is confirmed or
corrected (`core.Result.cite`).

---

## 1. Purpose

TradeComply helps California construction companies do the compliance work that offices otherwise hire people
for: checking licenses and subs, working out lien, payment and payroll deadlines, building Cal/OSHA programs and
permit checklists, flagging public works, accessibility, air district and federal duties, and drafting the
statutory forms. Every answer shows its official source, whether that source has been verified, and a disclaimer.
It does not replace the people California reserves certain work for: attorneys, engineers and architects,
QSDs and QSPs, HERS raters, certified asbestos and lead professionals, the CSLB qualifier, and others. When a result
needs one of them, TradeComply should say who is needed, why (the law, cited), when, and how to find and verify one
near the job through official public directories, without recommending or ranking anyone.

---

## 2. Who it is for

Target customer: a California GC, subcontractor or specialty contractor with roughly 5-50 people (see
`research/market.md` section 3 for the price reasoning; $1,000 a month fits a 15-50 person GC or multi-trade firm
better than a 5-10 person trade shop).

| Persona | Typical day | Jobs they hire TradeComply for | Tools they use most | What stays with them or a licensed person |
|---|---|---|---|---|
| **GC office manager / admin** | Sub paperwork, COIs, pay apps, lien waivers, permits, renewals | "Tell me which subs are a problem before award." "Don't let us miss a lien, prelim or retention deadline." "Keep our own license, bonds and workers' comp current." | `bulk_check_subcontractors`, `check_subcontractor_compliance`, `calculate_lien_deadline`, `select_lien_waiver`, `prompt_payment_check`, `retention_cap_check`, `company_license_obligations`, `compliance_calendar`, dashboard and calendar feed | Signing waivers and contracts; any lawsuit (attorney); buying bonds and insurance (surety, insurer) |
| **Small sub owner** (often also the qualifier) | Runs crews, bids, invoices | "Can my license take this job?" "Is this home improvement contract legal?" "When can I send a stop payment notice?" "Am I owed late-payment interest?" | `classification_for_scope`, `validate_home_improvement_contract`, `generate_home_improvement_contract`, `check_change_order`, `stop_payment_notice_deadline`, `prompt_payment_check`, `unlicensed_contracting_exposure` | Being the qualifier; contract terms the statute does not supply and any arbitration clause (attorney); collection suits |
| **Project manager** | Schedules, permits, inspections, closeout | "What must happen before we break ground, by date?" "Does this need a permit, HERS, a SWPPP, special inspections?" "Can we work at 6 a.m. Saturday in this city?" | `project_setup_checklist`, `project_risk_score`, `permit_required`, `energy_compliance_triggers`, `calgreen_and_stormwater`, `adu_rules`, `local_jobsite_requirements`, `construction_hours`, `construction_debris_plan`, `estimate_permit_fees`, accessibility tools | Stamped plans (architect / engineer), special inspection, HERS verification, SWPPP (QSD) and inspections (QSP), surveying |
| **Safety lead** | Programs, training, permits, incidents | "Which written programs do we need, as fill-in drafts?" "Which Cal/OSHA permits and notices, and by when?" "What do we report after an injury, and by when?" "Is this crew certified for this task?" | `generate_safety_programs`, `check_cal_osha_permits`, `injury_reporting_deadlines`, `jobsite_hazard_checklist`, `wildfire_smoke_requirements`, `overhead_line_clearance`, `confined_space_classification`, `valley_fever_requirements`, `certifications_required`, `check_crew_certifications`, `equipment_operating_permits`, Field screen | The competent person on site; engineered protective systems and falsework (engineer); crane certification (licensed certifier); medical evaluations (physician or other licensed health care professional) |
| **Payroll / HR** | Timesheets, final pay, leave, benefits | "What would this pay period cost us in premiums and penalties?" "When is final pay due?" "Which HR mandates apply at our size?" "Is this public works payroll right?" | `check_payroll`, `final_paycheck_deadline`, `sick_leave_status`, `jobsite_minimum_wage`, `employer_size_mandates`, `leave_request_check`, `aca_employer_status`, `cobra_deadlines`, `public_works_readiness`, `prevailing_wage_penalties`, `davis_bacon_vs_ca_prevailing`, `federal_overtime_exposure` | Running payroll (payroll provider); signing certified payroll; investigations, discipline and settlements (employment attorney recommended) |
| **Estimator** | Bids and pricing | "What does a worker really cost us?" "What will permits cost?" "What federal, prevailing wage, apprentice, bond and Buy America duties ride on this bid?" | `labor_burden_estimate`, `estimate_permit_fees`, `san_diego_right_of_way_fees`, `public_works_readiness`, `apprentice_hours_required`, `federal_project_requirements`, `buy_america_check`, `cmmc_requirements`, `offroad_contracting_requirements` | Bonds (surety), CMMC Level 2 certification (an authorized C3PAO), apprentice dispatch (apprenticeship committees) |

---

## 3. What TradeComply does, end to end

### 3.1 Three front doors, one rules engine

The same tools are served as: the hosted multi-tenant web app and JSON API (`uvicorn app.main:app`, Render), a
local offline mode for one company (`python -m app.local`), and an MCP server for AI assistants (`/mcp` on the
hosted app, or `python server.py` over stdio). The web app lists tools from `/api/tools` at runtime and renders a
form from each tool's JSON schema, so every tool below is usable from the browser, the API and an AI agent.

Around the tools the web app adds: sign-up with a 14-day trial and $1,000/month Stripe billing; 5-step onboarding;
a red/amber/green dashboard with deadlines and a private calendar feed; projects and subcontractors (CSV import);
documents saved from results (keeping "Verified" only while the signed result is unedited); a Field screen for
toolbox talks, inspections and incident reports that works offline (PWA); team roles (owner, office, field); API
keys with per-key tool scope, rate limit and expiry; MCP call audit log with export; OIDC single sign-on; and
integrations (Slack and Teams alerts, signed webhooks, Twilio text alerts, calendar feeds, Procore, Docusign,
Primavera P6 / Microsoft Project schedule import, Splunk) plus a 36-product integrations catalog.

### 3.2 The 71 tools, grouped by job

| Job | Tools | What they do |
|---|---|---|
| **Licensing** (6) | `verify_contractor_license`, `check_subcontractor_compliance`, `bulk_check_subcontractors`, `classification_for_scope`, `company_license_obligations`, `unlicensed_contracting_exposure` | CSLB status, classes, workers' comp and bond from the CSLB master file; sub risk ratings (a license missing from partial data is never called unlicensed); which classes can do a scope; bonds, LLC insurance, renewal and 90-day CSLB notices; B&P 7028 / 7031 / 7048 exposure |
| **Contracts** (4) | `validate_home_improvement_contract`, `generate_home_improvement_contract`, `check_change_order`, `home_improvement_cancellation_deadline` | Check or draft a B&P 7159 home improvement contract with every required heading and notice; leave `[[ATTORNEY: ...]]` placeholders for terms the statute does not supply; change orders; cancellation deadline |
| **Payments and liens** (5) | `calculate_lien_deadline`, `prompt_payment_check`, `retention_cap_check`, `select_lien_waiver`, `stop_payment_notice_deadline` | Preliminary notice, lien and foreclosure dates; prompt payment due dates and late charges; SB 61 / PCC 7201 retention caps; the right statutory waiver and when it is safe to sign; stop payment notice deadline and enforcement window |
| **Payroll and prevailing wage** (7) | `check_payroll`, `final_paycheck_deadline`, `sick_leave_status`, `jobsite_minimum_wage`, `public_works_readiness`, `apprentice_hours_required`, `prevailing_wage_penalties` | Wage Order 16 overtime, meal and rest premiums from a timesheet; final pay and waiting-time penalty; paid sick leave; state and San Diego minimum wage; DIR registration, eCPR, PWC-100, apprenticeship, listing and retention for public works; apprentice hours at 1:5; penalty exposure |
| **Safety** (9) | `lookup_safety_standard`, `generate_safety_programs`, `check_cal_osha_permits`, `injury_reporting_deadlines`, `jobsite_hazard_checklist`, `wildfire_smoke_requirements`, `overhead_line_clearance`, `confined_space_classification`, `valley_fever_requirements` | Title 8 rule lookup; fill-in written programs (IIPP, heat, WVPP, HazCom, respiratory, silica, excavation, cranes, fall protection plan, lead/asbestos); 8 CCR 341 permits and notices with fees and dates; 8-hour DOSH report and Log 300; task-based hazard checklist; smoke, power lines, confined spaces, Valley Fever |
| **Worker certifications** (4) | `certifications_required`, `check_crew_certifications`, `electrician_certification_path`, `equipment_operating_permits` | Which credentials a task needs (DIR electrician, EPA RRP, CDPH lead, crane operator, elevator mechanic, licensed blaster); whether each crew member may start; how an electrician gets certified; DOSH permits for air tanks, LPG, boilers, hoists |
| **Permits, building and the jobsite** (9) | `estimate_permit_fees`, `permit_required`, `energy_compliance_triggers`, `adu_rules`, `project_setup_checklist`, `project_risk_score`, `construction_hours`, `local_jobsite_requirements`, `san_diego_right_of_way_fees` | San Diego IB-501 fee estimate (other cities get the official calculator link); permit and license test; Title 24 energy forms and HERS; ADU rules and clocks; a dated "before you break ground" checklist and a red/amber/green readiness grade; city work hours, right-of-way, haul route and demolition notices |
| **Environmental** (7) | `calgreen_and_stormwater`, `construction_debris_plan`, `dust_control_requirements`, `offroad_fleet_obligations`, `offroad_contracting_requirements`, `truck_compliance`, `portable_engine_requirements` | CALGreen waste diversion and EV-capable; Construction General Permit at 1 acre; C&D debris deposits; SCAQMD / SJVAPCD / BAAQMD dust rules; CARB off-road fleet, Truck and Bus, Clean Truck Check and PERP |
| **HR** (4) | `employer_size_mandates`, `leave_request_check`, `aca_employer_status`, `cobra_deadlines` | Which state and federal mandates apply at your size; whether a leave request is protected and what you must do; ACA large employer test; COBRA calendar |
| **Business operations** (2) | `compliance_calendar`, `labor_burden_estimate` | A dated year of filings, payments, renewals and postings; the full yearly cost of a worker for bids |
| **Accessibility** (5) | `accessibility_triggers`, `path_of_travel_obligation`, `accessible_parking_requirements`, `accessibility_field_check`, `casp_lease_disclosure` | Which of CBC 11B / 11A, ADA and Fair Housing apply; the path-of-travel 20% test; parking counts; tape-measure pass/fail; CASp lease statements |
| **Federal** (5) | `federal_project_requirements`, `davis_bacon_vs_ca_prevailing`, `federal_overtime_exposure`, `buy_america_check`, `cmmc_requirements` | Davis-Bacon, CWHSSA, Miller Act bonds, E-Verify and FAR clauses; dual-coverage wage floor; overtime exposure; BABA / Buy American; CMMC level and assessment |
| **Rulebook** (2) | `search_california_rules`, `get_california_rule` | Search and read the verified rules behind every answer |
| **Integrations** (2) | `integrations_for_task`, `mcp_client_config` | Which catalog products fit a task; ready-to-paste MCP client settings |

---

## 4. The human-required boundary

### 4.1 What "required" means here

- **Required**: the law requires a licensed, certified, registered or designated person for this step.
- **Conditional**: required only if a fact the tool flags is true (for example, only if the asbestos survey finds
  100 sq ft or more; only if the claimant is a corporation or LLC that must go to court).
- **Optional** or **recommended**: the tool names a professional, but the law does not require one. These are
  listed separately in 4.3 so the product never says "the law requires" when it does not.
- A **required** entry is shown as conditional for a result where the tool itself left the deciding fact open (its
  `conditional_if` test in `handoffs.json`), for example `certifications_required` before the building type and
  the employer's classes are known. An empty handoff list is never presented as "no professional needed".

The legal basis column gives the rulebook rule id and its status. "Unconfirmed" means the rule is not in the
rulebook (or the module keeps it in its own `UNVERIFIED` table); a person must open the official text before the
product states it as fact. "Text read 2026-11-26" marks a rule whose official text was re-read in the independent
review of this table (CSLB Law Books on cslb.ca.gov, Title 8 on dir.ca.gov, the eCFR API); it is still not
"verified" in results until its research pack is updated and the rulebook rebuilt.

How the tools signal a professional today is inconsistent. Seven different fields are used:
`licensed_humans` (licensing), `required_professionals` (contracts), `licensed_professional` (a single string in
payments), `licensed_professionals` (building, project setup), `requires_licensed_professional` (project setup
items), `licensed_human` (public works tasks), `named_people` (safety programs and permits). Other tools put it in
free text (`next_step`, `notes`, `requirements[].item`). Section 5 proposes one structured field.

### 4.2 Situations where the law requires a licensed or certified human

| # | Situation | Professional type | Legal basis (rule id, status) | Tool and output that produces it |
|---|---|---|---|---|
| 1 | Doing contracted work in a CSLB classification | `cslb_licensed_contractor` | B&P 7117.6 (`work-only-in-classifications-held`, unchecked); 16 CCR 830, 832 (unchecked) | `classification_for_scope`: always, `licensed_humans[0]` and `trade_classes`. `adu_rules`: `licensed_professionals` entry "Prime contractor: B ..." (`adu-license-classification-b-or-c47`, unchecked) |
| 2 | Work that needs a permit, or a job at or over the minor-work limit | `cslb_licensed_contractor` | B&P 7048 (`minor-work-exemption-requires-no-permit`, confirmed) | `permit_required`: `license_required == true` |
| 3 | Owner-builder hiring trades, or anyone bidding or scheduling trades for the owner | `cslb_licensed_contractor` | B&P 7044(a)(2) (`owner-builder-licensed-trades`, confirmed); B&P 7026.1(a)(2) (`owner-builder-consultant-requires-license`, unchecked) | `permit_required` with `owner_builder` and `owner_builder_work_by == "licensed_contractors"`; `owner_builder_warnings` |
| 4 | Hiring a sub that cannot be shown to be licensed in the right class | `cslb_licensed_contractor` | B&P 7118 (`contracting-with-unlicensed-contractor`, confirmed) | `bulk_check_subcontractors`: `rows[].risk` in `do_not_use` / `high`. `check_subcontractor_compliance`: `overall_risk`. `verify_contractor_license`: `found` and `status != "active"`. `public_works_readiness`: checklist task `licensed_human` "each subcontractor must hold a CSLB license" (`registration-proof-before-bid-or-subcontract`, confirmed) |
| 5 | Fire sprinkler or suppression work | `cslb_licensed_contractor` (C-16) | B&P 7026.12 (`fire-protection-system-installer`, confirmed). Electrical alarm systems are excluded, and an owner-builder of an owner-occupied single-family home may install one with approved plans and a city or county inspection (7026.12(b), text read 2026-11-26 in the CSLB 2026 Law Book) | `classification_for_scope`: `"C-16" in trade_classes` |
| 6 | Hazardous substance removal or underground storage tank work | `cslb_licensed_contractor` (HAZ certification) | B&P 7058.7 (`hazardous-substance-removal-certification`, `underground-storage-tank-haz-certification`, both unchecked) | `classification_for_scope`: a `notes` entry mentioning "HAZ certification" |
| 7 | The license has no qualifying individual | `cslb_qualifier` | B&P 7068.2 (`qualifier-disassociation-notice-90-days`, `qualifier-replacement-90-days`, confirmed) | `company_license_obligations`: `has_qualifier == false` gives `actions[0]` "No qualifier ..."; `licensed_humans[0]` always names the role |
| 8 | License bonds (contractor, qualifier, LLC worker) | `surety_bond_producer` (the law requires an admitted surety insurer or the cash deposit, B&P 7071.4(a), text read 2026-11-26; using a broker is not required) | B&P 7071.6, 7071.9, 7071.6.5, 7071.4 (`contractor-bond-25000`, `qualifier-bond-25000`, `llc-employee-worker-bond-100000`, `bond-forms-surety-or-cash-only`, confirmed; a cash deposit is the alternative) | `company_license_obligations`: `bonds[].required`; `licensed_humans` "Admitted surety ..." |
| 9 | LLC liability insurance; workers' comp with any employee | `insurance_broker` (the policy must come from a licensed insurer) | B&P 7071.19 (`llc-liability-insurance-aggregate`, `llc-insurance-policy-terms`, confirmed); Labor Code 3700 / B&P 7125 (`wc-required-even-one-employee`, confirmed). Who may sell insurance (Ins. Code 1631): unconfirmed | `company_license_obligations`: `insurance[]` LLC item `required`; `workers_comp.requirement == "certificate_required"` |
| 10 | A salesperson sells home improvement work | `home_improvement_salesperson` (CSLB HIS registration) | B&P 7152-7154, 7159(d)(2) (`his-registration-required`, unchecked; `employing-unregistered-his-discipline`, confirmed; `hic-salesperson-name-registration`, corrected). Officers, LLC managers or members, general partners and the qualifier listed on the license record are exempt (7152(d)-(e), text read 2026-11-26 in the CSLB 2026 Law Book) | `validate_home_improvement_contract`: finding "Salesperson name and HIS registration number" with `status == "violation"` |
| 11 | A corporation or LLC suing to foreclose a lien, enforce a stop payment notice or collect a late payment | `attorney` (conditional: only entities must use one, outside small claims) | `corp-llc-need-attorney-in-court` (unchecked; text read 2026-11-26 in the CSLB 2026 Law Book, printed p. 50, which cites no court rule for it); the 90-day foreclosure window as printed in the Civil Code 8416(a)(8) lien notice (text read 2026-11-26 in the CSLB 2022 Law Book appendix); Civil Code 8460 (`lien-foreclosure-90-days`, unverifiable); Civil Code 8550 (`spn-enforcement-timing`, unverifiable); B&P 7108.5 (`subcontractor-late-payment-penalty-2-percent-per-month`, confirmed). A sole proprietor or partner may sue without a lawyer (Law Book p. 50), so for them a lawyer is advisable, not required; a lien cannot be foreclosed in small claims (p. 48); the higher small claims limit (CCP 116.221) is for individuals, and an entity gets the lower CCP 116.220 limit (both as printed in the 2022 Law Book appendix; current amounts not read) | `calculate_lien_deadline`: `foreclosure_deadline_if_recorded_on_last_day`. `stop_payment_notice_deadline`: `applicable` and `enforcement_suit_latest`; `licensed_professional`. `prompt_payment_check`: `total_late_charges_usd > 0`; `licensed_professional` |
| 12 | Plans for a building outside the conventional woodframe exemption | `architect` or `professional_engineer_civil` | B&P 5537(a)-(b): text read 2026-11-26 in the CSLB 2022 Law Book appendix (later amendments not checked). It exempts plans for small wood-frame buildings and, in (b), requires an architect's or engineer's stamped plans for any part that departs from conventional framing; the general rule for other buildings (B&P 5536 and following, 5538, 6737.1) was not read: unconfirmed (module `UNVERIFIED["design-professional-stamp"]` in `project_setup.py`). Either an architect or an engineer satisfies it (one, not both). For an alteration, purely nonstructural interior work is exempt (B&P 5538, unconfirmed), so those results get the conditional `ps-stamp-interior-*` entries | `project_setup_checklist`: item "Plans prepared and stamped by a licensed architect or engineer" (`requires_licensed_professional`) |
| 13 | Retaining wall over the exemption height, or with a surcharge | `professional_engineer_civil` (conditional: the building department sets it) | Permit: CBC 105.2 / CRC R105.2 (`permit-required-exemptions-thresholds`, corrected). Stamped engineering (B&P 6735): unconfirmed | `permit_required`: `licensed_professionals` contains "California-licensed civil or structural engineer" |
| 14 | Structural work needing special inspections | `special_inspector` (owner hires; the registered design professional prepares the statement) | CBC 1704.2.3, 1704.3, 1704.2.4 (`special-inspection-statement-before-permit`, `special-inspection-reports-and-final-report`, unchecked) | `project_setup_checklist`: items "Statement of Special Inspections with the permit set" and "Special inspector's final report" |
| 15 | Public K-12 school or community college construction | `dsa_inspector_ior` (conditional: the tool does not ask whether the job is a school) | Education Code 17280 et seq. (Field Act): unconfirmed (`UNVERIFIED["public-agency-plan-approval"]`) | `project_setup_checklist`: `owner_type == "public_agency"` gives the item "Confirm who approves the plans ..." |
| 16 | (Moved to 4.3 in the 2026-11-26 review: the accessibility code applies whoever prepares the plans, and no rule read requires an architect for accessibility work itself; whether the plans need an architect or engineer is row 12) | | | |
| 17 | HERS field verification (new homes, HVAC changeouts, ducts) | `hers_rater` (independent of the installer) | Title 24 Part 6 10-103(a)(5) (`energy-cf3r-hers-verification`, `energy-hvac-changeout-hers`, unchecked) | `energy_compliance_triggers`: `hers_rater_needed == true`. `project_setup_checklist`: closeout item "Title 24 energy documents before final" |
| 18 | Disturbing 1 acre or more (Construction General Permit) | `qsd_qsp` | 40 CFR 122.26 (`stormwater-small-construction-1-acre-definition`, confirmed); CGP Order 2022-0057-DWQ (`cgp-swppp-by-qsd`, `cgp-qsp-implementation-inspections`, `cgp-noi-smarts-before-construction`, `cgp-notice-of-termination`, unchecked). A 1 to under 5 acre site may get a rainfall erosivity waiver (40 CFR 122.26(b)(15)(i)(A), read through the eCFR API 2026-11-26; `stormwater-erosivity-waiver`, confirmed): when the tool finds one possible, the QSD/QSP item is conditional (`stormwater-qsd-qsp-waiver`) | `calgreen_and_stormwater`: `construction_general_permit_required == true`. `project_setup_checklist`: items "Stormwater: Construction General Permit ..." and "Stormwater Notice of Termination" |
| 19 | Disturbing a survey monument (San Diego) | `land_surveyor` (conditional) | 7-day notice and permittee pays (`local-sd-survey-monument-7-day-notice`, confirmed). The licensed land surveyor duty in B&P 8771, which the city names: unconfirmed | `local_jobsite_requirements`: `triggers` contains "survey monument disturbed" |
| 20 | Asbestos inspection before demolition or renovation | `certified_asbestos_consultant` (conditional: required when the owner pays someone to survey or sample, B&P 7180.5) | 40 CFR 61.145(a) (`neshap-asbestos-inspection-before-demo`, confirmed), which covers a NESHAP "facility": residential buildings with 4 or fewer units are excluded (40 CFR 61.141, read through the eCFR API 2026-11-26; `neshap-facility-excludes-4-or-fewer-units`, confirmed); Labor Code 6501.9 good-faith check before asbestos-related work (text read 2026-11-26 in the CSLB 2022 Law Book appendix). B&P 7180 sets no size threshold for consultant certification, while 8 CCR 1529(q)(1) defines the consultant's work at 100 sq ft or more and 8 CCR 341.15 requires the certification (both read on dir.ca.gov 2026-11-26): the finder never treats a smaller job as exempt; B&P 7180, 7180.5 (`asbestos-consultant-dosh-certification-required`, `owner-must-hire-certified-asbestos-consultant`, unchecked; text read 2026-11-26 in the CSLB 2026 Law Book, PDF pp. 432-433); air district notice (`air-district-demolition-notice-10-working-days`, corrected) | `project_setup_checklist`: item "Asbestos survey of every material you will disturb". `permit_required` (demolition): `licensed_professionals` "Asbestos consultant ...". `classification_for_scope`: C-22 scopes |
| 21 | Asbestos work of 100 sq ft or more | `asbestos_abatement_contractor` (C-22 or certified, registered with Cal/OSHA) | B&P 7058.5 (`asbestos-100-sqft-c22-or-certification`, confirmed); 8 CCR 341.6, 341.9 (`asbestos-dosh-registration-100sqft`, `asbestos-job-notification`, confirmed) | `check_cal_osha_permits`: `items[activity == "Asbestos registration (Cal/OSHA)"].required`. `classification_for_scope`: `"C-22" in trade_classes`. `permit_required`, `project_setup_checklist`, `generate_safety_programs` (`lead_asbestos`) |
| 22 | Paid renovation of pre-1978 housing or a child-occupied facility | `epa_rrp_certified_firm` (firm plus a certified renovator) | 40 CFR 745.82, 745.83 (`rrp-applicability-pre-1978`, `rrp-minor-repair-6-20-sqft`, confirmed); 745.81, 745.89 (`rrp-firm-certification`, unchecked; text read 2026-11-26 through the eCFR API. 745.81(a) applies where a state has no EPA-authorized renovation program; whether California has one was not checked); 745.90 (`cert-rrp-renovator-onsite-duties`, confirmed). The child-occupied facility item (any pre-1978 building) is shown as conditional (`ps-rrp` `conditional_if`) | `permit_required`: `lead_rrp_applies == true`. `certifications_required`: `requirements[trade == "lead_rrp"]`. `project_setup_checklist`: RRP items |
| 23 | Dust clearance sampling, when a contract or law requires it | `cdph_lead_professional` (conditional; certified inspector, risk assessor or dust sampling technician) | 40 CFR 745.85(c) (`cert-rrp-dust-clearance-action-levels-2026`, confirmed; text re-read through the eCFR API 2026-11-26): dust clearance testing replaces cleaning verification only when the contract or another law requires it, with the sample taken by one of these certified people. EPA does not otherwise require it. That California certifies them through CDPH: unconfirmed | `certifications_required`: `requirements[credential == "Dust clearance sampling by a certified professional"]` |
| 24 | Lead exposure at 50 ug/m3 or more | `cdph_lead_professional` (each exposed worker and supervisor CDPH-certified; CDPH-accredited training) | 8 CCR 1532.1(l) (`cert-lead-cdph-certification-50-ugm3`, corrected; `cert-lead-supervisor-must-be-certified`, `cert-lead-training-by-cdph-accredited-provider`, confirmed) | `certifications_required`: `requirements[trade == "lead_cdph"]`. `check_crew_certifications`: workers with a `lead` trade not cleared |
| 25 | South Coast large operation; San Joaquin Valley dust plan | `air_district_dust_control_supervisor` | SCAQMD Rule 403(e)(1)(E) (`air-scaqmd403-dust-control-supervisor`, confirmed); SJVAPCD Rule 8021 (`air-sjv8021-plan-onsite-training`, confirmed) | `dust_control_requirements`: requirements item "Large operation: dust control supervisor" or "Plan on site and training" |
| 26 | Excavation deeper than 20 ft | `professional_engineer_civil` | 8 CCR 1541.1 Appendix B note 3 ("Sloping or benching for excavations greater than 20 feet deep shall be designed by a registered professional engineer"): text read 2026-11-26 on dir.ca.gov, not yet in the rulebook (`UNVERIFIED` in `project_setup.py` and `safety_programs.py`); protective system at 5 ft (`excavation-protective-system-5-ft`, confirmed) | `project_setup_checklist`: item "Excavation protection for N ft" with "registered professional engineer". `generate_safety_programs`: `programs[key == "excavation"].named_people` |
| 27 | Falsework or shoring over 14 ft high, 16 ft span, or with traffic through it | `professional_engineer_civil` (California civil engineer) | 8 CCR 1717(b)-(c) (`falsework-engineer-design`, confirmed) | `check_cal_osha_permits`: `items["Scaffold / falsework ..."].named_people`. `project_setup_checklist`: item "Falsework/shoring design by a California civil engineer" |
| 28 | Operating a crane | `certified_crane_operator` | 8 CCR 5006.2 (`cert-crane-certification-exam-components`, corrected; `cert-crane-operator-employer-evaluation`, confirmed; `crane-operator-certification-5-years`, unchecked; 5006.2(d)(1) text read 2026-11-26 on dir.ca.gov) | `certifications_required`: `requirements[trade == "crane"]`. `check_crew_certifications`. `generate_safety_programs`: `cranes` |
| 29 | Cranes over 3 tons (annual certification); tower crane erection, climbing, dismantling | `dosh_licensed_crane_certifier` | 8 CCR 5021(a) (`crane-annual-certification`, confirmed); 8 CCR 341.1 (`permit-tower-crane`, unchecked) | `generate_safety_programs`: `cranes`. `check_cal_osha_permits`: `items["Tower crane ..."].required` |
| 30 | Respirators required (respiratory program, silica medical surveillance, wildfire smoke over AQI 500) | `physician_or_plhcp` | 8 CCR 5141.1(f)(3)(B) (`osha2-smoke-mandatory-respirators-over-500`, confirmed); 8 CCR 1532.3(h) (`silica-medical-surveillance-30-days`, confirmed); 8 CCR 5144 program (`respirator-program-annual-fit-test`, confirmed; the medical evaluation element is unconfirmed, `UNVERIFIED["respirator_program_elements"]`) | `wildfire_smoke_requirements`: `respirators == "mandatory"`. `generate_safety_programs`: `programs[key in ("respiratory", "silica")].named_people` |
| 31 | Yearly service of jobsite fire extinguishers | `state_fire_marshal_licensee` | 8 CCR 1922(a)(4) (`osha2-fire-extinguisher-monthly-inspection-annual-service`, confirmed) | `jobsite_hazard_checklist`: `groups[key == "fire_protection"]` |
| 32 | Work inside the clearance from an energized overhead line | `utility_line_operator` (only the line owner's qualified electrical workers are exempt) | 8 CCR 2948, 2949 (`osha2-elec-notify-line-operator`, `osha2-elec-overhead-qualified-worker-exemption`, confirmed) | `overhead_line_clearance`: `status` in `too_close`, `prohibited_over_lines`, `voltage_unknown`, `distance_not_given` |
| 33 | Electrical work for a C-10 contractor | `dir_certified_electrician` | Labor Code 108.2 (`cert-elec-c10-workers-must-be-certified`, `cert-elec-five-certification-types`, confirmed) | `certifications_required`: `requirements[trade == "electrical"]`. `check_crew_certifications`: `workers[].status` |
| 34 | Elevator or other conveyance work | `certified_conveyance_mechanic` (CCCM working for a CQCC) | Labor Code 7311.2, 7311.4; 8 CCR 344.30(h) (`cert-elev-cccm-requirements-and-fee`, `cert-elev-cqcc-company-certification`, confirmed; `cert-elev-only-cccm-of-cqcc-may-work`, unverifiable) | `certifications_required`: `requirements[trade == "elevator"].status != "not_required"` |
| 35 | Blasting | `licensed_blaster` (physically on site) | 8 CCR 5238, 344.20 (`cert-blaster-licensed-blaster-on-site`, `cert-blaster-license-qualifications-and-classes`, confirmed) | `certifications_required`: `requirements[trade == "blasting"]`; `check_crew_certifications` crew finding |
| 36 | Traffic control under a San Diego right-of-way permit | `traffic_control_supervisor_certified` (crew trained by an ATSSA-certified or equivalent supervisor) | City permit rule (`local-sd-tcp-trained-traffic-control-workers`, confirmed) | `local_jobsite_requirements`: requirements item "Trained traffic control crew" |
| 37 | Public works apprenticeship | `apprenticeship_committee` | Labor Code 1777.5; 8 CCR 230, 230.1 (`apprenticeship-threshold-30000`, `das140-contract-award-deadline`, `das142-dispatch-request-72-hours`, `apprentice-ratio-1-to-5`, `apprentice-hours-verified-statement-60-days`, confirmed) | `public_works_readiness`: `apprenticeship.applies`. `apprentice_hours_required`: `shortfall_hours > 0`. `project_setup_checklist`: DAS 140 / 142 items |
| 38 | Federal apprentice rates on Davis-Bacon work | `apprenticeship_committee` (a registered program) | 29 CFR 5.5(a)(4) (`fed-dba-apprentice-registered-only`, confirmed) | `federal_project_requirements`: `requirements[item == "Apprentices"]` |
| 39 | Federal performance and payment bonds | `surety_bond_producer` (surety on Treasury Circular 570) | Miller Act; FAR 52.228-15(d), 28.202 (`fed-miller-bonds-over-150k`, `fed-surety-treasury-570`, confirmed) | `federal_project_requirements`: `bonds` amounts and `bonds.deadline` |
| 40 | DoD contract needing CMMC Level 2 certification | `c3pao` | 32 CFR 170.17 (`cmmc-level2-c3pao-certification-3-years`, confirmed) | `cmmc_requirements`: `required_cmmc_level` or `plan_for_cmmc_level == "Level 2 (C3PAO)"` |
| 41 | Designated safety roles: competent person (excavation, silica, asbestos, confined spaces), qualified person (fall protection plan, scaffold erection, demolition) | `competent_person` (an employee you designate; no state license) | 8 CCR 1504 (`competent-person-definition`, `qualified-person-definition`, unchecked; the competent person definition text read 2026-11-26 on dir.ca.gov); 1541(k), 1532.3(g)(4), 1529(o), 1671.1 (unchecked); 1952 (`osha2-cs-competent-person-classifies-permit-spaces`, confirmed); 1734 (`osha2-demo-qualified-supervision`, confirmed) | `project_setup_checklist` items; `generate_safety_programs`: `named_people`; `confined_space_classification`: `confined_space == true`; `jobsite_hazard_checklist`: demolition group |

Not triggered by any current tool: notary public (no tool output needs a notarized signature. A mechanics lien is
a verified claim, `lien-claim-contents`, unchecked; Civil Code 8416(b), text read 2026-11-26 in the CSLB 2022 Law Book
appendix, says a proper verified claim is recorded "without acknowledgment". The claim also carries a proof of service affidavit, 8416(a)(7); whether that may be signed as a declaration under penalty of perjury instead of before a notary is Code of Civil Procedure 2015.5, not read, so the finder says to ask the county recorder. CSLB does take a notarized copy of an
accord that settles a bond-claim debt or an unsatisfied judgment, B&P 7071.11(h) and 7071.17, text read in the 2026
Law Book, but no tool covers accords yet), geotechnical engineer or engineering geologist
(no tool produces a soils report requirement), structural-engineer-only work (hospitals, schools), and CMMC Level 3
(assessed by the government, not hired). The finder can support these later without design changes.

### 4.3 Professionals the tools name where the law does not require one

| Situation | Type | Tool and output | Why the tool names them |
|---|---|---|---|
| Terms the home improvement statute does not supply; the arbitration clause; `[[ATTORNEY: ...]]` placeholders | `attorney` | `validate_home_improvement_contract` and `generate_home_improvement_contract`: `required_professionals`, `placeholders` | TradeComply's own boundary: custom legal drafting is the practice of law (B&P 6125, unconfirmed). A defective arbitration clause binds only the contractor (`noncompliant-arbitration-clause-unenforceable`, confirmed) |
| Contracting without a license | `attorney` | `unlicensed_contracting_exposure`: `licensed_humans` | B&P 7031 disgorgement and criminal exposure (`unlicensed-no-right-to-sue-disgorgement`, confirmed) |
| Public works coverage unclear | `attorney` (or DIR's own coverage determination) | `public_works_readiness`: `coverage.public_work == null` | `pw-coverage-determination` (unchecked) |
| Civil Wage and Penalty Assessment review | `attorney` | `prevailing_wage_penalties`: `notes` | The contractor may file the review request itself within 60 days (`cwpa-request-review-60-days`, confirmed) |
| Serious injury or death | `attorney` | `injury_reporting_deadlines`: `notes` | Cal/OSHA investigation is likely |
| Alternative workweeks, exemptions, back-wage settlements, disputed final pay | `attorney` | `check_payroll`, `final_paycheck_deadline`: `notes` | Judgment calls outside the arithmetic |
| Altered waiver, or asked to sign before payment | `attorney` | `select_lien_waiver`: `safe_to_sign_now == false` | No license is needed to sign a statutory waiver |
| Excess retention | `attorney` | `retention_cap_check`: `compliant == false` | Fees are recoverable in an action to enforce the cap (`private-retention-5pct-cap`, corrected) |
| CASp lease wording | `attorney` | `casp_lease_disclosure`: `next_step` | The full Civil Code 1938 text was not read |
| Accessibility work on a nonresidential or public permit set | `architect` | `project_setup_checklist`: item "Accessibility (CBC 11B) and path-of-travel upgrades". `accessibility_triggers`: `next_step` "Have the architect ..." | Whoever prepares the plans shows it; the accessibility code applies either way and no rule read requires an architect for it (CBC 11B-202.4 Exception 8: unconfirmed). Whether the plans need an architect or engineer at all is 4.2 row 12 |
| A CASp inspection | `casp` | `casp_lease_disclosure`; `project_setup_checklist` accessibility item | Voluntary; gives the owner legal protections (`ada-casp-definition-and-services`, confirmed) |
| Private plan check when the city is slow | `professional_engineer_civil` / `architect` | `project_setup_checklist`: item "Option: private plan check ..." | Optional under AB 253 (`private-plan-check-ab253`, corrected) |
| Leaving demolition walls unbraced | `professional_engineer_civil` | `jobsite_hazard_checklist`: demolition group | Optional alternative to bracing (`osha2-demo-freestanding-wall-12ft`, confirmed) |
| Tax filings, insurance limits | `cpa`, `insurance_broker` | `compliance_calendar`: `items[].who_files`, notes | Convenience; no rule requires a CPA or broker |
| Translating a contract sold in another language | translator (not a licensed role) | `generate_home_improvement_contract`: `[[TRANSLATE: ...]]` placeholder | The contract must be in the sales language; a translator is not a licensed role, so it is not in the finder |

The machine-readable version for the backend engineer is the handoff map (103 entries over 39 tools and 32
professional types: tool, condition with the exact output field and value, a test expression, professional type,
requirement level, legal basis, rule ids and their status, and when). Commit it as
`tradecomply/data/professional_handoffs.json` when the feature is built, and add a test that every rule id in it
exists and every test expression still evaluates against a sample result of its tool.

A directory registry, `tradecomply/data/professionals.json`, was being written at the same time and uses some
different type ids (for example `professional_engineer`, `licensed_contractor`, `lead_professional`). Each
handoff entry carries `registry_type_id` with the matching id. Four types map to registry entries that already hold
their search pages: `asbestos_abatement_contractor` and `home_improvement_salesperson` to `licensed_contractor`
(CSLB Check a License, plus the DOSH asbestos registrants search), `epa_rrp_certified_firm` to `lead_professional`
(the EPA firm search) and `surety_bond_producer` to `insurance_agent_broker`. Eight types the tools need have no
entry there yet: `physician_or_plhcp`, `state_fire_marshal_licensee`, `utility_line_operator`,
`certified_conveyance_mechanic`, `licensed_blaster`, `air_district_dust_control_supervisor`,
`traffic_control_supervisor_certified` and `c3pao`. Settle on one set of ids before building.

The handoff map was re-checked on 2026-11-26 by running every entry's `test` against fresh tool output with a
strict evaluator (a field missing from the result is an error, not a silent false). All 100 fire on at least one
real input with no missing fields. Fixed in that check: `ccc-blasting` (a crew member without a blaster's license is
`ok_with_supervision`, so the trigger is the crew finding "Licensed blaster on site" with status `problem`),
`permit-owner-builder-trades` (tests the `[owner-builder-licensed-trades]` warning in the result instead of the
input), `cfs-asbestos-consultant` (also checks the B&P 7180.5 sentence in `licensed_humans`), and
`payroll-attorney` (its note is on every `check_payroll` result, not triggered by input). For insurance and bond
entries the law names the insurer or surety, not a broker; the map now says so.

A legal-accuracy review on 2026-11-26 re-read the official texts behind every "required" statement (CSLB 2026 and
2022 Law Books, Title 8 on dir.ca.gov, the eCFR API) and tried to refute each one. It moved the lien-foreclosure
attorney situation to "advisable" for sole proprietors and partners, made the accessibility architect entries
suggestions, made the asbestos survey, interior-alteration stamp, erosivity-waiver QSD and child-occupied RRP
items conditional, downgraded entries where the tool's own status is conditional, stated the HIS, fire
protection, small claims, 341.15 Note and NESHAP exemptions, and kept every exemption it could not read marked
unconfirmed. `tests/test_professionals_legal.py` pins each fix.

---

## 5. The professional finder

### 5.1 What it does

For every result that needs a person from section 4, TradeComply shows:

- **Who**: the professional type in plain words ("a California-licensed civil engineer").
- **Why**: one sentence on the law, with the citation and its verification status ("8 CCR 1717 requires an
  engineered design for falsework over 14 ft. Verified against the official source on 2026-11-25.").
- **When**: a date when the tool computed one (`due_date`, `deadline`, `act_by_date`, `demolition_notice_send_by`),
  otherwise the timing in words ("before plan check submission").
- **How to find and check one**: links to the official public search pages for that license, with the county or
  city already filled in only where the official site accepts it in the URL. Each link names the agency.

It never recommends a specific person or firm, never ranks, sorts by quality, shows ads or takes referral fees,
never scrapes or embeds results, and never works around a site's bot protection. It links out and lets the user
search. (If TradeComply ever took a fee for sending users to lawyers it could be acting as a lawyer referral
service, which in California must be certified by the State Bar under B&P 6155: unconfirmed, not in the rulebook.
That is one more reason to link only to the State Bar's own lists.)

### 5.2 Proposed data shape

Add one structured field to `core.Result`, filled by each module where it already knows the answer (better than a
central mapping that can drift):

```text
needs_professional: [
  { type_id, label, requirement: required|conditional|optional|recommended,
    why, legal_basis, rule_ids, verified, when, due_date }
]
```

The directory links are not stored in results; the app and the MCP server add them from one registry keyed by
`type_id` (5.3), so a changed URL is fixed in one place. Keep the old fields for one release, then retire them.
Tell AI agents in the MCP server instructions to relay `needs_professional` and never to suggest a specific person.

### 5.3 Directory registry

What was checked on 2026-11-26 from this session. "Fetched" means the page was downloaded and read. Many official
hosts were refused by this session's network policy, so they were not fetched and nothing was done to get around
that; their candidate URLs come from web search results only and stay unconfirmed until a person opens them.

| Type id | Who | Official directory | Status (2026-11-26) | Location filter in the URL |
|---|---|---|---|---|
| `cslb_licensed_contractor`, `cslb_qualifier`, `home_improvement_salesperson`, `asbestos_abatement_contractor` (CSLB part) | CSLB licensee, qualifier, HIS | CSLB Check A License: https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx | Fetched: search by license number, business name, personnel name, and HIS name or number. The page loads Google reCAPTCHA, so it is linked, never automated. Whether a license record lists the qualifier was not confirmed | No. CSLB's "Find A Licensed Contractor" (`ZipCodeSearch.aspx`, linked from that page) redirected straight back to Check A License when requested directly, so a zip or class deep link is unconfirmed |
| `apprenticeship_committee` | Apprenticeship program / committee | DIR DAS public works apprenticeship search: https://www.dir.ca.gov/databases/das/pwaddrstart.asp ; program search: https://www.dir.ca.gov/databases/das/aigstart.asp | Fetched: both are GET forms. The public works form posts `varCounty` (all 58 counties) and `varType` (craft) to `results_pwadd.asp` | Yes, county and craft: `https://www.dir.ca.gov/databases/das/results_pwadd.asp?varCounty=<County>&varType=<Craft>` (`<County>` is DIR's code, e.g. `SAN+DIEGOG`; codes in `professionals.json` `county_codes`). Re-tested 2026-11-26 with craft 22: Los Angeles 6 sponsors, Fresno 4, San Diego 5, different lists, "Data is current as of 11/25/2026". DIR: a listing "does not signify they will dispatch apprentices" |
| `asbestos_abatement_contractor` (Cal/OSHA part) | Cal/OSHA-registered asbestos contractor | DOSH Asbestos Registrants database: https://www.dir.ca.gov/databases/doshacru/acrusearch.html | Fetched: POST form (registration number, CSLB number, employer, DBA, city, area code) | No (POST form); link to the search page |
| `certified_asbestos_consultant` | DOSH-certified asbestos consultant or site surveillance technician | DOSH certification search: https://www.dir.ca.gov/databases/doshcaccsst/caccsst_query_1.html | Fetched: POST form (last name, employer, city); a city search returns type (CAC or CSST), certification number and expiration | No (a GET returns "All fields are blank"); link to the search page |
| `dosh_licensed_crane_certifier` | Cal/OSHA-licensed crane certifier | DOSH crane certifier accreditation database: https://www.dir.ca.gov/databases/crane/cranesearch.html | Fetched: POST form (company, city, county, state, area code; bridge and gantry, tower, mobile). The Crane Unit page says it can be used "to find a crane certifier in a particular geographical area". A county search through the form returned different lists for San Diego and Fresno, but every results page says "This data is current as of: 08/01/2021": tell users to confirm with the Crane Unit | No (POST form; a GET returns "You must enter at least one criteria") |
| `dir_certified_electrician` | DIR-certified electrician or registered trainee | DIR electrician certification status search: https://www.dir.ca.gov/DLSE/ECU/ElectCert/ElectCertSearch.asp | Fetched: POST form. The DIR page also links lists of certified electricians on data.ca.gov (not fetched) | No |
| `certified_conveyance_mechanic` | CCCM / CQCC | DOSH Elevator Unit certification page: https://www.dir.ca.gov/dosh/ElevatorCertification.html | Fetched: explains CCCM and CQCC; no public list of certified mechanics or companies was found | None found; show "ask the elevator company for its CQCC and each mechanic's CCCM card" |
| `licensed_blaster` | DOSH-licensed blaster | DOSH Mining and Tunneling Unit: https://www.dir.ca.gov/dosh/mining-and-tunneling-unit.html | Fetched: licensing exam information; no public licensee list found | None found; verify the license card on site |
| `attorney` | California attorney | State Bar Attorney Search: https://apps.calbar.ca.gov/attorney/LicenseeSearch/QuickSearch ; certified lawyer referral services: https://www.calbar.ca.gov/public/find-legal-professionals/find-lawyer-referral-service | Not fetched (host refused). URLs from search results; they show region pages (Los Angeles, Bay Area, Central Valley, Central Coast, San Diego, Sacramento and north) that list counties. `professionals.json` uses an older address for the referral list (`/Public/Need-Legal-Help/Certified-Lawyer-Referral-Services-Directory`); which one is current is unconfirmed | Attorney search: name or number only (a `FreeText` parameter appears in search results). Referral services: link the region page for the job's county, once a person confirms the county-to-region list |
| `professional_engineer_civil`, `land_surveyor` | Licensed civil engineer (structural title included), land surveyor | BPELSG License Lookup: https://www.bpelsg.ca.gov/consumers/lic_lookup.shtml , which points to DCA License Search: https://search.dca.ca.gov/ | Not fetched (hosts refused). Search summary says DCA's advanced search can combine fields such as license type and county | Unconfirmed |
| `architect` | Licensed architect | DCA License Search (California Architects Board): https://search.dca.ca.gov/ | Not fetched (host refused) | Unconfirmed |
| `cpa`, `physician_or_plhcp` | CPA (Board of Accountancy); physician (Medical Board) and other DCA health licensees | DCA License Search: https://search.dca.ca.gov/ | Not fetched (host refused) | Unconfirmed |
| `insurance_broker`, `surety_bond_producer` | CDI-licensed agent or broker | CDI license status: https://cdicloud.insurance.ca.gov/cal ; Find an Agent or Broker: https://interactive.web.insurance.ca.gov/apex_extprd/f?p=119:1 | Not fetched (host refused). `professionals.json` uses https://www.insurance.ca.gov/license-status; which address is current is unconfirmed. The law requires an admitted surety or licensed insurer, not a broker | Unconfirmed |
| `surety_bond_producer` (federal) | Surety on Treasury Circular 570 | Treasury list of certified companies: https://www.fiscal.treasury.gov/surety-bonds/list-certified-companies.html | Not fetched (host refused) | Not applicable (national list) |
| `cdph_lead_professional` | CDPH lead-certified worker, supervisor, inspector/assessor, sampling technician | CDPH Find a Lead Professional: https://www.cdph.ca.gov/Programs/CCDPHP/DEODC/CLPPB/Pages/find_lead_prof.aspx ; index by area: https://www.cdph.ca.gov/Programs/CCDPHP/DEODC/CLPPB/Pages/LRCcertlist.aspx | Not fetched (host refused). Search summary: lists divided by geographic area | Area pages, unconfirmed |
| `epa_rrp_certified_firm` | EPA-certified renovation firm | EPA firm location search: https://cdxocsppapps.epa.gov/ocspp-oppt-lead/firm-location-search | Not fetched (host refused). `professionals.json` uses https://cdxapps.epa.gov/ocspp-oppt-lead/firm-location-search (also refused); which is current is unconfirmed. Older EPA pages point to a previous cfpub.epa.gov address | Unconfirmed |
| `hers_rater` | Certified HERS rater | California Energy Commission HERS program: https://www.energy.ca.gov/programs-and-topics/programs/home-energy-rating-system-hers-program | Not fetched (host refused). Search summary: CEC-approved providers each keep a rater directory | Unconfirmed; link the CEC page, not a provider |
| `qsd_qsp` | QSD / QSP | CASQA "How do I find a QSD or QSP": https://www.casqa.org/how-do-i/how-do-i-find-qsd-or-qsp ; the State Water Board's SMARTS public menu lists self-certified QSDs | Not fetched (hosts refused). CASQA is a nonprofit association, not a government agency; it runs the training and qualification directory, and whether the Water Board designates it as the official list is unconfirmed. Show the State Water Board's pages first | Unconfirmed |
| `casp` | Certified Access Specialist | DSA CASp list: https://www.apps2.dgs.ca.gov/dsa/casp/casp_certified_list.aspx | Not fetched (host refused). Search summary: "Do Inspections" column marks CASps available for hire; counties served may be listed. The CCDA guide on cslb.ca.gov (fetched 2026-11-26) names www.dgs.ca.gov/casp as the DSA CASp website, which `professionals.json` uses | Unconfirmed |
| `dsa_inspector_ior` | DSA-certified project inspector | DSA Databases and Listings: https://www.dgs.ca.gov/DSA/Resources/Databases-and-Listings | Not fetched (host refused); no inspector list found in search results | Unconfirmed |
| `state_fire_marshal_licensee` | OSFM-licensed extinguisher concern | OSFM Fire Extinguisher Program: https://osfm.fire.ca.gov/what-we-do/fire-engineering-and-investigations/fire-extinguisher-program | Not fetched (host refused); a public license lookup was not confirmed | Unconfirmed |
| `c3pao` | Authorized C3PAO | Cyber AB Marketplace: https://cyberab.org/Catalog | Not fetched (host refused) | Not applicable (national) |
| `special_inspector` | Special inspector approved by the building official | No statewide directory | Not applicable | Link the job's building department (from the project's city), never a list of firms |
| `competent_person`, `traffic_control_supervisor_certified`, `air_district_dust_control_supervisor`, `certified_crane_operator`, `utility_line_operator` | Roles you designate, or credentials checked on the card | No government directory | Not applicable | Show "how to check" text instead of a search link: the operator's certificate and issuer; the utility's line operator (and 811 for underground lines) |

Hosts refused by this session's network policy (report, do not bypass): www.calbar.ca.gov, apps.calbar.ca.gov,
search.dca.ca.gov, www.bpelsg.ca.gov, www.cab.ca.gov, www.insurance.ca.gov,
interactive.web.insurance.ca.gov, www.cdph.ca.gov, www.energy.ca.gov, www.dgs.ca.gov, osfm.fire.ca.gov,
www.fiscal.treasury.gov, cyberab.org, cfpub.epa.gov, www.waterboards.ca.gov, www.casqa.org, www.sos.ca.gov,
www.cba.ca.gov, www.mbc.ca.gov. Also refused when re-checked on 2026-11-26: sos.ca.gov, dca.ca.gov, www.latc.ca.gov,
smarts.waterboards.ca.gov, cdicloud.insurance.ca.gov, cdxapps.epa.gov, cdxocsppapps.epa.gov, www.apps2.dgs.ca.gov,
www.nccco.org, maps.cdtfa.ca.gov, data.ca.gov, www.ladbsservices2.lacity.org, selfhelp.courts.ca.gov and
www.courts.ca.gov. Hosts that answered but block automated readers (reported, not bypassed):
leginfo.legislature.ca.gov (Cloudflare challenge, HTTP 403), www.osha.gov regulation pages (HTTP 403) and the
www.ecfr.gov web pages (redirect to a bot check; the official eCFR API works). Before launch a person opens each
candidate URL, confirms it is the official page and whether it takes a location in the URL, and records the date in
the registry.

Keep the registry honest over time: a monthly link check from an allowed network that requests each URL once and
reports any that fail or redirect (no crawling, no form submission), and a "last checked" date shown next to each
link.

### 5.4 Where it shows up, and the guidance principles

The owner's rule: add ease-of-use guidance only where it makes sense and is not invasive. No pop-ups, no forced
tours, no nagging. Short, dismissible, collapsed by default, plain English for a busy office manager who is not
technical, phone-friendly and accessible.

| Place | What appears | Default |
|---|---|---|
| Tool result card (`app/static/app.js` result renderer) | A section "People you'll need" listing each `needs_professional` entry: label, a pill with plain text ("Required by law", "Only if ...", "Suggested"), one sentence why with the citation, when, and "Find one" and "Check a license" links naming the agency | Collapsed `<details>`, summary line shows the count ("2 people you'll need"). Required entries are listed first, but nothing is ranked within a type |
| Saved document (`result_to_markdown`) and Print / PDF | The same list as plain text with the links written out, so it survives printing | Always included, after the result |
| Project page | "People this project needs": the entries from that project's saved results, with dates | Collapsed |
| Dashboard | Nothing new by default. A professional step with a computed date joins the existing deadline list and turns amber inside the existing 14-day window | Same as other deadlines |
| MCP results | The same `needs_professional` field; server instructions tell agents to show it and not to name people | Always |
| First run and empty states | One line where it helps, e.g. on Tools: "Some results need a licensed person. We'll tell you who and link to the official search page." | Dismissible, shown once |

Rules for every tip:

- One or two sentences, in the words an office manager uses ("Hire a licensed civil engineer to design the
  shoring"), with the legal citation after the plain sentence, not instead of it.
- Collapsed or inline; never a modal, overlay, tour, badge count or repeated reminder. Alerts stay what the
  customer already configured (Slack, Teams, text, calendar).
- "Hide this tip" on each tip, remembered per user on the server so it stays hidden on every device (local mode can
  use the same store). Hiding a tip never hides a legal requirement in a result.
- Phone first: one column, links as full-width buttons at least 44 px tall, no horizontal scroll.
- Accessible: native `<details>`/`<summary>`, real link text ("State Bar attorney search, opens calbar.ca.gov"),
  `rel="noopener noreferrer"`, status in text not only color, visible focus, and checks with a screen reader on
  phone and desktop.
- No claim beyond what the tool knows: "helps you find" and "check the license", never "approved", "recommended",
  "guarantees compliance" or "replaces your attorney". When the legal basis is unconfirmed, the tip says so.

---

## 6. Commercial readiness and roadmap

### 6.1 Gaps

| Area | What exists today (in this repo) | What is missing | Who must do it |
|---|---|---|---|
| **Terms, privacy, disclaimers** | A disclaimer on every result and in the footer; "not legal advice" in the MCP instructions | Terms of service, privacy policy, data processing terms, acceptable use. A written opinion on whether document preparation needs Legal Document Assistant registration (B&P 6400, per `research/market.md` 4.4; unconfirmed) and on the unauthorized-practice line; limitation of liability for a wrong deadline; whether California consumer privacy law applies to this business (unconfirmed) | A California attorney |
| **Insurance** | None | Technology errors and omissions, cyber liability, general liability | A licensed insurance broker |
| **Billing** | Stripe Checkout and billing portal; one $1,000/month price (`app/billing.py`); 14-day trial; `past_due` banner; HTTP 402 after a lapsed trial or cancellation while data stays readable | Plan tiers (research suggests a $499-699 trade tier), annual billing, a written refund and cancellation policy, whether sales tax applies to this service in California (unconfirmed) | Operator; CPA or attorney for tax |
| **Service levels** | `/health`; Postgres option with connect retries; startup refuses a throwaway SQLite when `DATABASE_URL` is set | One instance only (rate limits and tool-list cache are in memory); no uptime monitoring, status page, published uptime target, backup schedule or tested restore; SQLite on a Render plan without a disk loses data on redeploy | Operator; engineering |
| **Data export and deletion** | Delete single projects, subs, documents, field logs, users, keys; MCP audit export (CSV/JSON); calendar feed; print to PDF | Export of all of a company's data in one file; closing an account and deleting its data; a retention schedule for audit and outbound-event rows | Engineering; attorney for the retention policy |
| **Support** | `SUPPORT_EMAIL` / `SUPPORT_URL` shown where customers need help (`/api/config` `support_contact`) | Password reset by email (owners must remove and re-invite users today); invite email; a help page for customers; a ticket inbox and response targets | Operator; engineering |
| **Legal content upkeep** | 3,056 rules with per-rule status; builds are reproducible (`scripts/build_rulebook.py --check`) | 952 rules are not verified (192 unverifiable, 760 unchecked), including the lien deadlines (leginfo cannot be read automatically) and many of the human-boundary rules in section 4. A review calendar for January 1 and July 1 law changes | A person who reads the official text; attorney review for lien and payment rules |
| **Professional handoffs** | Seven inconsistent fields name professionals (4.1) | One `needs_professional` field, the directory registry, the UI in 5.4, and a person confirming each candidate directory URL | Engineering; a person with browser access to the refused hosts |
| **Documentation** | `README.md`, `docs/integrations-dev.md`, `docs/mcp-security.md` | The README opening still says "five tools" and that every result is unverified until `rules.py` is checked; it predates the rulebook and the 71 tools | Engineering |
| **Product accessibility** | Keyboard support, skip link, `<details>` patterns, PWA | An audit against WCAG 2.1 AA on phone and desktop | Engineering or an accessibility reviewer |
| **Signatures** | On-screen drawn signatures stored as PNG | Not certified e-signatures; Docusign connector exists for documents that need one | Engineering |

### 6.2 Roadmap, in priority order

**P0: before the first paying customer**

1. Attorney-drafted terms of service and privacy policy; the attorney's opinion on document preparation, the
   unauthorized-practice line, and the professional finder's wording; E&O and cyber insurance through a broker.
2. Durable data: Postgres with automated backups and one tested restore; refuse to start in production without
   persistent storage.
3. Account data export (one file) and account deletion; a written retention policy.
4. Password reset email and invite email; a monitored support address.
5. `needs_professional` on every tool in the handoff map, with the directory registry limited to the pages fetched
   on 2026-11-26 (CSLB, DIR, and the LADBS and San Diego examples). Every other directory stays "unconfirmed" and is
   not shown until a person opens it.
6. Fix the README opening.

**P1: first ten customers**

7. The finder UI and guidance in 5.4, and the same field in documents, projects and MCP.
8. A person confirms each refused-host directory and the State Bar county-to-region list; the monthly link check.
9. Verify the unchecked and unverifiable rules that drive section 4 first (CGP QSD/QSP, HERS CF3R, special
   inspections, B&P 7180 and 7180.5, the qualifier and HIS rules, corporate court representation), then the lien
   rules with an attorney reading the Civil Code. B&P 7180 and 7180.5 and the corporate court rule were read in the
   CSLB 2026 Law Book on 2026-11-26 (`professionals.json` gives the pages), so their lawbook packs can be updated
   first.
10. Uptime monitoring and a status page; shared rate limits so a second instance can run.
11. Test a lower-priced trade tier.
12. Move the module `UNVERIFIED` tables (design stamp, 20 ft excavation engineer, Field Act, 5144 medical
    evaluation) into research packs so they can be verified like every other rule. The 20 ft engineer rule (8 CCR
    1541.1 Appendix B, dir.ca.gov) and B&P 5537 (CSLB 2022 Law Book appendix) were read on 2026-11-26 and can go in
    with those sources.

**P2: after that**

13. Annual plans; a security review toward a recognized attestation if customers ask for it.
14. Ask whether the job is a school or community college so the DSA inspector of record is a clear yes or no.
15. More cities' permit fees and jobsite rules beyond San Diego and Los Angeles.
16. Spanish interface and documents.
17. Add finder types only when a new tool needs them (notary public, geotechnical engineer).
