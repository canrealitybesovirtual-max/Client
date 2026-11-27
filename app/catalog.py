"""The people-facing catalog of checks and pages: groups, plain titles and one-line summaries for the 74 tools, the
roles' common checks, the six hand-designed long forms, Quick help's starter and follow-up questions, the pages and
Settings sections (with the roles that can open each), and plain field labels.

Applied only in the web layer: GET /api/tools sends these on a deep copy of each tool spec (api.list_tools), the
search index and Quick help read them from here. The MCP tool registration, api.tool_specs() and the tool list Ask
TradeComply sends to Anthropic never change (tests/test_catalog.py checks both are byte-identical).

Sources: spec_assets/tool_catalog.json and form_layouts.json of the 2026-11-27 build spec (generated and validated
against the live tool list); roles from the spec's navigation and Settings tables (1.4, 1.5).
"""
import copy

GROUPS = [
    {"id": "start", "label": "Start a job", "icon": "g-start",
     "purpose": "Plan a new job before you break ground.",
     "tools": ["project_setup_checklist", "project_risk_score"]},
    {"id": "paid", "label": "Get paid", "icon": "g-paid",
     "purpose": "Preliminary notices, liens, payment timing, retention and waivers.",
     "tools": ["calculate_lien_deadline", "stop_payment_notice_deadline", "prompt_payment_check",
               "retention_cap_check", "select_lien_waiver"]},
    {"id": "license", "label": "Licenses and subs", "icon": "g-license",
     "purpose": "CSLB licenses, license classes and your subcontractors.",
     "tools": ["verify_contractor_license", "check_subcontractor_compliance", "bulk_check_subcontractors",
               "classification_for_scope", "company_license_obligations", "unlicensed_contracting_exposure"]},
    {"id": "contract", "label": "Contracts and homeowners", "icon": "g-contract",
     "purpose": "Home improvement contracts, change orders and cancellation.",
     "tools": ["validate_home_improvement_contract", "generate_home_improvement_contract",
               "home_improvement_cancellation_deadline", "check_change_order"]},
    {"id": "safety", "label": "Jobsite safety", "icon": "g-safety",
     "purpose": "Cal/OSHA permits, programs, hazards and injury reporting.",
     "tools": ["check_cal_osha_permits", "generate_safety_programs", "injury_reporting_deadlines",
               "lookup_safety_standard", "jobsite_hazard_checklist", "confined_space_classification",
               "overhead_line_clearance", "wildfire_smoke_requirements", "valley_fever_requirements"]},
    {"id": "certs", "label": "Crew certifications", "icon": "g-certs",
     "purpose": "Trade certifications and equipment permits for the work and the crew.",
     "tools": ["certifications_required", "check_crew_certifications", "electrician_certification_path",
               "equipment_operating_permits"]},
    {"id": "pay", "label": "Pay and HR", "icon": "g-pay",
     "purpose": "Payroll, final pay, leave and the HR rules for your size.",
     "tools": ["check_payroll", "final_paycheck_deadline", "sick_leave_status", "jobsite_minimum_wage",
               "leave_request_check", "cobra_deadlines", "aca_employer_status", "employer_size_mandates",
               "labor_burden_estimate", "compliance_calendar"]},
    {"id": "public", "label": "Public and federal jobs", "icon": "g-public",
     "purpose": "Prevailing wage, apprentices, DIR and federal contract rules.",
     "tools": ["public_works_readiness", "apprentice_hours_required", "prevailing_wage_penalties",
               "federal_project_requirements", "federal_overtime_exposure", "davis_bacon_vs_ca_prevailing",
               "buy_america_check", "cmmc_requirements"]},
    {"id": "permits", "label": "Permits and local rules", "icon": "g-permits",
     "purpose": "Building permits, energy and stormwater, and city rules.",
     "tools": ["permit_required", "estimate_permit_fees", "adu_rules", "energy_compliance_triggers",
               "calgreen_and_stormwater", "construction_hours", "local_jobsite_requirements",
               "construction_debris_plan", "san_diego_right_of_way_fees"]},
    {"id": "access", "label": "Accessibility", "icon": "g-access",
     "purpose": "CBC 11B and ADA: what applies and whether it measures up.",
     "tools": ["accessibility_triggers", "accessibility_field_check", "accessible_parking_requirements",
               "path_of_travel_obligation", "casp_lease_disclosure"]},
    {"id": "equipment", "label": "Equipment and air", "icon": "g-equipment",
     "purpose": "CARB rules for off-road diesel, trucks, portable engines and dust.",
     "tools": ["offroad_fleet_obligations", "offroad_contracting_requirements", "truck_compliance",
               "portable_engine_requirements", "dust_control_requirements"]},
    {"id": "rules", "label": "Rules and professionals", "icon": "rule",
     "purpose": "Search the rulebook, read a rule, or find a licensed professional.",
     "tools": ["search_california_rules", "get_california_rule", "find_licensed_professional"]},
]

HIDDEN_GROUP = {"id": "for_assistants", "label": "For AI assistants"}

# name: (group, title, one-line summary). Group "for_assistants" = hidden from browsing (made for AI assistants).
_TOOLS = {
    "project_setup_checklist": ("start", "Project setup checklist",
        "A dated before-you-break-ground checklist for one job: contract, permits, safety, payroll and more."),
    "project_risk_score": ("start", "Project risk score",
        "Grades each phase of a job red, amber or green and lists what to do next, by date."),
    "calculate_lien_deadline": ("paid", "Calculate lien deadline",
        "Your preliminary notice, lien recording and foreclosure dates, with days left."),
    "stop_payment_notice_deadline": ("paid", "Stop payment notice deadline",
        "The last day to give a stop payment notice on a private or public job."),
    "prompt_payment_check": ("paid", "Prompt payment check",
        "When a progress payment or retention is due for one payer and payee, and whether it is late."),
    "retention_cap_check": ("paid", "Retention cap check",
        "Checks a retention percentage against California's 5% caps for private and public jobs."),
    "select_lien_waiver": ("paid", "Choose a lien waiver",
        "Which of the four statutory lien waiver and release forms fits a payment."),
    "verify_contractor_license": ("license", "Verify contractor license",
        "Looks up a CSLB license: status, classes, expiration, workers' comp and bond."),
    "check_subcontractor_compliance": ("license", "Check subcontractor compliance",
        "License, class, insurance, workers' comp and bond for one sub, with a risk rating."),
    "bulk_check_subcontractors": ("license", "Bulk check subcontractors",
        "The same checks for a whole sub list, from a CSV file or rows."),
    "classification_for_scope": ("license", "License class for the work",
        "Which CSLB license classes can do a scope of work, and on what conditions."),
    "company_license_obligations": ("license", "Company license obligations",
        "Bonds, insurance and filings your own contractor license must keep current."),
    "unlicensed_contracting_exposure": ("license", "Unlicensed contracting exposure",
        "Whether the under-$1,000 minor work exemption applies, and the exposure if it does not."),
    "validate_home_improvement_contract": ("contract", "Validate home improvement contract",
        "Checks a homeowner contract against the required headings, notices and payment rules."),
    "generate_home_improvement_contract": ("contract", "Generate home improvement contract",
        "Drafts a home improvement contract with the required notices in order, for review before anyone signs."),
    "home_improvement_cancellation_deadline": ("contract", "Home improvement cancellation deadline",
        "The last day a homeowner can cancel the contract, counted in business days."),
    "check_change_order": ("contract", "Check change order",
        "Checks a home improvement change order: in writing, scope, price change, signed before the work."),
    "check_cal_osha_permits": ("safety", "Check Cal/OSHA permits",
        "Which Cal/OSHA permits, registrations and notices a job needs: trenches, scaffolds, demolition, asbestos."),
    "generate_safety_programs": ("safety", "Generate safety programs",
        "Which written Cal/OSHA programs your company needs, with drafts: IIPP, heat illness and more."),
    "injury_reporting_deadlines": ("safety", "Injury reporting deadlines",
        "When to report a serious injury or death to Cal/OSHA, and the other forms and logs that follow."),
    "lookup_safety_standard": ("safety", "Look up safety standard",
        "The Cal/OSHA rule for a hazard: fall protection, scaffolds, silica, trenches or ladders."),
    "jobsite_hazard_checklist": ("safety", "Jobsite hazard checklist",
        "Cal/OSHA requirements for the tasks on a job, grouped by hazard."),
    "confined_space_classification": ("safety", "Confined space classification",
        "Classifies one space under Cal/OSHA's construction confined-space rules."),
    "overhead_line_clearance": ("safety", "Overhead line clearance",
        "Minimum distance from an energized overhead power line for a machine, crew, ladder or material."),
    "wildfire_smoke_requirements": ("safety", "Wildfire smoke requirements",
        "What Cal/OSHA's wildfire smoke rule requires for a crew at the current air quality."),
    "valley_fever_requirements": ("safety", "Valley fever requirements",
        "Valley fever training and dust rules for soil work in one county."),
    "certifications_required": ("certs", "Certifications required",
        "Which trade certifications the tasks need: electrical, lead, crane and more."),
    "check_crew_certifications": ("certs", "Check crew certifications",
        "Whether each worker's credentials cover the task they are assigned."),
    "electrician_certification_path": ("certs", "Electrician certification path",
        "Hours and steps for a worker to get DIR electrician certification."),
    "equipment_operating_permits": ("certs", "Equipment operating permits",
        "Which jobsite equipment needs a Cal/OSHA permit to operate: air tanks, hoists, boilers and more."),
    "check_payroll": ("pay", "Check payroll",
        "Overtime, meal and rest breaks and minimum wage for hourly construction workers."),
    "final_paycheck_deadline": ("pay", "Final paycheck deadline",
        "When a worker's final pay is due after a firing, layoff or quit."),
    "sick_leave_status": ("pay", "Sick leave status",
        "A worker's paid sick leave: when it can be used, hours earned and hours left."),
    "jobsite_minimum_wage": ("pay", "Jobsite minimum wage",
        "The state minimum wage, and San Diego's city rate where it applies, for a jobsite and date."),
    "leave_request_check": ("pay", "Leave request check",
        "Whether a time-off request is protected at your company's size, and what you must do."),
    "cobra_deadlines": ("pay", "COBRA deadlines",
        "Notice and election deadlines for health coverage after a qualifying event."),
    "aca_employer_status": ("pay", "ACA employer status",
        "Whether your company counts as a large employer under the federal health coverage mandate."),
    "employer_size_mandates": ("pay", "HR rules at your company size",
        "Which California and federal HR rules apply at your headcount."),
    "labor_burden_estimate": ("pay", "Labor burden estimate",
        "What one worker costs per year beyond the wage, for bids and billing rates."),
    "compliance_calendar": ("pay", "Compliance calendar",
        "A year of dated filings, payments, renewals, postings and trainings for your company."),
    "public_works_readiness": ("public", "Public works readiness",
        "Prevailing wage, DIR registration, certified payroll and other public works duties for a job."),
    "apprentice_hours_required": ("public", "Apprentice hours required",
        "Apprentice hours required on a public works job, for one craft."),
    "prevailing_wage_penalties": ("public", "Prevailing wage penalties",
        "Penalty exposure, low to high, from public works violation facts."),
    "federal_project_requirements": ("public", "Federal project requirements",
        "Davis-Bacon, Buy America and other federal rules for a federally funded job."),
    "federal_overtime_exposure": ("public", "Federal overtime exposure",
        "Unpaid federal overtime and damages from a timesheet on a federal job."),
    "davis_bacon_vs_ca_prevailing": ("public", "Davis-Bacon vs. California prevailing wage",
        "Compares the federal and California wage rates for one classification."),
    "buy_america_check": ("public", "Buy America check",
        "Domestic-content rules for one product or material on a federal or federal-aid job."),
    "cmmc_requirements": ("public", "CMMC requirements (defense contracts)",
        "CMMC and federal cybersecurity rules for one defense contract."),
    "permit_required": ("permits", "Is a permit needed?",
        "Whether a building permit is needed for the work, and who may do it."),
    "estimate_permit_fees": ("permits", "Estimate permit fees",
        "A building permit fee estimate: San Diego from its official tables; other cities link to their calculator."),
    "adu_rules": ("permits", "ADU rules",
        "State ADU and junior ADU size, setback and approval rules for one unit."),
    "energy_compliance_triggers": ("permits", "Title 24 energy triggers",
        "Which Energy Code documents and HERS checks a permit needs."),
    "calgreen_and_stormwater": ("permits", "CALGreen and stormwater",
        "Whether CALGreen measures and state stormwater permit coverage apply."),
    "construction_hours": ("permits", "Construction hours",
        "Whether work is allowed at a date and time in a city, and what it takes if not."),
    "local_jobsite_requirements": ("permits", "Local jobsite requirements",
        "City permits, notices and deadlines for a jobsite: right-of-way, dumpsters and more."),
    "construction_debris_plan": ("permits", "Construction debris plan",
        "The debris recycling plan, deposit and deadlines for a job in a covered city."),
    "san_diego_right_of_way_fees": ("permits", "San Diego right-of-way fees",
        "Traffic control and right-of-way permit fee estimate for a San Diego job."),
    "accessibility_triggers": ("access", "Accessibility triggers",
        "Which accessibility rules apply to a project: CBC 11B, 11A, ADA and fair housing."),
    "accessibility_field_check": ("access", "Accessibility field check",
        "Checks field measurements (doors, ramps, counters, parking) against CBC 11B."),
    "accessible_parking_requirements": ("access", "Accessible parking requirements",
        "How many accessible and van-accessible spaces each lot or garage needs."),
    "path_of_travel_obligation": ("access", "Path-of-travel upgrades",
        "How much path-of-travel accessibility work an alteration must include."),
    "casp_lease_disclosure": ("access", "CASp lease disclosure",
        "The CASp inspection statement a commercial lease must include."),
    "offroad_fleet_obligations": ("equipment", "Off-road diesel fleet rules",
        "CARB off-road diesel fleet reporting and turnover duties for your equipment."),
    "offroad_contracting_requirements": ("equipment", "Off-road diesel: hiring fleets",
        "What a prime contractor must collect from equipment fleets before signing."),
    "truck_compliance": ("equipment", "Truck compliance",
        "CARB Truck and Bus, Clean Truck Check and DMV checks for one heavy truck."),
    "portable_engine_requirements": ("equipment", "Portable engine requirements",
        "Rules and registration for one portable engine: generator, compressor, pump or light tower."),
    "dust_control_requirements": ("equipment", "Dust control requirements",
        "Construction dust rules for one site, by air district."),
    "search_california_rules": ("rules", "Search California rules",
        "Search TradeComply's rulebook of California construction rules, with their sources."),
    "get_california_rule": ("rules", "Read a California rule",
        "One rule in full: plain English, official quote, citation and whether it is verified."),
    "find_licensed_professional": ("rules", "Find licensed professional",
        "What a licensed professional does, when the law requires one, and the official directories."),
    "mcp_client_config": ("for_assistants", "AI assistant setup file",
        "Settings for an AI assistant (MCP client) that uses TradeComply and other apps' servers."),
    "integrations_for_task": ("for_assistants", "Apps for a task",
        "Products in the integrations catalog that fit a task, and how each connects."),
    "list_professional_types": ("for_assistants", "List of professional types",
        "Every kind of licensed professional TradeComply can point to."),
    "who_must_sign": ("for_assistants", "Who must sign (for AI assistants)",
        "The licensed people another check's result needs. Used by AI assistants."),
}

COMMON = {
    "owner": ["calculate_lien_deadline", "check_subcontractor_compliance", "verify_contractor_license",
              "permit_required", "validate_home_improvement_contract", "construction_hours",
              "project_setup_checklist", "injury_reporting_deadlines"],
    "office": ["calculate_lien_deadline", "check_subcontractor_compliance", "verify_contractor_license",
               "permit_required", "validate_home_improvement_contract", "construction_hours",
               "project_setup_checklist", "injury_reporting_deadlines"],
    "field": ["construction_hours", "lookup_safety_standard", "wildfire_smoke_requirements",
              "injury_reporting_deadlines", "jobsite_hazard_checklist", "overhead_line_clearance"],
}

# The 6 hand-designed long forms (validated against the schemas by tests/test_catalog.py). Each show_if entry's
# BECAUSE line quotes the field description it rests on.
LAYOUTS = {
    "validate_home_improvement_contract": {
        "layout": "steps", "final_button": "Check this contract",
        "steps": [
            {"title": "The contract", "fields": ["contract_price_usd", "contract_signed_on",
              "buyer_received_signed_copy_on", "start_date", "completion_date", "is_service_and_repair",
              "residential_units", "license_classifications"]},
            {"title": "Payments", "fields": ["down_payment_usd", "progress_payments", "finance_charge_usd",
              "contractor_has_payment_bond_or_joint_control", "time_and_materials", "service_charge_usd"]},
            {"title": "The buyer and the sale", "fields": ["buyer_age_65_or_older", "buyer_initiated_contact",
              "negotiated_at_contractor_place_of_business", "has_salesperson", "salesperson_his_number",
              "disaster_repair", "only_work_needed_for_the_problem"]},
            {"title": "What the contract includes", "fields": ["includes", "includes_arbitration_clause",
              "arbitration_formatted_per_7191", "subcontractors_will_be_used", "is_swimming_pool",
              "workers_comp_notice_says_no_employees"]},
        ],
        "show_if": {
            "salesperson_his_number": {"field": "has_salesperson", "op": "eq", "value": True},
            "arbitration_formatted_per_7191": {"field": "includes_arbitration_clause", "op": "eq", "value": True},
            "time_and_materials": {"field": "is_service_and_repair", "op": "eq", "value": True},
            "service_charge_usd": {"field": "is_service_and_repair", "op": "eq", "value": True},
            "only_work_needed_for_the_problem": {"field": "is_service_and_repair", "op": "eq", "value": True},
            "buyer_initiated_contact": {"field": "is_service_and_repair", "op": "eq", "value": True},
        },
    },
    "permit_required": {
        "layout": "steps", "final_button": "Check if a permit is needed",
        "steps": [
            {"title": "The work", "fields": ["work_description", "structure_type", "project_type", "valuation_usd",
              "sq_ft", "stories", "year_built"]},
            {"title": "Details for this kind of work", "fields": ["fence_height_ft", "is_pool_barrier",
              "retaining_wall_height_ft", "retaining_wall_supports_surcharge",
              "retaining_wall_impounds_flammable_liquids", "is_one_story_detached_accessory",
              "height_above_grade_in", "deck_attached_or_serves_exit_door"]},
            {"title": "Systems in the work", "fields": ["affects_structure_egress_or_utilities",
              "includes_electrical_plumbing_or_gas", "has_fuel_burning_appliance_or_attached_garage",
              "existing_building_sq_ft"]},
            {"title": "Who does the work", "fields": ["owner_builder", "owner_builder_work_by",
              "owner_intends_to_sell", "owner_lived_in_home_12_months_before_completion",
              "performer_advertises_or_has_employees", "part_of_larger_project"]},
            {"title": "Dates", "fields": ["permit_application_date", "permit_issued_date", "demolition_start_date"]},
        ],
        "show_if": {
            "fence_height_ft": {"field": "structure_type", "op": "eq", "value": "fence"},
            "is_pool_barrier": {"field": "structure_type", "op": "eq", "value": "fence"},
            "retaining_wall_height_ft": {"field": "structure_type", "op": "eq", "value": "retaining_wall"},
            "retaining_wall_supports_surcharge": {"field": "structure_type", "op": "eq", "value": "retaining_wall"},
            "retaining_wall_impounds_flammable_liquids":
                {"field": "structure_type", "op": "eq", "value": "retaining_wall"},
            "is_one_story_detached_accessory":
                {"field": "structure_type", "op": "eq", "value": "detached_accessory_structure"},
            "height_above_grade_in": {"field": "structure_type", "op": "in", "value": ["deck", "sidewalk_or_driveway"]},
            "deck_attached_or_serves_exit_door": {"field": "structure_type", "op": "eq", "value": "deck"},
            "owner_builder_work_by": {"field": "owner_builder", "op": "eq", "value": True},
            "owner_intends_to_sell": {"field": "owner_builder", "op": "eq", "value": True},
            "owner_lived_in_home_12_months_before_completion": {"field": "owner_builder", "op": "eq", "value": True},
        },
    },
    "accessibility_field_check": {
        "layout": "sections", "final_button": "Check these measurements",
        "steps": [
            {"title": "The building and doors", "fields": ["existing_building", "door_clear_width",
              "door_opening_depth", "level_change_height", "level_change_beveled"]},
            {"title": "Ramps and curb ramps", "fields": ["ramp_running_slope", "ramp_rise", "ramp_run",
              "ramp_cross_slope", "ramp_clear_width", "curb_ramp_running_slope", "curb_ramp_flare_slope"]},
            {"title": "Walkways, counters and turning space", "fields": ["walkway_clear_width",
              "walkway_running_slope", "walkway_cross_slope", "counter_height", "counter_length", "checkout_counter",
              "turning_space_diameter"]},
            {"title": "Parking", "fields": ["parking_stall_width", "parking_aisle_width", "parking_stall_length",
              "van_space", "parking_vertical_clearance", "parking_sign_bottom_height", "parking_surface_slope",
              "existing_parking_structure", "sign_in_circulation_path"]},
        ],
        "show_if": {},
    },
    "generate_safety_programs": {
        "layout": "steps", "final_button": "Show my safety programs",
        "steps": [
            {"title": "Your company", "fields": ["company_name", "employee_count", "max_employees_at_one_site",
              "peak_employees_last_year", "worksites_have_public_access", "naics_code", "majority_language",
              "shift_hours"]},
            {"title": "The work", "fields": ["outdoor_work", "heat_exposure", "excavation_max_depth_ft",
              "workers_enter_trench", "building_stories", "building_height_ft", "demolition",
              "scaffold_or_falsework_height_stories", "crane_use"]},
            {"title": "Exposures", "fields": ["silica_tasks", "asbestos_or_lead", "respirators_used",
              "hazardous_chemicals", "max_fall_height_ft", "conventional_fall_protection_infeasible",
              "last_toolbox_meeting"]},
        ],
        "show_if": {
            "workers_enter_trench": {"field": "excavation_max_depth_ft", "op": "truthy"},
        },
    },
    "project_setup_checklist": {
        "layout": "steps", "final_button": "Make my checklist",
        "steps": [
            {"title": "The job", "fields": ["project_name", "city", "county", "owner_type", "is_home_improvement",
              "residential_units", "contract_price_usd", "contractor_role", "contractor_license_number"]},
            {"title": "Schedule and size", "fields": ["start_date", "estimated_completion_date", "valuation_usd",
              "sq_ft", "stories", "height_ft", "scope", "mixed_use", "year_built"]},
            {"title": "Site work and crew", "fields": ["demolition", "excavation_depth_ft", "disturbed_acres",
              "peak_crew_size", "uses_subcontractors", "has_construction_lender", "public_funds", "holidays"]},
        ],
        "show_if": {},
    },
    "project_risk_score": {
        "layout": "steps", "final_button": "Grade this project",
        "steps": [
            {"title": "The job", "fields": ["project_name", "city", "county", "owner_type", "is_home_improvement",
              "residential_units", "contract_price_usd", "contractor_role", "contractor_license_number"]},
            {"title": "Schedule and size", "fields": ["start_date", "estimated_completion_date", "valuation_usd",
              "sq_ft", "stories", "height_ft", "scope", "mixed_use", "year_built"]},
            {"title": "Site work and crew", "fields": ["demolition", "excavation_depth_ft", "disturbed_acres",
              "peak_crew_size", "uses_subcontractors", "has_construction_lender", "public_funds", "holidays"]},
        ],
        "show_if": {},
    },
}
BECAUSE = {
    "validate_home_improvement_contract": {
        "salesperson_his_number": "Salesperson's CSLB HIS registration number",
        "arbitration_formatted_per_7191": ("Clause titled ARBITRATION OF DISPUTES in required type with the statutory "
                                           "NOTICE before the initials"),
        "time_and_materials": "Service and repair priced by time and materials",
        "service_charge_usd": "Service and repair: service/trip/inspection charge",
        "only_work_needed_for_the_problem": "Service and repair: you sell only what the problem needs",
        "buyer_initiated_contact": "Service and repair: the buyer called you to request the work",
    },
    "permit_required": {
        "fence_height_ft": "Fence height in feet",
        "is_pool_barrier": "Fence serves as a swimming pool barrier",
        "retaining_wall_height_ft": "Retaining wall height in feet, bottom of footing to top of wall",
        "retaining_wall_supports_surcharge": "Wall holds back a slope, driveway, building or other surcharge",
        "retaining_wall_impounds_flammable_liquids": "Wall impounds Class I, II or IIIA liquids",
        "is_one_story_detached_accessory": "Shed/playhouse is one story and detached from other buildings",
        "height_above_grade_in": "Deck or walk height above grade in inches (highest point)",
        "deck_attached_or_serves_exit_door": "Deck is attached to the house or serves the required exit door",
        "owner_builder_work_by": "Owner-builder only: who does the work",
        "owner_intends_to_sell": "Owner-builder only: owner plans to sell or offer the property for sale",
        "owner_lived_in_home_12_months_before_completion": (
            "Owner-builder only: the home is the owner's principal residence and they will have lived there 12 "
            "months before the work is completed (B&P 7044(a)(3))"),
    },
    "generate_safety_programs": {
        "workers_enter_trench": "Workers go down into the trench/excavation",
    },
}


TOOLS = {name: {"group": g, "title": t, "summary": s, "hidden": g == HIDDEN_GROUP["id"]}
         for name, (g, t, s) in _TOOLS.items()}
GROUP_LABELS = {g["id"]: g["label"] for g in GROUPS} | {HIDDEN_GROUP["id"]: HIDDEN_GROUP["label"]}

# Plain labels where humanize(name) reads oddly (sent as x-label in /api/tools).
LABELS = {
    "salesperson_his_number": "Salesperson's HIS registration number",
    "firm_rrp_certification_expires": "Firm's RRP certification expires",
    "incident_datetime": "When it happened",
    "height_above_grade_in": "Height above grade (inches)",
    "wc_class_code": "Workers' comp class code",
    "claimant_role": "Your role on this job",
    "construction_start_date": "Construction start (design professionals only)",
}

ALL, OFFICE_UP, OWNER = ("owner", "office", "field"), ("owner", "office"), ("owner",)

# Settings sections (#/settings?section=<key>) and who can open each (spec 1.5).
SETTINGS_SECTIONS = [
    {"key": "account", "label": "Your account", "group": "You", "roles": ALL},
    {"key": "display", "label": "Display", "group": "You", "roles": ALL},
    {"key": "apps", "label": "Connected apps", "group": "You", "roles": ALL},
    {"key": "company", "label": "Company", "group": "Company", "roles": OFFICE_UP},
    {"key": "team", "label": "Team", "group": "Company", "roles": OFFICE_UP},
    {"key": "billing", "label": "Billing", "group": "Company", "roles": OWNER},
    {"key": "ask", "label": "Ask TradeComply", "group": "Help and AI", "roles": OWNER},
    {"key": "mcp", "label": "AI assistant (MCP)", "group": "Help and AI", "roles": OFFICE_UP},
    {"key": "calendar", "label": "Calendar", "group": "Reminders and connections", "roles": OFFICE_UP},
    {"key": "email", "label": "Email", "group": "Reminders and connections", "roles": OWNER},
    {"key": "integrations", "label": "Integrations", "group": "Reminders and connections", "roles": OFFICE_UP},
    {"key": "activity", "label": "Activity log", "group": "Records", "roles": OFFICE_UP},
    {"key": "mcp-log", "label": "AI assistant call log", "group": "Records", "roles": OWNER},
    {"key": "data", "label": "Account and data", "group": "Account and data", "roles": OWNER},
]
SECTION_ROLES = {s["key"]: s["roles"] for s in SETTINGS_SECTIONS}


def _section(key: str, title: str, keys: str, detail: str | None = None) -> dict:
    return {"id": f"settings-{key}", "title": title, "href": f"#/settings?section={key}", "keys": keys,
            "roles": SECTION_ROLES[key], "detail": detail or "Settings"}


# Pages Quick help and search can send people to (spec 5.6): words people use for each, and who can open it.
PAGES = [
    {"id": "dashboard", "title": "Today", "href": "#/dashboard", "roles": ALL,
     "keys": "home dashboard overview today deadlines due this week what's due alerts needs attention"},
    {"id": "projects", "title": "Projects", "href": "#/projects", "roles": ALL, "keys": "jobs job list projects"},
    {"id": "project_new", "title": "Add a project", "href": "#/project/new", "roles": OFFICE_UP,
     "keys": "new project new job add a job add job create project start a job"},
    {"id": "subs", "title": "Subcontractors", "href": "#/subs", "roles": ALL,
     "keys": "subs sub list subcontractor list vendors add a sub coi insurance certificate w-9 license check"},
    {"id": "subs_import", "title": "Import a sub list", "href": "#/subs", "roles": OFFICE_UP,
     "keys": "upload subs import subs sub list csv spreadsheet import a list", "detail": "Subcontractors"},
    {"id": "tools", "title": "Checks", "href": "#/tools", "roles": ALL,
     "keys": "checks tools calculators all checks run a check"},
    {"id": "documents", "title": "Documents", "href": "#/documents", "roles": ALL,
     "keys": "documents docs saved results notes safety programs printouts pdf files print"},
    {"id": "field", "title": "Field log", "href": "#/field", "roles": ALL,
     "keys": "field log toolbox talk tailgate meeting safety meeting inspection incident report sign-in sheet "
             "sign in sheet crew signatures daily log"},
    {"id": "professionals", "title": "Find a professional", "href": "#/professionals", "roles": ALL,
     "keys": "find a professional engineer attorney lawyer notary surveyor architect asbestos consultant hers rater "
             "qsd casp"},
    {"id": "integrations", "title": "Integrations", "href": "#/integrations", "roles": ALL,
     "keys": "integrations apps connect procore docusign slack teams text alerts sms schedule import"},
    {"id": "calendar", "title": "Calendar reminders", "href": "#/integrations?integration=calendar-feed",
     "roles": OFFICE_UP, "keys": "calendar google calendar outlook apple calendar ics reminders deadline reminders "
                                 "feed subscribe", "detail": "Integrations"},
    {"id": "settings", "title": "Settings", "href": "#/settings", "roles": ALL,
     "keys": "settings preferences profile"},
    _section("account", "Your account", "account profile my name my email"),
    _section("account", "Change password", "password change password reset password"),
    _section("display", "Display", "display dark mode light mode theme contrast text size"),
    _section("apps", "Connected apps", "connected apps oauth claude connection"),
    _section("company", "Company", "company name company license number cslb license address"),
    _section("team", "Team", "team invite invite someone add user add a person users roles foreman office crew "
                             "members"),
    _section("billing", "Billing", "billing subscription invoice payment card plan cancel subscription"),
    _section("ask", "Ask TradeComply settings", "ask tradecomply ai helper claude turn off"),
    _section("mcp", "API keys and AI assistant (MCP)", "api key api keys mcp connect claude assistant key"),
    _section("calendar", "Calendar link", "calendar link ics feed"),
    _section("email", "Email", "email sending smtp email settings"),
    _section("activity", "Activity log", "activity log audit history who ran"),
    _section("mcp-log", "AI assistant call log", "call log mcp log assistant calls"),
    _section("data", "Account and data", "export data download my data delete company delete account"),
    {"id": "welcome", "title": "Guided setup", "href": "#/welcome", "roles": OFFICE_UP,
     "keys": "guided setup getting started setup onboarding first steps"},
    {"id": "more", "title": "More", "href": "#/more", "roles": ALL, "keys": "more menu"},
]
PAGE_BY_ID = {p["id"]: p for p in PAGES}

# Starter questions (spec 5.10): every one must get an answer from Quick help (tests/test_quickhelp.py).
PAGE_STARTERS = {
    "dashboard": ["When is my prelim due?", "Can we start at 6:30 am Saturday in San Diego?",
                  "Do I need a permit for a 10x12 shed?"],
    "projects": ["When do I have to record my lien?", "What do I need before we break ground?",
                 "Is a city park job over $1,000 a prevailing wage job?"],
    "subs": ["How do I upload my sub list?", "Does my roofing sub need workers comp with no employees?",
             "What license does an electrician need for a panel upgrade?"],
    "tools": ["Which check gives my lien deadline?", "Do I need a Cal/OSHA permit for a 6 ft trench?",
              "Can a handyman do a $900 job without a license?"],
    "field": ["When is a toolbox talk due?", "What do I do when the AQI is over 150?",
              "Someone was admitted to the hospital, when do I report it?"],
    "documents": ["How do I save a result?", "Where are my safety programs?", "How do I print a document?"],
    "professionals": ["Do I need an engineer for a retaining wall?", "When do I need a CASp inspection?",
                      "Where do I find a HERS rater?"],
    "integrations": ["How do I get deadline reminders on my calendar?", "Can I connect Procore?",
                     "How do I set up text alerts?"],
    "settings": ["How do I invite my foreman?", "How do I change my password?", "Where is billing?"],
}
PAGE_STARTERS["project"] = PAGE_STARTERS["projects"]
GROUP_STARTERS = {
    "start": ["What do I need before we break ground?"],
    "paid": ["When is my prelim due?", "What if no notice of completion was recorded?"],
    "license": ["Does a sub with no employees need workers comp?"],
    "contract": ["How much can I take as a down payment?", "How long does a 72-year-old homeowner have to cancel?"],
    "safety": ["When do I need a trench permit?", "What do I do when the AQI is 175?"],
    "certs": ["Who needs DIR electrician certification?"],
    "pay": ["When is a final paycheck due if I fire someone today?"],
    "public": ["How many apprentice hours on a $2M school job?"],
    "permits": ["Do I need a permit to build a 10x12 shed?", "What's the max size for a detached ADU?"],
    "access": ["When does a remodel need path-of-travel upgrades?"],
    "equipment": ["Do my excavators fall under CARB's off-road rule?"],
    "rules": ["Do I need an engineer for shoring over 20 feet?"],
}

# Follow-up questions per Quick help intent (2 each); every one must get an answer (tests/test_quickhelp.py).
FOLLOWUPS = {
    "lien": ["What if no notice of completion was recorded?", "How long do I have to foreclose on a lien?"],
    "stop_notice": ["When is a stop payment notice due on a public job?", "How do I serve a stop payment notice?"],
    "cancel": ["How long does a homeowner over 65 have to cancel?", "Can a homeowner cancel a contract by email?"],
    "down_payment": ["Can I take payment before the work is done?", "How much can I take as a down payment?"],
    "retention": ["When does retention have to be released on a private job?",
                  "What's the retention cap on a public job?"],
    "prompt_pay": ["How fast do I have to pay my subs after I get paid?", "When is retention due to my sub?"],
    "osha_permit": ["Do I need a Cal/OSHA permit for scaffolding over 36 feet?",
                    "Do I need a Cal/OSHA permit for a 5 ft trench?"],
    "final_pay": ["When is the final paycheck due if someone quits without notice?",
                  "When is the final paycheck due after a layoff?"],
    "injury": ["When do I have to record an injury on the Log 300?",
               "How fast do I report a death on the job to Cal/OSHA?"],
    "smoke": ["When are respirators required for wildfire smoke?", "What do I do when the AQI is over 150?"],
    "valley_fever": ["Do we need valley fever training in Fresno County?",
                     "Does valley fever training apply to grading in Kern County?"],
    "heat": ["When do I have to provide shade?", "How much drinking water do workers need when it's hot?"],
    "toilets": ["How many toilets do I need for 30 workers?", "Do I need hand washing stations on a jobsite?"],
    "hours": ["What are construction hours in San Diego?", "Can we work on Sunday in Los Angeles?"],
    "min_wage": ["What is the minimum wage in San Diego?", "What is the California minimum wage?"],
    "sick": ["How much paid sick leave do workers get?", "When can a new hire use paid sick leave?"],
    "overtime": ["When is daily overtime due in California?", "When do workers get a meal break?"],
    "public_works": ["Do I need DIR registration for a public job?", "When does prevailing wage apply?"],
    "apprentice": ["What is the apprentice ratio on public works?", "When do apprentice rules start on public works?"],
    "adu": ["What's the max size for a detached ADU?", "How long does the city have to approve an ADU?"],
    "energy": ["Does a reroof trigger Title 24?", "When is a HERS test required?"],
    "stormwater": ["When do I need a SWPPP?", "Who can write a SWPPP?"],
    "lead": ["Do lead paint rules apply to a 1960s house?", "What's the minor repair limit for lead paint?"],
    "asbestos": ["When do I need asbestos registration?", "Is removing 50 sq ft of asbestos tile regulated?"],
    "building_permit": ["Do I need a permit for a fence?", "Do I need a permit for a retaining wall?"],
    "safety": ["When is fall protection required?", "What are the silica rules for concrete cutting?"],
    "unlicensed": ["Can a handyman do a job under $1,000?", "What's the penalty for contracting without a license?"],
    "license_lookup": ["How do I check a contractor's license?", "Does my roofing sub need workers comp?"],
    "workers_comp": ["Does a sub with no employees need workers comp?",
                     "Which license classes always need workers comp?"],
    "classification": ["What license do I need for a panel upgrade?", "Can a B contractor do a roofing job?"],
    "find_pro": ["Do I need an engineer for a retaining wall?", "Where do I find a HERS rater?"],
    "toolbox": ["When is a toolbox talk due?", "Do I need a written injury and illness prevention program?"],
    "setup": ["What do I need before we break ground?", "Do I need to call 811 before digging?"],
    "offroad": ["Do my excavators fall under CARB's off-road rule?",
                "Do we have to report our off-road fleet to CARB?"],
    "certs": ["Who needs DIR electrician certification?", "Does a crane operator have to be certified?"],
}


def layout_for(name: str) -> dict | None:
    lay = LAYOUTS.get(name)
    return copy.deepcopy(lay) if lay else None


def annotate(spec: dict) -> dict:
    """The /api/tools view of one tool spec: a deep copy (never the cached spec Ask TradeComply sends to Anthropic)
    with the catalog title, summary, group, hidden flag, layout and x-label on fields that have a plain label."""
    out = copy.deepcopy(spec)
    c = TOOLS.get(spec["name"])
    if c:
        out.update(title=c["title"], summary=c["summary"], group=c["group"], group_label=GROUP_LABELS[c["group"]],
                   hidden=c["hidden"])
    else:   # a tool added to the server after this catalog was written: listed under its spec title
        out.update(summary=(spec.get("description") or "").split(". ")[0][:110], group=None, group_label=None,
                   hidden=False)
    out["layout"] = layout_for(spec["name"])
    props = (out.get("input_schema") or {}).get("properties") or {}
    for field, schema in props.items():
        if field in LABELS and isinstance(schema, dict):
            schema["x-label"] = LABELS[field]
    return out


def groups_payload(names: set[str]) -> list[dict]:
    """Display-ordered groups with their tools limited to `names` (an API key's scope) and their starters."""
    return [{"id": g["id"], "label": g["label"], "icon": g["icon"], "purpose": g["purpose"],
             "tools": [t for t in g["tools"] if t in names], "starters": list(GROUP_STARTERS.get(g["id"], []))}
            for g in GROUPS]


def common_payload(names: set[str]) -> dict:
    return {role: [t for t in tools if t in names] for role, tools in COMMON.items()}


def title(name: str, fallback: str | None = None) -> str:
    c = TOOLS.get(name)
    return c["title"] if c else (fallback or name)


def pages_for(role: str) -> list[dict]:
    return [p for p in PAGES if role in p["roles"]]
