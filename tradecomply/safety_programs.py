"""Cal/OSHA written safety programs, 8 CCR 341 permits and injury reporting for California construction employers.

  generate_safety_programs    company profile -> which written programs are required (IIPP, Code of Safe Practices,
                              heat illness, workplace violence, HazCom, respiratory, silica, emergency medical, fall
                              protection plan, excavation, cranes, lead/asbestos), each as a fill-in markdown outline
                              naming the responsible person, plus training schedule, Log 300 recordkeeping, postings
  check_cal_osha_permits      job scope -> 8 CCR 341 project/annual permits, asbestos registration, lead and demolition
                              notices and 811, with fees and the latest date to apply or notify
  injury_reporting_deadlines  incident -> DOSH 8-hour report, DWC-1, Form 5020, Log 300/301, 300A posting, penalties

Every legal number comes from core.rule(id).param(key) at call time. Values with no rule live in UNVERIFIED and make
any result that uses them verified=False. Working days are counted Monday-Friday; state holidays are not skipped.
"""
import math
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

from . import core, logic

# Values with no rule in the rulebook. A result that uses one is marked verified=False and names the source.
UNVERIFIED = {
    "lc6500_three_stories": {
        "stories_gt": 3,
        "text": "Labor Code 6500 requires a Cal/OSHA permit to construct or demolish any building, structure, "
                "falsework or scaffolding more than three stories high or the equivalent height; 8 CCR 341 states "
                "that height as more than 36 ft.",
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=LAB&sectionNum=6500",
    },
    "excavation_over_20ft_engineer": {
        "depth_ft_gt": 20,
        "text": "Excavations deeper than 20 ft: sloping or benching must be designed by a registered professional "
                "engineer (8 CCR 1541.1 Appendix B, Table B-1 note 3), and the Appendix C timber and Appendix D "
                "aluminum hydraulic shoring tables stop at 20 ft, so shoring or shields must follow the manufacturer's "
                "tabulated data for that depth, other tabulated data approved by a registered professional engineer, "
                "or a registered professional engineer's design (8 CCR 1541.1(c)(2)-(4)).",
        "source": "https://www.dir.ca.gov/title8/1541_1b.html",
    },
    "hazcom_training_timing": {
        "text": "Train employees on hazardous chemicals in their work area at the time of their initial assignment and "
                "whenever a new chemical hazard is introduced into their work area (8 CCR 5194(h)(1)).",
        "source": "https://www.dir.ca.gov/title8/5194.html",
    },
    "hazcom_list_and_nonroutine": {
        "text": "The written program must also include a list of the hazardous chemicals known to be present (by the "
                "product identifier on each SDS, for the whole workplace or by work area) and the methods used to "
                "inform employees of the hazards of non-routine tasks and of chemicals in unlabeled pipes in their "
                "work areas (8 CCR 5194(e)(1)(A)-(B)).",
        "source": "https://www.dir.ca.gov/title8/5194.html",
    },
    "respirator_program_elements": {
        "elements": ["Procedures for selecting respirators", "Medical evaluation of employees required to use "
                     "respirators (by a physician or other licensed health care professional, before fit testing)",
                     "Fit-testing procedures for tight-fitting respirators", "Proper use in routine and reasonably "
                     "foreseeable emergency situations", "Cleaning, disinfecting, storing, inspecting, repairing and "
                     "discarding respirators", "Breathing-air quality for atmosphere-supplying respirators",
                     "Training on respiratory hazards and on proper use, limitations and maintenance",
                     "Regular evaluation of the program's effectiveness"],
        "text": "Written respirator program procedures listed in 8 CCR 5144(c)(1)(A)-(I).",
        "source": "https://www.dir.ca.gov/title8/5144.html",
    },
    "emergency_action_plan_3220": {
        "elements": ["How to report a fire or other emergency", "Evacuation procedures and route assignments",
                     "Procedures for employees who stay behind to shut down critical operations",
                     "How all employees are accounted for after evacuation", "Rescue and medical duties",
                     "Names or job titles of people to contact for more information", "Alarm / alert method"],
        "text": "8 CCR 3220 sets the contents of an Emergency Action Plan wherever a Title 8 order requires one; "
                "many contractors include it in the IIPP. The plan must be written, except that an employer with 10 "
                "or fewer employees may communicate it orally (3220(e)(3)). Confirm with your safety professional "
                "whether your operations trigger it.",
        "source": "https://www.dir.ca.gov/title8/3220.html",
    },
    "indoor_heat_response_training": {
        "text": "Emergency response procedures (respond to symptoms, never leave a symptomatic employee alone or send "
                "them home without offering first aid or emergency medical services, clear directions to the site "
                "for responders) and effective training of every employee and supervisor before work that may "
                "expose them to heat illness risk (8 CCR 3396(f), (h)).",
        "source": "https://www.dir.ca.gov/title8/3396.html",
    },
    "log300_survey_and_5020": {
        "text": "Size-exempt employers must still keep Cal/OSHA injury and illness records if OSHA or the BLS informs "
                "them in writing that they must (14300.41 / 14300.42), and must still file occupational injury "
                "reports (Form 5020) with the Division of Labor Statistics and Research or their insurer "
                "(8 CCR 14300.1(a)(1)).",
        "source": "https://www.dir.ca.gov/T8/14300_1.html",
    },
    "dwc1_trigger_5401a": {
        "text": "Labor Code 5401(a) ties the one-working-day claim-form duty to injuries that cause lost time beyond "
                "the employee's work shift at the time of injury or need medical treatment beyond first aid. DIR's "
                "employer fact sheet says to give the form for any injury or illness you learn of, which is the safe "
                "practice; if the worker left work early or missed any later shift, the form is required.",
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=LAB&sectionNum=5401",
    },
    "log300_recording_criteria": {
        "criteria": ["death", "days away from work", "restricted work or job transfer", "medical treatment beyond "
                     "first aid", "loss of consciousness", "significant injury or illness diagnosed by a physician "
                     "or other licensed health care professional"],
        "text": "Log 300 general recording criteria (8 CCR 14300.7).",
        "source": "https://www.dir.ca.gov/T8/14300_7.html",
    },
}
WORKING_DAY_NOTE = ("Working days are counted Monday-Friday; state holidays are not skipped, so if a holiday falls in "
                    "the window, act one working day earlier for each holiday.")


def P(rule_id: str, key: str):
    return core.rule(rule_id).param(key)


def _cit(rule_id: str) -> str:
    return core.rule(rule_id).citation_text or rule_id


def _ok(rule_ids, unverified=()) -> bool:
    return not unverified and all(core.rule(r).verified for r in rule_ids)


def _date(value, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ValueError(f"{field} must be an ISO date like 2026-03-15, got {value!r}") from None


def _dt(value, field: str) -> tuple[datetime, bool]:
    """ISO date or datetime (California local) -> (naive local datetime, date_only)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return datetime.combine(value, time()), True
    else:
        s = str(value or "").strip()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"{field} must be ISO like 2026-11-20T14:30 or 2026-11-20, got {value!r}") from None
        if len(s) <= 10:
            return dt, True
    if dt.tzinfo is not None:
        dt = dt.astimezone(logic.CA_TZ).replace(tzinfo=None)
    return dt, False


def _add_working_days(d: date, n: int) -> date:
    """Move n working days (Mon-Fri) from d; negative n moves back."""
    step = 1 if n >= 0 else -1
    left = abs(n)
    while left:
        d += timedelta(days=step)
        if d.weekday() < 5:
            left -= 1
    return d


def _md(month_day: str, year: int) -> date:
    m, d = (int(x) for x in month_day.split("-"))
    return date(year, m, d)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


# ================================================================ models
class Element(BaseModel):
    item: str
    requirement: str
    fill_in: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    verified: bool = False


def _el(item, requirement, rule_ids=(), fill_in=(), unverified=()) -> Element:
    return Element(item=item, requirement=requirement, fill_in=list(fill_in), rule_ids=list(rule_ids),
                   unverified=list(unverified), verified=_ok(rule_ids, unverified))


ProgramStatus = Literal["required", "conditional", "exempt", "not_required"]


class Program(BaseModel):
    key: str
    name: str
    status: ProgramStatus
    why: str
    responsible_role: str
    named_people: list[str] = Field(default_factory=list,
                                    description="Licensed or designated people the law requires for this program")
    elements: list[Element] = Field(default_factory=list)
    review: str = ""
    document_outline_md: str = ""
    rule_ids: list[str] = Field(default_factory=list)
    verified: bool = False


class Line(BaseModel):
    item: str
    detail: str
    due: Optional[str] = None
    rule_ids: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    verified: bool = False


def _line(item, detail, rule_ids=(), due=None, unverified=()) -> Line:
    return Line(item=item, detail=detail, due=due, rule_ids=list(rule_ids), unverified=list(unverified),
                verified=_ok(rule_ids, unverified))


def _finish(res: core.Result, parts) -> core.Result:
    """Cite every rule the parts used; any UNVERIFIED value makes the whole result unverified."""
    ids, unv = [], set()
    for p in parts:
        for r in getattr(p, "rule_ids", []):
            if r not in ids:
                ids.append(r)
        for el in getattr(p, "elements", []):
            ids.extend(r for r in el.rule_ids if r not in ids)
            unv.update(el.unverified)
        unv.update(getattr(p, "unverified", []))
    res.cite(*ids)
    if unv:
        res.verified = False
        for k in sorted(unv):
            res.notes.append(f"Unverified (no rulebook entry): {UNVERIFIED[k].get('text', k)} Source: "
                             f"{UNVERIFIED[k]['source']}")
    return res


def _program(key, name, status, why, role, elements, named=(), review="", company="", as_of=None) -> Program:
    ids, unv = [], []
    for e in elements:
        ids.extend(r for r in e.rule_ids if r not in ids)
        unv.extend(e.unverified)
    p = Program(key=key, name=name, status=status, why=why, responsible_role=role, named_people=list(named),
                elements=elements, review=review, rule_ids=ids, verified=_ok(ids, unv))
    if status in ("required", "conditional"):
        p.document_outline_md = _outline(p, company, as_of)
    return p


def _outline(p: Program, company: str, as_of: date) -> str:
    out = [f"# {p.name}", "", f"**Company:** {company}  ", f"**Prepared:** {as_of.isoformat()}  "
           "**Last reviewed:** __________  ", f"**Responsible person ({p.responsible_role}):** "
           "Name __________ Title __________ Phone __________", ""]
    if p.named_people:
        out += ["**People this program must name:**", *[f"- {n}: __________" for n in p.named_people], ""]
    out += [f"_Why this program applies: {p.why}_", ""]
    for i, e in enumerate(p.elements, 1):
        out += [f"## {i}. {e.item}", e.requirement]
        out += [f"- [ ] {f}: __________" for f in e.fill_in]
        cites = "; ".join(dict.fromkeys(part for r in e.rule_ids for part in _cit(r).split("; ")))
        if e.unverified:
            cites = "; ".join(filter(None, [cites, "unverified: " + ", ".join(UNVERIFIED[k]["source"]
                                                                              for k in e.unverified)]))
        out += [f"_Authority: {cites}_", ""]
    if p.review:
        out += ["## Review and updates", p.review, ""]
    out += ["---", core.DISCLAIMER]
    return "\n".join(out)


class SafetyProgramsResult(core.Result):
    as_of: str
    company_name: str
    employee_count: int
    required_programs: list[str]
    programs: list[Program]
    training: list[Line]
    recordkeeping: list[Line]
    log300_required: bool
    postings: list[Line]
    permits_to_check: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ================================================================ programs
_IIPP_TEXT = {
    "responsible_person_identified": (
        "Responsible person", "Identify the person(s) with authority and responsibility for implementing the program.",
        ["Name and title of the IIPP administrator", "Backup when the administrator is away"]),
    "compliance_system": (
        "Compliance system", "A system to make sure employees follow safe work practices: recognition of safe "
        "work, training and retraining, and disciplinary steps.", ["How safe work is recognized",
                                                                   "Disciplinary steps (verbal, written, removal)"]),
    "communication_system_incl_reporting_without_reprisal": (
        "Communication with employees", "Communicate on safety in a form employees readily understand, including a "
        "way to report hazards without fear of reprisal.",
        ["Toolbox/tailgate meeting schedule", "How workers report hazards (anonymous option)",
         "Languages used on site"]),
    "hazard_identification_and_periodic_inspections": (
        "Hazard identification and inspections", "Scheduled periodic inspections, plus an inspection when the "
        "program is first set up, when new substances, processes or equipment bring new hazards, and when you learn "
        "of a new or previously unrecognized hazard. The regulation sets no fixed interval; you set it.",
        ["Jobsite inspection frequency (e.g. weekly)", "Who inspects", "Checklist used"]),
    "injury_illness_investigation_procedure": (
        "Injury and illness investigation", "A procedure to investigate occupational injuries and illnesses.",
        ["Who investigates", "Form used", "How findings feed hazard correction"]),
    "hazard_correction_procedures": (
        "Hazard correction", "Correct unsafe conditions in a timely manner based on severity. If an imminent hazard "
        "cannot be corrected immediately, remove everyone from the area except the people needed to correct it.",
        ["Correction timeframes by severity", "Who can stop work"]),
    "training_and_instruction": (
        "Training and instruction", "Train when the program starts, every new employee, employees given new "
        "assignments, when new substances, processes or equipment bring new hazards, when new hazards are "
        "recognized, and supervisors on the hazards their crews face.",
        ["New-hire orientation content", "Who trains", "Training record form"]),
    "employee_access_to_program": (
        "Employee access to the IIPP", "Give an employee or designated representative access to the IIPP no later "
        "than {d} business days after a request; the first printed copy is free.",
        ["Where the IIPP is kept", "Who handles requests"]),
}


def _iipp(company, n, as_of) -> Program:
    keys = P("iipp-written-program-required", "required_elements")
    els = []
    for k in keys:
        title, req, fill = _IIPP_TEXT.get(k, (k.replace("_", " ").capitalize(), "See 8 CCR 3203(a).", []))
        ids = ["iipp-written-program-required", "iipp-1509-construction-iipp"]
        if k == "communication_system_incl_reporting_without_reprisal":
            lt = P("iipp-small-employer-oral-communication", "employee_count_lt")
            req += (f" You have {n} employees, which is under {lt}: you may meet this element by instructing "
                    "employees orally (the program itself must still be written)." if n < lt else
                    f" With {n} employees ({lt} or more) the oral-only exception is not available to you.")
            ids.append("iipp-small-employer-oral-communication")
        elif k == "hazard_identification_and_periodic_inspections":
            ids.append("iipp-hazard-inspection-triggers")
        elif k == "hazard_correction_procedures":
            ids.append("iipp-hazard-correction")
        elif k == "training_and_instruction":
            ids.append("iipp-training-triggers")
        elif k == "employee_access_to_program":
            req = req.format(d=P("iipp-employee-access-5-business-days", "deadline_business_days"))
            ids.append("iipp-employee-access-5-business-days")
        els.append(_el(title, req, ids, fill))
    yrs = P("iipp-inspection-records-1-year", "retention_years_min")
    rec_req = (f"Keep inspection records (inspector, unsafe conditions found, corrective action) and training records "
               f"(employee, dates, type, trainer) at least {yrs} year(s).")
    # 3203(b) Exceptions 1-2 use the same "fewer than 10 employees" cut as the (a)(3) oral exception; the records
    # rules carry it only as a flag (their quote states "fewer than 10"), so the number comes from the (a)(3) rule.
    lt10 = P("iipp-small-employer-oral-communication", "employee_count_lt")
    if n < lt10 and P("iipp-inspection-records-1-year", "small_employer_lt_10_may_keep_until_hazard_corrected"):
        rec_req += (f" With fewer than {lt10} employees you may keep inspection records only until the hazard is "
                    "corrected, and may document training with a log of the instructions given to each employee at "
                    "hire or on new duties (3203(b) Exceptions 1-2).")
    els.append(_el("Records", rec_req, ["iipp-inspection-records-1-year", "iipp-training-records-1-year"],
                   ["Where inspection and training records are filed"]))
    lt20, emr = (P("iipp-reduced-documentation-under-20", "employee_count_lt"),
                 P("iipp-reduced-documentation-under-20", "emr_max"))
    if n < lt20:
        els.append(_el("Reduced documentation (check eligibility)",
                       f"With fewer than {lt20} employees you may limit written documentation to the responsible "
                       f"person, periodic inspections and training ONLY if your industry is not on DIR's high-hazard "
                       f"list and your EMR is {emr} or less. Many construction trades are on that list, so do not "
                       "rely on this without checking.", ["iipp-reduced-documentation-under-20"],
                       ["Your EMR", "Is your trade on DIR's high-hazard list?"]))
    return _program("iipp", "Injury and Illness Prevention Program (IIPP)", "required",
                    "Every California employer needs a written, implemented IIPP; construction employers are required "
                    "to have one by 8 CCR 1509(a) as well.",
                    "IIPP Administrator (owner, officer or safety manager with authority to implement it)", els,
                    review="Update when you add new work, equipment or chemicals, after any serious injury, and at "
                           "least yearly as good practice. Operating without an IIPP blocks good-faith and history "
                           "penalty reductions (8 CCR 336(d)(8)).", company=company, as_of=as_of)


def _csp(company, as_of, hazards: list[str]) -> Program:
    wd = P("toolbox-tailgate-meetings-10-working-days", "max_interval_working_days")
    els = [
        _el("Written Code of Safe Practices", "Adopt a written Code of Safe Practices specific to your operations, "
            f"in language equivalent to the relevant parts of {P('code-of-safe-practices-written', 'model_language')}.",
            ["code-of-safe-practices-written"], ["General rules (housekeeping, PPE, reporting injuries)"]),
        _el("Trade-specific safe practices", "Add rules for the hazards your crews actually face: "
            + (", ".join(hazards) if hazards else "list your trade's hazards") + ".", ["code-of-safe-practices-written",
                                                                                     "pre-job-site-hazard-survey"],
            [f"Rules for {h}" for h in hazards] or ["Trade hazards and rules"]),
        _el("Posting", f"Post it in a conspicuous place at each jobsite office "
            f"({P('code-of-safe-practices-posting', 'posting_location')}), or give a copy to every supervisor to "
            "keep readily available.", ["code-of-safe-practices-posting"], ["Where posted / who holds copies"]),
        _el("Toolbox / tailgate meetings", f"Supervisors hold a toolbox or tailgate safety meeting with their crews at "
            f"least every {wd} working days (working days, not calendar days).",
            ["toolbox-tailgate-meetings-10-working-days"], ["Meeting day of the week", "Sign-in sheet location"]),
        _el("Supervisor safety meetings", "Management holds periodic meetings of supervisors on safety problems and "
            "accidents that occurred.", ["supervisor-safety-meetings"], ["Frequency (e.g. monthly)"]),
        _el("New-worker instruction", "When a worker is first hired, instruct them on the hazards and precautions of "
            "their work and direct them to read the Code of Safe Practices.", ["new-worker-safety-instruction"],
            ["Orientation checklist"]),
        _el("Pre-job hazard survey", "Before employees are on a site, survey it for predictable hazards and the "
            "safeguards needed.", ["pre-job-site-hazard-survey"], ["Survey form / who performs it"]),
        _el("Qualified operators", "Only qualified persons may operate equipment and machinery.",
            ["qualified-operators-only", "qualified-person-definition"], ["Equipment and authorized operators"]),
    ]
    return _program("code_of_safe_practices", "Code of Safe Practices", "required",
                    "Every construction employer must adopt a written Code of Safe Practices (8 CCR 1509(b)).",
                    "Superintendent / foreman at each jobsite", els, company=company, as_of=as_of)


def _heat_outdoor(company, site_n, shift_h, language, as_of) -> Program:
    qph = P("heat-water-one-quart-per-hour", "quarts_per_employee_per_hour")
    quarts = site_n * qph * shift_h
    shade_f = P("heat-shade-above-80f", "temperature_f_gt")
    hi_f = P("heat-high-heat-procedures-95f", "temperature_f_gte")
    per = P("heat-high-heat-procedures-95f", "observation_options.supervisor_or_designee_max_employees")
    observers = math.ceil(site_n / per)
    rest = P("heat-cool-down-rest-5-minutes", "min_rest_minutes")
    acc = P("heat-acclimatization-14-days", "new_employee_observation_days")
    hw = P("heat-acclimatization-14-days", "heat_wave_definition")
    langs = P("heat-written-plan-outdoor", "languages")
    lang = f"English and {language}" if language and language.lower() != "english" else \
        " and ".join(langs)
    els = [
        _el("Water", f"Fresh, pure, suitably cool water, free, as close as practicable to the work. If not plumbed, "
            f"bring {qph} quart per employee per hour at shift start: {site_n} workers x {qph} qt x {shift_h:g} h = "
            f"{quarts:g} quarts ({quarts / 4:g} gallons) per site per shift, unless you have effective refill "
            "procedures. Encourage frequent drinking.", ["heat-water-one-quart-per-hour"],
            ["Water source and refill procedure", "Who checks water levels"]),
        _el("Shade", f"Above {shade_f}F keep shade up at all times while employees are present, large enough for "
            f"everyone on rest or recovery to sit fully in the shade without touching, and during meal periods for "
            f"everyone who stays on site to eat. At or below {shade_f}F provide shade or timely access on request.",
            ["heat-shade-above-80f"], ["Shade type (canopy, building)", "Who sets it up"]),
        _el("Cool-down rests", f"Allow a cool-down rest in the shade whenever an employee asks; monitor them, ask about "
            f"symptoms, and do not order them back until symptoms are gone and at least {rest} minutes have passed "
            "(plus time to reach shade).", ["heat-cool-down-rest-5-minutes"], ["Who monitors resting workers"]),
        _el("High-heat procedures", f"At {hi_f}F or higher (construction is a listed high-heat industry): effective "
            f"communication with a supervisor; observation by one supervisor/designee per {per} or fewer workers "
            f"({site_n} workers -> at least {observers} observer(s)), a mandatory buddy system, or regular contact "
            "with solo workers; a designated person at each site to call emergency medical services; water "
            "reminders through the shift; and a pre-shift meeting reviewing these procedures and the right to "
            "cool-down rests.", ["heat-high-heat-procedures-95f", "heat-pre-shift-meeting-high-heat",
                                 "heat-outdoor-construction-full-coverage"],
            ["Observation method", "Designated EMS caller at each site", "How you track the forecast"]),
        _el("Emergency response", "Working communication to reach a supervisor or EMS, immediate response to symptoms, "
            "never leave a symptomatic worker alone or send them home without offering first aid/EMS, and clear "
            "directions to the site for responders.", ["heat-emergency-response"],
            ["Site address/cross streets card", "Nearest hospital"]),
        _el("Acclimatization", f"Closely observe every employee newly assigned to a high-heat area for their first "
            f"{acc} days, and all employees during a heat wave (forecast high at least {hw['predicted_high_f_gte']}F "
            f"and at least {hw['above_prior_5_day_avg_high_f_gte']}F above the prior {hw['lookback_days']}-day "
            "average high).", ["heat-acclimatization-14-days"], ["How new workers are paired/observed"]),
        _el("Training", "Train every employee and supervisor on heat illness before work with heat exposure starts; "
            "supervisors also on implementing the procedures, responding to symptoms and monitoring weather.",
            ["heat-training-before-exposure"], ["Training date and trainer"]),
        _el("Written plan and languages", f"Keep the plan in writing in {lang}, available at the worksite. It may be "
            "part of the IIPP.", ["heat-written-plan-outdoor"], ["Where the plan is kept on each site"]),
    ]
    return _program("heat_illness_outdoor", "Heat Illness Prevention Plan (outdoor, 8 CCR 3395)", "required",
                    "The outdoor heat standard covers all outdoor places of employment, and construction must also "
                    "follow its high-heat procedures.",
                    "Foreman / supervisor at each outdoor site, plus a designated EMS caller on high-heat days", els,
                    named=["Designated person to call emergency medical services at each worksite"],
                    company=company, as_of=as_of)


def _heat_indoor(company, as_of) -> Program:
    sc = core.rule("indoor-heat-scope-82f").parameters
    full = sc["full_controls_triggers"]
    cool = P("indoor-heat-cool-down-area", "cool_down_area_temp_f_lt")
    rest = P("indoor-heat-cool-down-area", "min_rest_minutes")
    m = core.rule("indoor-heat-measurement-records").parameters
    acc = core.rule("indoor-heat-acclimatization-14-days").parameters
    inc = sc["incidental_exposure_exception"]
    trig = (f"areas at {full['temperature_f_gte']}F or more (temperature or heat index), or at "
            f"{full['restrictive_clothing_and_temperature_f_gte']}F or more where employees wear heat-restrictive "
            "clothing or work near high radiant heat")
    els = [
        _el("Scope", f"Applies to fully enclosed indoor work areas at {sc['scope_temperature_f_gte']}F or more while "
            f"employees are present. Measurement and control measures (3396(e)) apply only in {trig}. Not covered: "
            f"incidental exposure at {inc['temperature_f_gte']}F to under {inc['temperature_f_lt']}F for less than "
            f"{inc['exposure_minutes_lt']} minutes in any {inc['per_period_minutes']}-minute period (not available in "
            "vehicles without working air conditioning). Partially enclosed construction areas may fall under the "
            "outdoor rule instead.",
            ["indoor-heat-scope-82f"], ["Indoor areas that reach 82F (attics, mechanical rooms, unconditioned shells)",
                                        "Which of those reach the 87F / 82F control triggers"]),
        _el("Water and cool-down areas", f"Water as in the outdoor rule; one or more cool-down areas kept below {cool}F "
            f"unless infeasible; cool-down rests of at least {rest} minutes plus access time.",
            ["indoor-heat-cool-down-area"], ["Cool-down area location"]),
        _el("Measurement and records", f"In {trig}: measure temperature and heat index, record the greater with date, "
            f"time and location, re-measure when readings are expected to rise "
            f"{m['remeasure_when_expected_increase_f_gte']}F or more, and keep records {m['retention_months']} months "
            "or until the next measurement, whichever is later.", ["indoor-heat-measurement-records",
                                                                  "indoor-heat-scope-82f"],
            ["Who measures", "Instrument used"]),
        _el("Control measures", f"In {trig}: engineering controls first (fans, ventilation, cooling), then "
            "administrative controls, then personal heat-protective equipment.",
            ["indoor-heat-control-hierarchy", "indoor-heat-scope-82f"], ["Controls for each hot area"]),
        _el("Acclimatization", f"Closely observe an employee newly assigned to an area at {acc['trigger_f_gte']}F or "
            f"more (temperature or heat index, whichever is greater), or {acc['trigger_restrictive_or_radiant_f_gte']}F"
            f" or more with heat-restrictive clothing or high radiant heat, for their first "
            f"{acc['new_employee_observation_days']} days; where no engineering controls hold off outdoor heat, "
            "observe everyone during a heat wave.", ["indoor-heat-acclimatization-14-days"], ["Observation method"]),
        _el("Emergency response and training", UNVERIFIED["indoor_heat_response_training"]["text"],
            ["indoor-heat-written-plan"], ["EMS caller", "Training date and trainer"],
            unverified=["indoor_heat_response_training"]),
        _el("Written plan", "Written in English and the language of most employees, available at the worksite; may be "
            "combined with the IIPP or the outdoor heat plan.", ["indoor-heat-written-plan"], ["Plan location"]),
    ]
    return _program("heat_illness_indoor", "Indoor Heat Illness Prevention Plan (8 CCR 3396)", "required",
                    f"You reported indoor work areas that reach {sc['scope_temperature_f_gte']}F or more.",
                    "Foreman / supervisor of indoor work",
                    els, company=company, as_of=as_of)


_WVPP_TEXT = {
    "responsible_persons_names_or_titles": "Names or job titles of the people responsible for the plan",
    "employee_and_representative_involvement": "How employees and their representatives help develop and run the plan",
    "coordination_with_other_employers": "How you coordinate with the GC, subs and other employers on shared sites",
    "compliance_procedures": "How supervisors and employees are held to the plan",
    "communication_and_training_procedures": "How you communicate about workplace violence and train employees",
    "hazard_identification_evaluation_correction": "How you identify, evaluate and correct workplace violence hazards",
    "response_to_reports_and_anti_retaliation": "How you accept and respond to reports, with no retaliation",
    "emergency_response": "How you respond to actual or potential violent emergencies (alerts, shelter, getting help)",
    "post_incident_response_and_investigation": "Post-incident response and investigation",
    "plan_review_and_revision": "Plan review and revision",
    "other_division_required_procedures": "Any other procedures Cal/OSHA requires",
}


def _wvpp(company, n_site, public, as_of) -> Program:
    ex = core.rule("wvpp-exemptions").parameters
    lt = ex["small_site_exempt_if_employees_at_any_time_lt"]
    exempt = n_site < lt and not public
    why = (f"Small-site exemption: fewer than {lt} employees at the place at any given time ({n_site}) and not "
           "accessible to the public, provided you comply with the IIPP (8 CCR 3203). Cal/OSHA may still order "
           "compliance, and each place (office, yard, every jobsite) must meet the test on its own."
           if exempt else
           f"Required: {n_site} employees at a place at one time (exempt only under {lt}) and "
           f"{'sites are' if public else 'sites are not'} accessible to the public; the exemption needs both "
           f"fewer than {lt} employees AND no public access.")
    keys = P("wvpp-written-plan-required", "required_elements")
    els = [_el("Coverage", why, ["wvpp-exemptions"], ["Places that claim the small-site exemption, if any"])]
    els += [_el(_WVPP_TEXT.get(k, k.replace("_", " ").capitalize()), "Required plan element (Labor Code 6401.9(c)).",
                ["wvpp-written-plan-required"], ["Your procedure"]) for k in keys]
    rec = core.rule("wvpp-records-retention").parameters
    els += [
        _el("Violent incident log", "Record every workplace violence incident, including threats, with no personal "
            "identifying information; on multi-employer sites give a copy to the controlling employer (Cal/OSHA "
            "model plan).", ["wvpp-violent-incident-log"], ["Log keeper"]),
        _el("Training", f"Train when the plan is first established, at least every "
            f"{P('wvpp-training-initial-and-annual', 'training_interval_months_max')} months, and when new hazards "
            "are identified or the plan changes.", ["wvpp-training-initial-and-annual"], ["Annual training month"]),
        _el("Records", f"Keep hazard records, violent incident logs and investigation records at least "
            f"{rec['hazard_records_years_min']} years and training records at least {rec['training_records_years_min']}"
            f" year; no medical information. Provide them to employees free within "
            f"{P('wvpp-records-access-15-days', 'deadline_calendar_days')} calendar days of a request.",
            ["wvpp-records-retention", "wvpp-records-access-15-days"], ["Records location"]),
        _el("Crime victims", f"If an employee is the victim of a crime at work, give written notice of workers' comp "
            f"eligibility within {P('wvpp-workers-comp-notice-one-working-day', 'workers_comp_notice_working_days')}"
            " working day.", ["wvpp-workers-comp-notice-one-working-day"], ["Who sends the notice"]),
    ]
    review = (f"Review at least every {P('wvpp-plan-review-annual', 'review_interval_months_max')} months, when a "
              "deficiency becomes apparent, and after every workplace violence incident.")
    return _program("wvpp", "Workplace Violence Prevention Plan (Labor Code 6401.9)",
                    "exempt" if exempt else "required", why, "WVPP Administrator (name or job title)", els,
                    review=review, company=company, as_of=as_of)


def _hazcom(company, as_of) -> Program:
    els = [
        _el("Written program kept at the workplace", "Describe how labels and other warnings, safety data sheets "
            "(SDS) and employee information and training will be handled.", ["hazcom-written-program"],
            ["Where the written program is kept"]),
        _el("Labels and warnings", "Containers labeled; secondary containers marked.", ["hazcom-written-program"],
            ["Labeling method for secondary containers"]),
        _el("Safety data sheets", "SDS for each hazardous chemical, readily accessible during each shift.",
            ["hazcom-written-program"], ["SDS binder / app location"]),
        _el("Chemical list and non-routine tasks", UNVERIFIED["hazcom_list_and_nonroutine"]["text"],
            ["hazcom-written-program"], ["Hazardous chemical list (by SDS product identifier)",
                                         "How crews are briefed before non-routine tasks"],
            unverified=["hazcom_list_and_nonroutine"]),
        _el("Training", UNVERIFIED["hazcom_training_timing"]["text"], ["hazcom-written-program"],
            ["Training schedule"], unverified=["hazcom_training_timing"]),
        _el("Multi-employer sites", "Explain how you inform other employers on the site of hazardous chemicals their "
            "workers may be exposed to, and how you get theirs.", ["hazcom-written-program"],
            ["How SDS are exchanged with the GC and other subs"]),
    ]
    return _program("hazcom", "Hazard Communication Program (8 CCR 5194)", "required",
                    "Your crews may be exposed to hazardous chemicals (fuels, cement, adhesives, coatings, solvents).",
                    "HazCom Coordinator", els, company=company, as_of=as_of)


def _resp(company, as_of) -> Program:
    fit = P("respirator-program-annual-fit-test", "fit_test_interval_months_max")
    els = [
        _el("Written worksite-specific program", "Required where respirators are necessary or required (silica, "
            "lead, asbestos and similar), run by a trained program administrator.",
            ["respirator-program-annual-fit-test"], ["Program administrator", "Respirator models used"]),
        _el("Fit testing", f"Fit-test tight-fitting respirators before first use, whenever a different facepiece "
            f"(size, style, model or make) is used, and at least every {fit} months.",
            ["respirator-program-annual-fit-test"], ["Fit-test provider", "Next fit-test date per employee"]),
        _el("Program procedures", "; ".join(UNVERIFIED["respirator_program_elements"]["elements"]) + ".",
            ["respirator-program-annual-fit-test"], ["Medical evaluation provider (PLHCP)"],
            unverified=["respirator_program_elements"]),
    ]
    return _program("respiratory", "Respiratory Protection Program (8 CCR 5144)", "required",
                    "You reported that employees use respirators.",
                    "Respiratory Protection Program Administrator (suitably trained)", els,
                    named=["Physician or other licensed health care professional (PLHCP) for medical evaluations"],
                    company=company, as_of=as_of)


def _silica(company, as_of) -> Program:
    al = P("silica-scope-action-level", "action_level_ug_m3_8hr_twa")
    pel = P("silica-table1-or-pel-50", "pel_ug_m3_8hr_twa")
    ms = core.rule("silica-medical-surveillance-30-days").parameters
    els = [
        _el("Scope", f"Covers all respirable crystalline silica exposure in construction unless exposure stays below "
            f"{al} ug/m3 (8-hour TWA) under any foreseeable conditions.", ["silica-scope-action-level"],
            ["Silica tasks (saw cutting, coring, grinding, jackhammering, tuckpointing)"]),
        _el("Controls per task", f"For Table 1 tasks fully implement the listed engineering controls, work practices "
            f"and respirators; otherwise assess exposure and keep it at or below the {pel} ug/m3 PEL.",
            ["silica-table1-or-pel-50", "silica-written-exposure-control-plan"],
            ["Table 1 line and controls for each task", "Respirator (APF) required by Table 1, if any"]),
        _el("Housekeeping", "No dry sweeping or brushing where it could add to exposure unless wet sweeping, HEPA "
            "vacuuming or other low-exposure methods are infeasible; no compressed air to clean clothing or surfaces "
            "unless a ventilation system captures the dust cloud or no alternative method is feasible.",
            ["silica-no-dry-sweeping-compressed-air"], ["Cleanup method"]),
        _el("Restricting access", "Procedures to limit other workers' access to areas with high exposure.",
            ["silica-written-exposure-control-plan"], ["Access control method"]),
        _el("Competent person", "Designate a competent person to make frequent and regular inspections of jobsites, "
            "materials and equipment to implement the plan.", ["silica-competent-person", "competent-person-definition"],
            ["Competent person name"]),
        _el("Medical surveillance", f"Offer free medical exams to employees required to wear a respirator for silica "
            f"{ms['respirator_days_per_year_gte']} or more days a year: baseline within {ms['baseline_within_days']} "
            f"days of assignment (unless one in the last {ms['baseline_lookback_years']} years), then at least every "
            f"{ms['periodic_interval_years']} years.", ["silica-medical-surveillance-30-days"],
            ["Clinic / PLHCP", "Employees at 30+ respirator days"]),
    ]
    return _program("silica", "Silica Exposure Control Plan (8 CCR 1532.3)", "required",
                    "You reported silica-generating tasks.", "Silica competent person", els,
                    named=["Competent person (8 CCR 1532.3(g)(4))", "PLHCP for medical exams"],
                    review=f"Review at least every {P('silica-written-exposure-control-plan', 'review_interval_months_max')}"
                           " months.", company=company, as_of=as_of)


def _first_aid_tier(n: int) -> str:
    for t in P("first-aid-kit-weatherproof", "crew_size_tiers"):
        if t.startswith("over"):
            if n > int(t.split()[1]):
                return t
        else:
            lo, hi = (int(x) for x in t.split("-"))
            if lo <= n <= hi:
                return t
    return "unknown"


def _emergency(company, site_n, stories, height_ft, depth_ft, as_of) -> Program:
    tier = _first_aid_tier(site_n)
    floors, ft = (P("emergency-call-system-5-floors-48-ft", "floors_gte"),
                  P("emergency-call-system-5-floors-48-ft", "height_or_depth_ft_gte"))
    big = (stories or 0) >= floors or (height_ft or 0) >= ft or (depth_ft or 0) >= ft
    gear = " and a ".join(x.replace("_", " ") for x in P("emergency-call-system-5-floors-48-ft", "required"))
    els = [
        _el("Written emergency medical plan", "Describe in writing how you meet the emergency medical service "
            "requirements; employers sharing a combined program may use one plan.", ["emergency-medical-written-plan"],
            ["Nearest clinic and hospital", "Transport method"]),
        _el("Trained first-aid persons", "Enough appropriately trained persons available to give first aid; on a "
            "multi-employer site you may pool with other employers if the pool covers the combined workforce.",
            ["first-aid-trained-persons"], ["First-aid/CPR trained people per crew"]),
        _el("First-aid kit", f"At least one kit in a weatherproof container, inspected and restocked. Crew of {site_n} "
            f"falls in the '{tier}' tier of the 8 CCR 1512 contents table. No drugs or medicines without written "
            "physician approval.", ["first-aid-kit-weatherproof"], ["Kit location", "Who restocks"]),
        _el("Emergency numbers", "Post physician (and alternate), hospital, ambulance and fire numbers near the job "
            "phone or otherwise make them available; tell all employees what to do if someone is hurt.",
            ["emergency-numbers-posting"], ["Physician", "Hospital", "Ambulance", "Fire"]),
        _el("Emergency communication and litter", (
            f"Your project is {stories or 0} floors / {height_ft or 0:g} ft high / {depth_ft or 0:g} ft deep: at "
            f"{floors} floors or {ft} ft above or below ground you need a {gear}. "
            + ("REQUIRED for this profile." if big else
                                                                            "Not triggered by this profile.")),
            ["emergency-call-system-5-floors-48-ft"], ["Communication system", "Litter location"] if big else []),
        _el("Emergency action procedures", UNVERIFIED["emergency_action_plan_3220"]["text"] + " Typical contents: "
            + "; ".join(UNVERIFIED["emergency_action_plan_3220"]["elements"]) + ".", [],
            ["Evacuation and muster point per site"], unverified=["emergency_action_plan_3220"]),
    ]
    return _program("emergency", "Emergency Medical Services and Emergency Action Plan (8 CCR 1512)", "required",
                    "Every construction employer must provide emergency medical services and describe them in a "
                    "written plan (8 CCR 1512(i)).", "Site first-aid / emergency coordinator", els,
                    named=["Appropriately trained first-aid person(s) at each site"], company=company, as_of=as_of)


def _fall(company, fall_ft, infeasible, as_of) -> Program:
    gen = P("fall-protection-7-5-ft", "fall_height_ft_gt")
    res_ft = P("residential-framing-fall-6-ft", "fall_height_ft_gte")
    roof_ft = P("residential-roofing-fall-6-ft", "fall_height_ft_gte")
    com = P("commercial-roofing-fall-20-ft", "roof_height_ft_gt")
    steep = P("residential-roofing-fall-6-ft", "slope_max_for_6ft_rule")
    triggers = (f"Conventional fall protection triggers: more than {gen} ft generally (8 CCR 1670); {res_ft} ft or more "
                f"for residential framing (1716.2) and {roof_ft} ft or more for residential roofing up to {steep} "
                f"slope, any height on steeper residential roofs (1731); more than {com} ft for commercial roofing "
                "(1730).")
    ids = ["fall-protection-7-5-ft", "residential-framing-fall-6-ft", "residential-roofing-fall-6-ft",
           "commercial-roofing-fall-20-ft"]
    exposure = f"Your highest fall exposure: {fall_ft:g} ft. " if fall_ft else ""
    if not infeasible:
        why = (exposure + triggers + " Use guardrails, nets or personal fall protection. A written fall protection "
               "plan is only allowed when you can show those are infeasible or create a greater hazard, so none is "
               "required now.")
        return _program("fall_protection_plan", "Fall Protection Plan (8 CCR 1671.1)", "not_required", why,
                        "Qualified person", [_el("Conventional fall protection", triggers, ids)], company=company,
                        as_of=as_of)
    fp = core.rule("fall-protection-plan").parameters
    els = [
        _el("When allowed", exposure + triggers + " Conventional protection is presumed feasible; you carry the burden "
            "of showing it is infeasible or a greater hazard.", ids + ["fall-protection-plan"],
            ["Why guardrails, nets and arrest are infeasible here"]),
        _el("Prepared by a qualified person", f"Site-specific, prepared by a {fp['prepared_by']}; changes approved by "
            f"a {fp['changes_approved_by']}; a current copy kept at the jobsite.", ["fall-protection-plan",
                                                                                  "qualified-person-definition"],
            ["Qualified person name", "Site address"]),
        _el("Alternative measures and controlled access zones", "Discuss alternative measures, controlled access zone "
            "locations, a safety monitoring system if no alternative works, and the employees designated to work in "
            "the zones.", ["fall-protection-plan"], ["CAZ locations", "Designated employees", "Safety monitor"]),
        _el("Implemented under a competent person", f"Implemented under the supervision of a {fp['supervised_by']}.",
            ["fall-protection-plan", "competent-person-definition"], ["Competent person name"]),
        _el("Review after incidents", "Investigate and update the plan after any fall, serious incident or near miss.",
            ["fall-protection-plan"], ["Incident review procedure"]),
    ]
    return _program("fall_protection_plan", "Fall Protection Plan (8 CCR 1671.1)", "required",
                    "You reported that conventional fall protection is infeasible or a greater hazard for some work.",
                    "Qualified person (prepares) and competent person (supervises)", els,
                    named=["Qualified person who prepares and approves the plan",
                           "Competent person who supervises it"], company=company, as_of=as_of)


def _excavation(company, depth, enter, as_of) -> Program:
    ps = P("excavation-protective-system-5-ft", "depth_ft_gte_requires_protective_system")
    eg = core.rule("excavation-egress-4-ft-25-ft").parameters
    usa = core.rule("usa-811-notice-2-working-days").parameters
    els = [
        _el("Call 811 before digging", f"Contact the regional notification center (811) at least "
            f"{usa['min_working_days_before']} working days (not counting the day of the call) and no more than "
            f"{usa['max_calendar_days_before']} calendar days before excavating.",
            ["usa-811-notice-2-working-days", "excavation-usa-notice-2-working-days"], ["Ticket number per job"]),
        _el("Protective system", f"Planned depth {depth:g} ft: at {ps} ft or deeper every employee in the excavation "
            "needs sloping, benching, shoring or shielding unless entirely in stable rock; under that depth a "
            "competent person must find no sign of potential cave-in. " + (
                "REQUIRED at this depth." if depth >= ps else "Competent person exam required at this depth."),
            ["excavation-protective-system-5-ft"], ["Protective system / tabulated data on site"]),
        _el("Egress", f"Trenches {eg['trench_depth_ft_gte']} ft or deeper need a ladder, stairway or ramp within "
            f"{eg['max_lateral_travel_ft']} ft of lateral travel; test the atmosphere before entry over "
            f"{eg['atmosphere_test_depth_ft_gt']} ft where hazardous atmospheres could exist.",
            ["excavation-egress-4-ft-25-ft"], ["Ladder spacing"]),
        _el("Spoil setback", f"Keep spoil and equipment at least {P('excavation-spoil-2-ft', 'min_setback_ft')} ft from "
            "the edge or use retaining devices.", ["excavation-spoil-2-ft"], []),
        _el("Daily competent-person inspection", "A competent person inspects the excavation, adjacent areas and "
            "protective systems daily before work, as needed during the shift, and after every rainstorm or other "
            "hazard-increasing event; remove workers if hazards are found.",
            ["excavation-daily-competent-person-inspection", "competent-person-definition"],
            ["Competent person name", "Daily inspection log"]),
    ]
    named = ["Competent person for daily excavation inspections"]
    over = UNVERIFIED["excavation_over_20ft_engineer"]
    if depth > over["depth_ft_gt"]:
        els.append(_el(f"Deeper than {over['depth_ft_gt']} ft: engineered protective system",
                       f"Planned depth {depth:g} ft > {over['depth_ft_gt']} ft. " + over["text"],
                       ["excavation-protective-system-5-ft"],
                       ["Registered PE name and stamp, or the manufacturer's tabulated data covering this depth"],
                       unverified=["excavation_over_20ft_engineer"]))
        named.append(f"Registered professional engineer (sloping/benching deeper than {over['depth_ft_gt']} ft; or "
                     "approves the tabulated data or design used for shoring/shields)")
    return _program("excavation", "Excavation and Trenching Procedures (8 CCR 1541-1541.1)", "required",
                    f"You reported excavations up to {depth:g} ft" + (" that workers enter." if enter else "."),
                    "Excavation competent person", els, named=named, company=company, as_of=as_of)


def _crane(company, as_of) -> Program:
    cert = core.rule("crane-annual-certification").parameters
    op = core.rule("crane-operator-certification-5-years").parameters
    els = [
        _el("Crane certification", f"Cranes and derricks used in lifting service over "
            f"{cert['rated_capacity_tons_gt']} tons rated capacity may not be used without current annual (every "
            f"{cert['certification_interval_years']} year) and quadrennial certificates from a Cal/OSHA-licensed "
            "certifying agency.", ["crane-annual-certification"], ["Crane ID and certificate expiry"]),
        _el("Operator certification", f"Operators trained, certified by an accredited entity (valid up to "
            f"{op['certificate_validity_years_max']} years) or government-licensed, and evaluated before operating; "
            "the employer pays for certification.", ["crane-operator-certification-5-years"],
            ["Operator names and certificate expiry"]),
        _el("Tower cranes", "Erecting, climbing or dismantling a tower crane needs a Cal/OSHA project permit and "
            "24-hour notices (run check_cal_osha_permits).", ["permit-tower-crane"], []),
    ]
    return _program("cranes", "Crane and Rigging Procedures", "required", "You reported crane use.",
                    "Lift supervisor", els, named=["Certified crane operator", "Cal/OSHA-licensed crane certifier"],
                    company=company, as_of=as_of)


def _lead_asbestos(company, as_of) -> Program:
    asb = core.rule("asbestos-dosh-registration-100sqft").parameters
    lead = core.rule("lead-pre-job-notification-24h").parameters
    els = [
        _el("Lead compliance program", f"Before the job starts, establish a written lead compliance program and revise "
            f"it at least every {P('lead-written-compliance-program', 'update_interval_months_max')} months; keep it "
            f"at the worksite. Lead PEL {P('lead-pel-and-action-level-2025', 'pel_ug_m3_8hr_twa')} ug/m3, action "
            f"level {P('lead-pel-and-action-level-2025', 'action_level_ug_m3_8hr_twa')} ug/m3.",
            ["lead-written-compliance-program", "lead-pel-and-action-level-2025"], ["Lead tasks and controls"]),
        _el("Lead notice to Cal/OSHA", f"When planned work includes a lead trigger task listed in 8 CCR 1532.1(d)(2), "
            f"the nearest district office must receive written notice at least {lead['advance_notice_hours']} hours "
            f"before work starts (not needed under {lead['exempt_if_area_sqft_or_linear_ft_lt']} sq ft / linear ft "
            "disturbed, among other exemptions).", ["lead-pre-job-notification-24h"], []),
        _el("Asbestos registration and notification", f"Asbestos work on {asb['surface_area_sqft_gte']} sq ft or more "
            "of asbestos-containing construction material at one site needs a Cal/OSHA registration before work "
            "starts and a written notice to the district office before each job.",
            ["asbestos-dosh-registration-100sqft", "asbestos-job-notification"], ["Registration number"]),
        _el("Asbestos license", f"Asbestos work of {asb['surface_area_sqft_gte']} sq ft or more must be done by a "
            "C-22 contractor or one whose qualifier passed the CSLB asbestos certification exam. B&P 7058.5 waives the "
            "exam only for installation, maintenance and repair of "
            + ", ".join(P("asbestos-cslb-certification-100sqft", "exempt_materials")) + ".",
            ["asbestos-cslb-certification-100sqft"], ["C-22 / certified contractor"]),
        _el("Asbestos competent person", "Designate a competent person on every asbestos worksite; for Class I work "
            f"they inspect at least {P('asbestos-competent-person-inspections', 'class_i_inspection_min')}.",
            ["asbestos-competent-person-inspections"], ["Name"]),
    ]
    return _program("lead_asbestos", "Lead and Asbestos Work Procedures (8 CCR 1532.1, 1529, 341.6)", "conditional",
                    "You reported lead or asbestos work; which items apply depends on testing results and quantities.",
                    "Lead/asbestos competent person", els,
                    named=["CSLB C-22 or asbestos-certified contractor (100 sq ft+)", "Asbestos competent person"],
                    company=company, as_of=as_of)


# ================================================================ recordkeeping
def _log300(peak: int, naics: Optional[str], as_of: date) -> tuple[bool, list[Line]]:
    ex = core.rule("log300-size-exemption-10-or-fewer").parameters
    lim = ex["exempt_if_peak_employees_lte"]
    required = peak > lim
    construction = not naics or str(naics).startswith("23")
    lines = [_line("Cal/OSHA Log 300 / 301 / 300A",
                   f"Peak employment last calendar year {peak}; records are required above {lim} (company-wide "
                   f"peak). Construction (NAICS 23) is not a partially exempt industry. "
                   + ("REQUIRED." if required else "Size-exempt, but you must still report serious injuries "
                                                   "and deaths to Cal/OSHA. "
                                                   + UNVERIFIED["log300_survey_and_5020"]["text"]),
                   ["log300-size-exemption-10-or-fewer"],
                   unverified=[] if required else ["log300_survey_and_5020"])]
    if not required:
        return False, lines
    post = core.rule("log300a-posting-feb1-apr30").parameters
    start, end = _md(post["post_by"], as_of.year), _md(post["keep_posted_until"], as_of.year)
    if as_of > end:
        start, end = _md(post["post_by"], as_of.year + 1), _md(post["keep_posted_until"], as_of.year + 1)
    ita = core.rule("osha-ita-electronic-submission-march-2").parameters
    sub = _md(ita["submit_by"], as_of.year)
    if as_of > sub:
        sub = _md(ita["submit_by"], as_of.year + 1)
    lo, hi = ita["form_300a_construction_range"]
    big = ita["form_300a_if_employees_gte"]
    lines += [
        _line("Record each case", f"Enter each recordable case on Form 300 and complete Form 301 within "
              f"{P('log300-record-within-7-days', 'deadline_calendar_days')} calendar days of learning of it.",
              ["log300-record-within-7-days"]),
        _line("Short-term jobsites", "Jobsites expected to last under a year may share one Form 300; link every "
              "employee to an establishment.", ["log300-short-term-jobsites"]),
        _line("Annual summary 300A", "Total the year on Form 300A (zeros if no cases) with average employees and "
              "hours worked, certified by a company executive (owner, corporate officer or highest-ranking official).",
              ["log300a-annual-summary-certification"]),
        _line("Post 300A", f"Post from {start.isoformat()} through {end.isoformat()} where notices are posted; hand "
              "or mail a copy to employees paid in that period who do not report weekly to the posting location.",
              ["log300a-posting-feb1-apr30", "log300a-mail-to-field-employees"], due=start.isoformat()),
        _line("Retention", f"Keep Forms 300, 301 and 300A {P('log300-retention-5-years', 'retention_years')} years "
              "after the end of the year they cover.", ["log300-retention-5-years"]),
        _line("Access requests", f"Employees: copies by end of next business day. Cal/OSHA: within "
              f"{P('log300-government-request-4-hours', 'deadline_business_hours')} business hours.",
              ["log300-employee-access-next-business-day", "log300-government-request-4-hours"]),
    ]
    if construction and (lo <= peak <= hi or peak >= big):
        extra = ""
        if peak >= ita["form_300_301_if_employees_gte"] and naics and any(
                str(naics).startswith(c) for c in ita["form_300_301_construction_naics_in_appendix_i"]):
            extra = " Your NAICS is in Appendix I with 100+ employees, so also submit Form 300 and 301 data."
        lines.append(_line("Electronic submission (OSHA ITA)", f"Peak {peak} is in the {lo}-{hi} construction range "
                           f"or {big}+: submit Form 300A data electronically by {sub.isoformat()} (OSHA counts the "
                           f"establishment's peak in the year covered; last year's peak is the estimate).{extra}", ["osha-ita-electronic-submission-march-2"], due=sub.isoformat()))
    elif construction:
        lines.append(_line("Electronic submission (OSHA ITA)", f"Peak {peak} is under {lo}: no electronic submission "
                           "for construction establishments.", ["osha-ita-electronic-submission-march-2"]))
    return True, lines


# ================================================================ generate_safety_programs
def generate_safety_programs(company_name: str, employee_count: int, worksites_have_public_access: bool,
                             outdoor_work: bool = True, heat_exposure: bool = False,
                             excavation_max_depth_ft: float = 0, workers_enter_trench: bool = False,
                             building_stories: int = 0, building_height_ft: Optional[float] = None,
                             demolition: bool = False, scaffold_or_falsework_height_stories: float = 0,
                             silica_tasks: bool = False, crane_use: bool = False, asbestos_or_lead: bool = False,
                             respirators_used: bool = False, as_of=None, *,
                             max_employees_at_one_site: Optional[int] = None,
                             peak_employees_last_year: Optional[int] = None, hazardous_chemicals: bool = True,
                             max_fall_height_ft: Optional[float] = None,
                             conventional_fall_protection_infeasible: bool = False, naics_code: Optional[str] = None,
                             majority_language: Optional[str] = None, shift_hours: float = 8,
                             last_toolbox_meeting: Optional[str] = None) -> SafetyProgramsResult:
    if employee_count < 1:
        raise ValueError("employee_count must be at least 1")
    today = _date(as_of, "as_of") if as_of else logic.today_ca()
    site_n = max_employees_at_one_site or employee_count
    peak = employee_count if peak_employees_last_year is None else peak_employees_last_year
    hazards = [h for h, on in [("fall protection", (max_fall_height_ft or 0) > 0 or building_stories >= 2),
                               ("trenching and excavation", excavation_max_depth_ft > 0),
                               ("scaffolds", scaffold_or_falsework_height_stories > 0), ("demolition", demolition),
                               ("silica dust", silica_tasks), ("cranes and rigging", crane_use),
                               ("lead/asbestos", asbestos_or_lead), ("heat", outdoor_work or heat_exposure)] if on]
    c = company_name
    progs = [_iipp(c, employee_count, today), _csp(c, today, hazards)]
    if outdoor_work:
        progs.append(_heat_outdoor(c, site_n, shift_hours, majority_language, today))
    if heat_exposure:
        progs.append(_heat_indoor(c, today))
    progs.append(_wvpp(c, site_n, worksites_have_public_access, today))
    if hazardous_chemicals:
        progs.append(_hazcom(c, today))
    if respirators_used:
        progs.append(_resp(c, today))
    if silica_tasks:
        progs.append(_silica(c, today))
    progs.append(_emergency(c, site_n, building_stories, building_height_ft, excavation_max_depth_ft, today))
    progs.append(_fall(c, max_fall_height_ft, conventional_fall_protection_infeasible, today))
    if excavation_max_depth_ft > 0:
        progs.append(_excavation(c, excavation_max_depth_ft, workers_enter_trench, today))
    if crane_use:
        progs.append(_crane(c, today))
    if asbestos_or_lead:
        progs.append(_lead_asbestos(c, today))

    # ---- training schedule
    wd = P("toolbox-tailgate-meetings-10-working-days", "max_interval_working_days")
    tb_due = None
    if last_toolbox_meeting:
        tb_due = _add_working_days(_date(last_toolbox_meeting, "last_toolbox_meeting"), wd).isoformat()
    training = [
        _line("New-hire safety orientation", "At hire: hazards and precautions of the work, read the Code of Safe "
              "Practices; also on new assignments and new hazards.", ["new-worker-safety-instruction",
                                                                       "iipp-training-triggers"]),
        _line("Toolbox / tailgate meeting", f"Every crew, at least every {wd} working days, led by its supervisor."
              + (f" Last meeting {last_toolbox_meeting} -> next due by {tb_due} ({wd} working days later)"
                 + (f"; OVERDUE as of {today.isoformat()}, hold one now. " if tb_due < today.isoformat() else ". ")
                 + WORKING_DAY_NOTE if tb_due else ""), ["toolbox-tailgate-meetings-10-working-days"], due=tb_due),
        _line("Supervisor safety meeting", "Periodic, led by management (no fixed interval).",
              ["supervisor-safety-meetings"]),
    ]
    if outdoor_work:
        training += [
            _line("Heat illness training", "Before outdoor work with heat exposure starts, for every employee and "
                  "supervisor.", ["heat-training-before-exposure"]),
            _line("High-heat pre-shift meeting", f"Before work on any day at "
                  f"{P('heat-high-heat-procedures-95f', 'temperature_f_gte')}F or more.",
                  ["heat-pre-shift-meeting-high-heat", "heat-high-heat-procedures-95f"]),
        ]
    wv = next(p for p in progs if p.key == "wvpp")
    if wv.status == "required":
        training.append(_line("Workplace violence training", f"When the plan starts, then at least every "
                              f"{P('wvpp-training-initial-and-annual', 'training_interval_months_max')} months.",
                              ["wvpp-training-initial-and-annual"]))
    if hazardous_chemicals:
        training.append(_line("HazCom training", UNVERIFIED["hazcom_training_timing"]["text"],
                              ["hazcom-written-program"], unverified=["hazcom_training_timing"]))
    if respirators_used:
        training.append(_line("Respirator fit test", f"Before first use, on any facepiece change, and at least every "
                              f"{P('respirator-program-annual-fit-test', 'fit_test_interval_months_max')} months.",
                              ["respirator-program-annual-fit-test"]))
    if silica_tasks:
        ms = core.rule("silica-medical-surveillance-30-days").parameters
        training.append(_line("Silica medical exams", f"Workers in a respirator {ms['respirator_days_per_year_gte']}+ "
                              f"days/yr: baseline within {ms['baseline_within_days']} days, then every "
                              f"{ms['periodic_interval_years']} years.", ["silica-medical-surveillance-30-days"]))

    # ---- recordkeeping
    log_req, records = _log300(peak, naics_code, today)
    records += [
        _line("IIPP inspection and training records", f"Keep at least "
              f"{P('iipp-inspection-records-1-year', 'retention_years_min')} year.",
              ["iipp-inspection-records-1-year", "iipp-training-records-1-year"]),
        _line("Toolbox meeting sign-ins", "Date, supervisor, topic, attendees (supports IIPP training records).",
              ["toolbox-tailgate-meetings-10-working-days"]),
    ]
    if wv.status == "required":
        rec = core.rule("wvpp-records-retention").parameters
        records.append(_line("WVPP records", f"Incident log, hazard and investigation records "
                             f"{rec['violent_incident_log_years_min']} years; training "
                             f"{rec['training_records_years_min']} year.", ["wvpp-records-retention"]))

    # ---- postings
    postings = [
        _line("Cal/OSHA 'Safety and Health Protection on the Job'", "Post at each location employees report to each "
              f"day. Missing it is a regulatory violation (up to "
              f"${P('calosha-poster-posting', 'penalty_max_usd_per_violation'):,}).", ["calosha-poster-posting"]),
        _line("Emergency phone numbers", "Near the job phone at each site.", ["emergency-numbers-posting"]),
        _line("Code of Safe Practices", "Each jobsite office, or a copy with each supervisor.",
              ["code-of-safe-practices-posting"]),
        _line("Workers' comp Notice to Employees", "Where employees frequent, showing your carrier; also in Spanish "
              "if you have Spanish-speaking employees.", ["workers-comp-notice-to-employees-poster"]),
        _line("Other state and federal employment posters", "Wage Order 16, minimum wage, sick leave, payday, "
              "discrimination, EDD and more (see the DIR list).", ["required-workplace-postings"]),
    ]
    if hazardous_chemicals:
        postings.append(_line("Access to medical and exposure records", "Employers using hazardous or toxic "
                              "substances (8 CCR 3204 notice).", ["required-workplace-postings"]))
    if log_req:
        postings.append(next(r for r in records if r.item == "Post 300A"))
    if outdoor_work:
        postings.append(_line("Heat Illness Prevention Plan", "Keep available at each worksite (not posted).",
                              ["heat-written-plan-outdoor"]))

    # ---- permits screen (assumes the company is the project administrator)
    pr = check_cal_osha_permits(excavation_max_depth_ft, workers_enter_trench, building_stories, building_height_ft,
                                demolition=demolition,
                                demolition_height_ft=building_height_ft if demolition else None,
                                demolition_stories=building_stories if demolition else None,
                                scaffold_or_falsework_height_stories=scaffold_or_falsework_height_stories,
                                as_of=today)
    permits = [f"{i.activity}: {i.permit_type}" for i in pr.items if i.required and i.kind == "permit"]

    res = SafetyProgramsResult(as_of=today.isoformat(), company_name=c, employee_count=employee_count,
                               required_programs=[p.name for p in progs if p.status == "required"], programs=progs,
                               training=training, recordkeeping=records, log300_required=log_req, postings=postings,
                               permits_to_check=permits)
    if permits:
        res.notes.append("Cal/OSHA permits likely needed (assumes you are the GC/project administrator; subs need "
                         "annual permits instead). Run check_cal_osha_permits per project for fees and dates.")
    if asbestos_or_lead or silica_tasks:
        if not respirators_used:
            res.notes.append("If any silica, lead or asbestos task requires respirators, a written Respiratory "
                             "Protection Program (8 CCR 5144) becomes required.")
    if wv.status == "exempt":
        res.notes.append("Even with the WVPP small-site exemption, Labor Code 3553 still applies: if an employee is the "
                         "victim of a crime at work, give written notice of workers' comp eligibility within "
                         f"{P('wvpp-workers-comp-notice-one-working-day', 'workers_comp_notice_working_days')} "
                         "working day.")
        res.cite("wvpp-workers-comp-notice-one-working-day", "workers-comp-workplace-crime-notice")
    res.notes.append("Have a qualified safety professional review the finished programs; they must match how your "
                     "crews actually work.")
    res.cite("penalty-no-iipp-no-reduction")
    return _finish(res, [*progs, *training, *records, *postings, *pr.items])


# ================================================================ check_cal_osha_permits
class PermitItem(BaseModel):
    activity: str
    kind: Literal["permit", "registration", "notification"]
    required: bool
    permit_type: str = ""
    holder: str = ""
    fee_usd: float = 0
    why: str
    lead_time: str = ""
    deadline: Optional[str] = None
    deadline_passed: bool = False
    named_people: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    verified: bool = False


class PermitsResult(core.Result):
    as_of: str
    planned_start: Optional[str] = None
    items: list[PermitItem]
    permits_required: list[str]
    total_fees_usd: float
    fee_math: str
    penalties: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


Exemption = Literal["none", "government_agency", "puc_regulated_utility", "emergency_underground_repair", "grave",
                    "swimming_pool_excavation"]
Role = Literal["project_administrator", "subcontractor"]


def _item(activity, kind, required, why, rule_ids, unverified=(), **kw) -> PermitItem:
    return PermitItem(activity=activity, kind=kind, required=required, why=why, rule_ids=list(rule_ids),
                      unverified=list(unverified), verified=_ok(rule_ids, unverified), **kw)


def _tall(height_ft, stories, gt_ft) -> tuple[bool, str, list[str]]:
    lc = UNVERIFIED["lc6500_three_stories"]
    parts, unv, hit = [], [], False
    if height_ft is not None:
        hit = height_ft > gt_ft
        parts.append(f"{height_ft:g} ft {'>' if hit else '<='} {gt_ft} ft")
    if stories and stories > lc["stories_gt"]:
        hit, unv = True, ["lc6500_three_stories"]
        parts.append(f"{stories:g} stories > {lc['stories_gt']} (Labor Code 6500 'or the equivalent height')")
    elif stories:
        parts.append(f"{stories:g} stories (not more than {lc['stories_gt']})")
    return hit, "; ".join(parts) or "no height given", unv


def check_cal_osha_permits(excavation_max_depth_ft: float = 0, workers_enter_trench: bool = False,
                           building_stories: int = 0, building_height_ft: Optional[float] = None, *,
                           demolition: bool = False, demolition_height_ft: Optional[float] = None,
                           demolition_stories: Optional[int] = None,
                           scaffold_or_falsework_height_ft: Optional[float] = None,
                           scaffold_or_falsework_height_stories: float = 0, tower_crane: bool = False,
                           role: Role = "project_administrator", self_performs_structural_work: bool = True,
                           has_annual_permit: bool = False, exemption: Exemption = "none",
                           asbestos_sqft: float = 0, has_asbestos_registration: bool = False,
                           lead_disturbed_sqft: float = 0, planned_start: Optional[str] = None,
                           as_of=None) -> PermitsResult:
    today = _date(as_of, "as_of") if as_of else logic.today_ca()
    start, start_date_only = _dt(planned_start, "planned_start") if planned_start else (None, True)
    fees = core.rule("permit-fees").parameters
    issue_wd = P("permit-project-issuance-5-working-days", "issuance_working_days")
    notice_h = P("permit-annual-24h-new-site-notice", "advance_notice_hours")
    items: list[PermitItem] = []
    all_exempt = exemption in ("government_agency", "puc_regulated_utility")
    project_fee = {"Trench / excavation": fees["excavation_trench_usd"],
                   "Scaffold / falsework / vertical shoring":
                       fees["construction_building_structure_scaffold_falsework_usd"]}

    # Cal/OSHA has up to issue_wd working days after complete materials to issue, so the latest safe date leaves all
    # of them AND has the permit in hand by the working day before work starts (issue_wd + 1 working days back).
    project_lead = (f"Cal/OSHA issues within {issue_wd} working days of complete materials (or the safety "
                    f"conference); latest date = {issue_wd} + 1 working days before start so the permit is in hand "
                    "the working day before. Apply earlier")

    def project_deadline():
        return _add_working_days(start.date(), -(issue_wd + 1)).isoformat() if start else None

    def annual_notice():
        return _fmt(start - timedelta(hours=notice_h)) if start else None

    def annual_or_project(activity, why, ids, unv=()):
        if has_annual_permit:
            return _item(activity, "permit", True, why + f" You hold an annual permit: notify the district office "
                         f"with jurisdiction over the site at least {notice_h} hours before the work starts.",
                         ids + ["permit-annual-vs-project", "permit-annual-24h-new-site-notice"], unv,
                         permit_type="annual (held)", holder="your company",
                         lead_time=f"{notice_h} hours' notice", deadline=annual_notice())
        return _item(activity, "permit", True, why + f" No annual permit: get a project permit "
                     f"(${project_fee[activity]:g}). If you do this work on more than one job a year, an annual "
                     f"permit (${fees['annual_permit_usd']:g}) covers any site with {notice_h}-hour notice.",
                     ids + ["permit-annual-vs-project", "permit-project-issuance-5-working-days"], unv,
                     permit_type="project", holder="your company", fee_usd=project_fee[activity],
                     lead_time=project_lead, deadline=project_deadline())

    # ---- trench / excavation
    if excavation_max_depth_ft > 0:
        d_ft = P("permit-trench-excavation-5ft", "depth_ft_gte")
        deep = excavation_max_depth_ft >= d_ft
        trench_exempt = exemption in ("emergency_underground_repair", "grave", "swimming_pool_excavation")
        why = (f"Depth {excavation_max_depth_ft:g} ft {'>=' if deep else '<'} {d_ft} ft; workers "
               f"{'do' if workers_enter_trench else 'do not'} descend into it ('descend' = enter any part once it "
               f"is {d_ft} ft deep).")
        ids = ["permit-trench-excavation-5ft"]
        if deep and workers_enter_trench and not (all_exempt or trench_exempt):
            items.append(annual_or_project("Trench / excavation", why, ids))
        else:
            reason = (f" Exempt from the permit ({exemption.replace('_', ' ')}); excavation safety rules still apply."
                      if (all_exempt or trench_exempt) and deep and workers_enter_trench else " No permit needed.")
            items.append(_item("Trench / excavation", "permit", False, why + reason, ids + ["permit-exceptions"]))
        usa = core.rule("usa-811-notice-2-working-days").parameters
        dl, window = None, ""
        if start:
            # The call day does not count, so leave two full working days between the call and the dig day.
            last = _add_working_days(start.date(), -(usa["min_working_days_before"] + 1))
            first = start.date() - timedelta(days=usa["max_calendar_days_before"])
            dl, window = last.isoformat(), f" Call between {first.isoformat()} and {last.isoformat()}."
        items.append(_item("811 underground utility locate", "notification", True,
                           f"Call 811 at least {usa['min_working_days_before']} working days (the call day does not "
                           f"count) and no more than {usa['max_calendar_days_before']} calendar days before digging; "
                           f"the ticket is valid {P('web_permits-building:usa-ticket-valid-28-days', 'ticket_validity_calendar_days')}"
                           f" calendar days.{window} {WORKING_DAY_NOTE}",
                           ["usa-811-notice-2-working-days", "excavation-usa-notice-2-working-days",
                            "web_permits-building:usa-ticket-valid-28-days"], holder="excavator",
                           lead_time=f"{usa['min_working_days_before']} working days", deadline=dl))

    # ---- structures over 36 ft
    if not demolition or demolition_height_ft is not None or demolition_stories is not None:
        gt = P("permit-structure-36ft-erection", "height_ft_gt")
        hit, how, unv = _tall(building_height_ft, building_stories, gt)
        ids = ["permit-structure-36ft-erection"]
        if (building_height_ft is not None or building_stories) and hit and not all_exempt:
            if role == "project_administrator":
                items.append(_item("Building / structure over 36 ft", "permit", True,
                                   f"{how}. The project administrator (GC, prime, owner-builder or CM) must hold a "
                                   f"project permit.", ids + ["permit-project-issuance-5-working-days"], unv,
                                   permit_type="project", holder="project administrator",
                                   fee_usd=fees["construction_building_structure_scaffold_falsework_usd"],
                                   lead_time=project_lead,
                                   deadline=project_deadline()))
            elif self_performs_structural_work:
                items.append(_item("Building / structure over 36 ft", "permit", True,
                                   f"{how}. Employers doing steel or structural member erection, decking, curtain "
                                   "walls/precast/fascia, concrete on steel, or structural/roof framing need an annual "
                                   f"permit (rebar-only and interior partitions are not covered). Notify the district "
                                   f"office {notice_h} hours before starting.",
                                   ids + ["permit-annual-24h-new-site-notice"], unv,
                                   permit_type="annual" + (" (held)" if has_annual_permit else ""), holder="your company",
                                   fee_usd=0 if has_annual_permit else fees["annual_permit_usd"],
                                   lead_time=f"{notice_h} hours' notice", deadline=annual_notice()))
            else:
                items.append(_item("Building / structure over 36 ft", "permit", False, f"{how}. You do not do the "
                                   "covered structural activities, so you need no permit (the GC holds the project "
                                   "permit).", ids, unv))
        elif building_height_ft is not None or building_stories:
            items.append(_item("Building / structure over 36 ft", "permit", False,
                               f"{how}." + (" Exempt employer." if all_exempt and hit else " No permit needed."),
                               ids + (["permit-exceptions"] if all_exempt else []), unv))

    # ---- scaffold / falsework over 36 ft
    if scaffold_or_falsework_height_ft is not None or scaffold_or_falsework_height_stories:
        gt = P("permit-scaffold-falsework-36ft", "height_ft_gt")
        hit, how, unv = _tall(scaffold_or_falsework_height_ft, scaffold_or_falsework_height_stories, gt)
        ce = core.rule("falsework-engineer-design").parameters
        eng = (f" Falsework or vertical shoring over {ce['engineer_required_if_height_ft_gt']} ft high (top of sills to "
               f"soffit), with spans over {ce['engineer_required_if_span_ft_gt']} ft, or with vehicle or rail traffic "
               "through it needs design drawings signed by a California-registered civil engineer; scaffold "
               "erection is directed by a qualified person.")
        tall_for_ce = hit or (scaffold_or_falsework_height_ft or 0) > ce["engineer_required_if_height_ft_gt"]
        ids = ["permit-scaffold-falsework-36ft"] + (["falsework-engineer-design", "scaffold-qualified-person-erection"]
                                                    if tall_for_ce else [])
        named = ["California-registered civil engineer (falsework/shoring design)",
                 "Qualified person directing scaffold erection"] if tall_for_ce else []
        if hit and not all_exempt:
            it = annual_or_project("Scaffold / falsework / vertical shoring", f"Intended height when complete: {how}.",
                                   ids, unv)
            it.why += eng
            it.named_people = named
            items.append(it)
        else:
            items.append(_item("Scaffold / falsework / vertical shoring", "permit", False,
                               f"{how}." + (" Exempt employer (8 CCR 341(e))." if hit else " No permit needed.")
                               + (eng if tall_for_ce else ""), ids + (["permit-exceptions"] if hit else []), unv,
                               named_people=named))

    # ---- demolition
    if demolition:
        gt = P("permit-demolition-36ft", "height_ft_gt")
        own = demolition_height_ft is not None or demolition_stories is not None
        h = demolition_height_ft if own else building_height_ft
        s = (demolition_stories or 0) if own else building_stories
        hit, how, unv = _tall(h, s, gt)
        ids = ["permit-demolition-36ft"]
        if hit and not all_exempt:
            if role == "project_administrator":
                items.append(_item("Demolition over 36 ft", "permit", True, f"{how}. The project administrator holds a "
                                   "project permit; every other employer doing demolition holds an annual permit.",
                                   ids + ["permit-project-issuance-5-working-days"], unv, permit_type="project",
                                   holder="project administrator", fee_usd=fees["demolition_usd"],
                                   lead_time=project_lead,
                                   deadline=project_deadline()))
            else:
                items.append(_item("Demolition over 36 ft", "permit", True, f"{how}. Demolition subcontractors need "
                                   f"an annual permit; notify the district office {notice_h} hours before starting.",
                                   ids + ["permit-annual-24h-new-site-notice"], unv,
                                   permit_type="annual" + (" (held)" if has_annual_permit else ""),
                                   holder="your company", fee_usd=0 if has_annual_permit else fees["annual_permit_usd"],
                                   lead_time=f"{notice_h} hours' notice", deadline=annual_notice()))
        else:
            items.append(_item("Demolition over 36 ft", "permit", False,
                               f"{how}." + (" Exempt employer (8 CCR 341(e))." if hit else
                                            " No Cal/OSHA demolition permit."),
                               ids + (["permit-exceptions"] if hit else []), unv))
        ne = core.rule("neshap-notification-10-working-days").parameters
        units = P("neshap-facility-excludes-4-or-fewer-units", "excluded_residential_max_units")
        dl = _add_working_days(start.date(), -ne["lead_time_working_days"]).isoformat() if start else None
        items.append(_item("Air district / NESHAP demolition notice", "notification", True,
                           f"Written notice postmarked or delivered at least {ne['lead_time_working_days']} working "
                           f"days (Mon-Fri, holidays count) before demolition begins, even with little or no "
                           f"asbestos. The federal rule excludes residential buildings with {units} or fewer dwelling "
                           "units, but California air districts can still require notice (Bay Area AQMD requires it for "
                           "every demolition; South Coast AQMD even has a form for owner-occupants demolishing their "
                           "own house), so check your district before relying on that. Have the building inspected "
                           "for asbestos before demolition. Your city will not issue the demolition permit without a "
                           "copy of the notice (or your declaration that none is needed).",
                           ["neshap-notification-10-working-days", "air-district-demolition-notice-10-working-days",
                            "neshap-facility-excludes-4-or-fewer-units", "neshap-asbestos-inspection-before-demo",
                            "demolition-permit-requires-air-district-notice"], holder="owner or operator",
                           lead_time=f"{ne['lead_time_working_days']} working days", deadline=dl))

    # ---- tower crane
    if tower_crane and all_exempt:
        items.append(_item("Tower crane erection / climbing / dismantling", "permit", False,
                           "Exempt employer under 8 CCR 341(e).", ["permit-tower-crane", "permit-exceptions"]))
    elif tower_crane:
        tc = core.rule("permit-tower-crane").parameters
        items.append(_item("Tower crane erection / climbing / dismantling", "permit", True,
                           f"Project permit held by the crane erector; a crane relocated on the same project needs a "
                           f"new permit. Cal/OSHA must receive notice at least {tc['advance_notice_hours']} hours "
                           f"before each of: {', '.join(e.replace('_', ' ') for e in tc['notice_events'])}.",
                           ["permit-tower-crane", "permit-fees"], permit_type="project",
                           holder=tc["holder"], fee_usd=fees["tower_crane_usd"],
                           lead_time=f"{tc['advance_notice_hours']} hours before each event",
                           deadline=_fmt(start - timedelta(hours=tc["advance_notice_hours"])) if start else None,
                           named_people=["Division-licensed tower crane certifier or manufacturer's representative "
                                         "present during erection, climbing and dismantling"]))

    # ---- asbestos registration
    if asbestos_sqft > 0:
        a = core.rule("asbestos-dosh-registration-100sqft").parameters
        need = asbestos_sqft >= a["surface_area_sqft_gte"]
        pt = core.rule("asbestos-registration-processing-time").parameters
        lead_bd = pt["completeness_notice_business_days"] + pt["decision_business_days"]
        if need:
            reg_needed = not has_asbestos_registration
            items.append(_item("Asbestos registration (Cal/OSHA)", "registration", True,
                               f"{asbestos_sqft:g} sq ft >= {a['surface_area_sqft_gte']} sq ft of material over "
                               f"{a['acm_definition_pct_asbestos_by_weight_gt']}% asbestos (sum all locations at one "
                               f"site). Registration is valid {a['validity_years']} year. "
                               + (f"Apply early: Cal/OSHA has {pt['completeness_notice_business_days']} business days to "
                                  f"confirm completeness and {pt['decision_business_days']} more to decide = {lead_bd} "
                                  "business days." if reg_needed else "You hold a registration: renew it at least "
                                  f"{P('asbestos-registration-renewal-30-days', 'renew_days_before_expiration')} days "
                                  "before it expires.") + " Post a copy beside the Cal/OSHA poster and give copies to "
                               "the prime contractor and the other employers on the site.",
                               ["asbestos-dosh-registration-100sqft", "asbestos-registration-processing-time",
                                "asbestos-registration-fees", "asbestos-posting-sign-and-registration",
                                "asbestos-cslb-certification-100sqft"]
                               + ([] if reg_needed else ["asbestos-registration-renewal-30-days"]),
                               permit_type="registration" + ("" if reg_needed else " (held)"), holder="your company",
                               fee_usd=P("asbestos-registration-fees", "application_fee_usd") if reg_needed else 0,
                               lead_time=f"{lead_bd} business days" if reg_needed else "",
                               deadline=_add_working_days(start.date(), -lead_bd).isoformat()
                               if start and reg_needed else None,
                               named_people=["CSLB C-22 contractor or asbestos-certified qualifier"]))
        else:
            items.append(_item("Asbestos registration (Cal/OSHA)", "registration", False,
                               f"{asbestos_sqft:g} sq ft < {a['surface_area_sqft_gte']} sq ft: no registration, but "
                               "the job must still be reported under 8 CCR 5208.", ["asbestos-dosh-registration-100sqft"]))
        n = core.rule("asbestos-job-notification").parameters
        items.append(_item("Asbestos job notice to district office", "notification", need,
                           "Written notice before each separate job or phase (address, location, dates, certified "
                           f"supervisor, air-sampling person, work description); emergencies by phone then in writing "
                           f"within {n['emergency_oral_then_written_within_hours']} hours. Hold a pre-job safety "
                           "conference with the owner, contractor and employees.",
                           ["asbestos-job-notification", "asbestos-safety-conference"], holder="your company",
                           lead_time="before work starts", deadline=_fmt(start) if start and need else None))

    # ---- lead notice
    if lead_disturbed_sqft > 0:
        ld = core.rule("lead-pre-job-notification-24h").parameters
        need = lead_disturbed_sqft >= ld["exempt_if_area_sqft_or_linear_ft_lt"]
        items.append(_item("Lead work notice to district office", "notification", need,
                           f"{lead_disturbed_sqft:g} sq ft disturbed "
                           f"{'>=' if need else '<'} {ld['exempt_if_area_sqft_or_linear_ft_lt']}: "
                           + (f"if the work includes a lead trigger task listed in 8 CCR 1532.1(d)(2) (e.g. manual "
                              "demolition, scraping or sanding, power-tool cleaning, abrasive blasting, torch cutting), "
                              f"written notice must be RECEIVED at least {ld['advance_notice_hours']} hours before "
                              "the work starts (letter, fax, email, or phone then writing). Also exempt if only "
                              f"torch cutting/welding up to {ld['exempt_if_only_torch_cutting_welding_hours_per_shift_lte']}"
                              f" h/shift or material under {ld['exempt_if_lead_pct_lt']}% lead." if need else
                              "no notice needed."), ["lead-pre-job-notification-24h"], holder="your company",
                           lead_time=f"{ld['advance_notice_hours']} hours" if need else "",
                           deadline=_fmt(start - timedelta(hours=ld["advance_notice_hours"])) if start and need else None))

    # ---- fees: one annual permit covers the employer, so its fee is charged once
    seen_annual, total = False, 0.0
    for i in items:
        if not (i.required and i.fee_usd):
            continue
        if i.permit_type == "annual":
            if seen_annual:
                i.fee_usd, i.why = 0, i.why + " (fee already counted on your annual permit)"
                continue
            seen_annual = True
        total += i.fee_usd
    fee_parts = [f"{i.activity} ({i.permit_type}) ${i.fee_usd:g}" for i in items if i.required and i.fee_usd]
    res = PermitsResult(as_of=today.isoformat(), planned_start=_fmt(start) if start else None, items=items,
                        permits_required=[i.activity for i in items if i.required and i.kind != "notification"],
                        total_fees_usd=round(total, 2),
                        fee_math=(" + ".join(fee_parts) + f" = ${total:g}") if fee_parts else "No Cal/OSHA fees.",
                        penalties=[f"Working without a required permit or asbestos registration: minimum proposed "
                                   f"penalty ${P('permit-violation-min-penalty-1250', 'minimum_proposed_penalty_usd'):,}"
                                   f" (not below ${P('permit-violation-min-penalty-1250', 'minimum_after_adjustment_usd'):,}"
                                   " after adjustments)."])
    for i in items:
        i.deadline_passed = bool(i.required and i.deadline and _date(i.deadline, "deadline") < today)
    late = [i.activity for i in items if i.deadline_passed]
    if late:
        res.notes.append(f"Already past the latest date as of {today.isoformat()}: {', '.join(late)}. Act today and "
                         "expect the start date to slip.")
    required_permits = [i for i in items if i.required and i.kind == "permit"]
    if any(i.permit_type == "annual" for i in required_permits) and any(
            i.permit_type == "project" and i.holder == "your company" for i in required_permits):
        res.notes.append("You need an annual permit anyway: ask the district office to list your trench and "
                         "scaffold/falsework work on it too, so those jobs need only 24-hour notices instead of "
                         "project permits.")
    if required_permits:
        res.cite("permit-fees", "permit-posting", "permit-application-contents")
        res.notes.append(f"Pay by {fees['payment']}. Post a copy of each permit at or near the work (at an excavation, "
                         "keep it available on site). Applications need your CSLB license number, workers' comp "
                         "proof and a description of safe work methods.")
        if any(i.permit_type == "annual" for i in required_permits):
            res.notes.append("8 CCR 341.3(e) states the annual permit fee period as Jan 1-Dec 31 while 341.1 says it "
                             "expires a year after issuance; confirm the expiry with your district office.")
    if start and start_date_only:
        res.notes.append("planned_start had no time, so deadlines assume work starts at 00:00 that day; give a time "
                         "for exact hours.")
    if all_exempt:
        res.notes.append(f"Exemption '{exemption}': 8 CCR 341 permits do not apply, but all safety orders still do. "
                         "341(e)(1)-(2) name the government body or the PUC-regulated utility itself; a private "
                         "contractor doing the work for one is a separate employer and is not listed, so it still "
                         "needs its own permits.")
    res.notes.append(WORKING_DAY_NOTE + (" Exception: the NESHAP/air district count already matches federal law, "
                                         "which counts holidays that fall Monday-Friday as working days."
                                         if demolition else ""))
    res.cite("permit-violation-min-penalty-1250")
    _finish(res, items)
    return res


# ================================================================ injury_reporting_deadlines
DeadlineStatus = Literal["overdue", "due_today", "upcoming", "do_now", "not_required"]


class Deadline(BaseModel):
    action: str
    required: bool
    due: Optional[str] = None
    status: DeadlineStatus
    hours_left: Optional[float] = None
    days_left: Optional[int] = None
    who_and_how: str = ""
    how_computed: str = ""
    rule_ids: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    verified: bool = False


class Penalty(BaseModel):
    item: str
    detail: str
    min_usd: Optional[float] = None
    max_usd: Optional[float] = None
    rule_ids: list[str] = Field(default_factory=list)
    verified: bool = False


class InjuryReportingResult(core.Result):
    as_of: str
    incident_datetime: str
    employer_knew_at: str
    reportable_to_cal_osha: bool
    recordable: bool
    log300_required: bool
    deadlines: list[Deadline]
    report_contents: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    penalties: list[Penalty] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _deadline(action, required, due, now, date_only, rule_ids, who="", how="", unverified=()) -> Deadline:
    d = Deadline(action=action, required=required, status="not_required", who_and_how=who, how_computed=how,
                 rule_ids=list(rule_ids), unverified=list(unverified), verified=_ok(rule_ids, unverified))
    if not required:
        return d
    if due is None:
        d.status = "do_now"
        return d
    if isinstance(due, datetime):
        d.due = _fmt(due)
        if date_only:
            d.days_left = (due.date() - now.date()).days
            d.status = "overdue" if d.days_left < 0 else "due_today" if d.days_left == 0 else "upcoming"
        else:
            d.hours_left = round((due - now).total_seconds() / 3600, 2)
            d.status = "overdue" if d.hours_left < 0 else "due_today" if due.date() == now.date() else "upcoming"
    else:
        d.due = due.isoformat()
        d.days_left = (due - now.date()).days
        d.status = "overdue" if d.days_left < 0 else "due_today" if d.days_left == 0 else "upcoming"
    return d


_CONTENT_TEXT = {
    "time_and_date_of_accident": "Time and date of the accident",
    "employer_name_address_phone": "Employer name, address and phone",
    "reporter_name_and_title_or_badge": "Name and title of the person reporting",
    "accident_site_address": "Address of the accident site",
    "onsite_contact_name": "Name of an on-site contact",
    "injured_employee_names_and_addresses": "Name(s) and address(es) of the injured employee(s)",
    "nature_of_injury": "Nature of the injury",
    "location_employee_moved_to": "Where the injured employee(s) were taken",
    "other_law_enforcement_agencies_present": "Other law enforcement agencies at the scene",
    "accident_description_and_whether_scene_altered": "What happened and whether the scene was altered",
}


def injury_reporting_deadlines(incident_datetime: str, serious: bool = False, fatality: bool = False,
                               hospitalized: bool = False, *, employer_knew_at: Optional[str] = None,
                               beyond_first_aid_or_lost_time: bool = True,
                               peak_employees_last_year: Optional[int] = None, exigent_circumstances: bool = False,
                               public_road_outside_construction_zone: bool = False, workplace_crime: bool = False,
                               as_of=None) -> InjuryReportingResult:
    inc, inc_date_only = _dt(incident_datetime, "incident_datetime")
    knew, knew_date_only = _dt(employer_knew_at, "employer_knew_at") if employer_knew_at else (inc, inc_date_only)
    if knew_date_only and knew.date() == inc.date():
        knew = max(knew, inc)
    if knew < inc:
        raise ValueError("employer_knew_at cannot be before incident_datetime")
    if as_of:
        now, now_date_only = _dt(as_of, "as_of")
    else:
        now, now_date_only = datetime.now(logic.CA_TZ).replace(tzinfo=None), False
    serious_case = serious or hospitalized or fatality
    excluded = public_road_outside_construction_zone
    reportable = serious_case and not excluded
    recordable = fatality or hospitalized or serious or beyond_first_aid_or_lost_time
    peak_known = peak_employees_last_year is not None
    lim = P("log300-size-exemption-10-or-fewer", "exempt_if_peak_employees_lte")
    log_req = recordable and (not peak_known or peak_employees_last_year > lim)
    kd = knew.date()
    dl: list[Deadline] = []

    # 1. Cal/OSHA (DOSH) report
    r342 = core.rule("serious-injury-report-8-hours").parameters
    hrs, ex_hrs = r342["deadline_hours"], r342["deadline_hours_exigent_circumstances"]
    due8 = knew + timedelta(hours=hrs)
    due342 = max(due8, inc + timedelta(hours=ex_hrs)) if exigent_circumstances else due8
    kinds = [k for k, on in [("death", fatality), ("inpatient hospitalization", hospitalized),
                             ("amputation / loss of eye / serious permanent disfigurement", serious)] if on]
    if not reportable:
        how = ("Public street or highway accident outside a construction zone is excluded from the definition."
               if serious_case else "Not a death or serious injury/illness (no inpatient hospitalization beyond "
                                    "observation, amputation, eye loss or serious permanent disfigurement).")
    else:
        how = f"Employer knew at {_fmt(knew)} + {hrs} hours = {_fmt(due8)}"
        if exigent_circumstances:
            # 342(a): the exigent-circumstances limit runs from the incident, not from when the employer knew.
            how += (f". Exigent circumstances you can demonstrate allow up to {ex_hrs} hours after the INCIDENT: "
                    f"{_fmt(inc)} + {ex_hrs} hours = {_fmt(inc + timedelta(hours=ex_hrs))}; the later of the two "
                    f"is {_fmt(due342)}")
        how += ("; you gave no time, so the clock starts at 00:00" if knew_date_only else "") + "."
    dl.append(_deadline("Report to Cal/OSHA (DOSH) district office", reportable, due342, now,
                        now_date_only, ["serious-injury-report-8-hours", "serious-injury-definition",
                                        "serious-injury-report-cal-osha-immediately"],
                        f"Immediately, by phone or Cal/OSHA's online reporting, for: {', '.join(kinds) or 'n/a'}. "
                        "Required even if you are exempt from Log 300 recordkeeping.", how))

    # 2. DWC-1 claim form
    dwc = P("workers-comp-claim-form-one-working-day", "deadline_working_days")
    due = _add_working_days(kd, dwc)
    first_aid_only = not recordable
    dl.append(_deadline("Give the employee a DWC-1 workers' comp claim form", True, due, now, now_date_only,
                        ["workers-comp-claim-form-one-working-day"],
                        "Hand it to the employee (or mail it first class); when returned, complete the employer part, "
                        "give them a copy and send it to your claims administrator.",
                        f"Learned {kd.isoformat()} + {dwc} working day = {due.isoformat()}. DIR's employer guidance "
                        f"covers any work injury or illness you learn of. {WORKING_DAY_NOTE}"
                        + (" " + UNVERIFIED["dwc1_trigger_5401a"]["text"] if first_aid_only else ""),
                        unverified=["dwc1_trigger_5401a"] if first_aid_only else []))

    # 3. Form 5020
    d5 = P("employer-report-form-5020-5-days", "deadline_days")
    due = kd + timedelta(days=d5)
    dl.append(_deadline("File Form 5020 (Employer's Report of Occupational Injury or Illness)", recordable, due, now,
                        now_date_only, ["employer-report-form-5020-5-days", "workers-comp-injury-report-5-days",
                                        "injury-report-5-day-deadline"],
                        "With your workers' comp insurer (or DIR/DLSR if self-insured); include the employee's SSN.",
                        f"Learned {kd.isoformat()} + {d5} days = {due.isoformat()} (counted as calendar days; the "
                        "source does not say calendar or working, so calendar is the safe reading)."
                        if recordable else "No lost time beyond the day of injury and no treatment beyond first aid."))

    # 4. Log 300 / 301
    d7 = P("log300-record-within-7-days", "deadline_calendar_days")
    due = kd + timedelta(days=d7)
    log_how = (f"Learned {kd.isoformat()} + {d7} calendar days = {due.isoformat()}." if log_req else
               f"Size-exempt: peak {peak_employees_last_year} employees last year is {lim} or fewer."
               if recordable else "Not recordable (first aid only, no lost time).")
    if log_req and not peak_known:
        log_how += f" Assumed required; you are exempt only if your company peak last calendar year was {lim} or fewer."
    dl.append(_deadline("Enter case on Cal/OSHA Form 300 and complete Form 301", log_req, due, now, now_date_only,
                        ["log300-record-within-7-days", "log300-size-exemption-10-or-fewer"],
                        "Privacy cases: write 'privacy case' instead of the name.", log_how,
                        unverified=["log300_recording_criteria"] if recordable else []))

    # 5. 300A posting, ITA, retention
    yr = inc.year
    post = core.rule("log300a-posting-feb1-apr30").parameters
    p_from, p_to = _md(post["post_by"], yr + 1), _md(post["keep_posted_until"], yr + 1)
    dl.append(_deadline(f"Post certified Form 300A summary for {yr}", log_req, p_from, now, now_date_only,
                        ["log300a-posting-feb1-apr30", "log300a-annual-summary-certification",
                         "log300a-mail-to-field-employees"],
                        f"Post {p_from.isoformat()} through {p_to.isoformat()}; certified by a company executive; mail "
                        "or hand a copy to field employees who do not report weekly to the posting location.",
                        f"Year covered {yr} -> post by {post['post_by']} and keep up until {post['keep_posted_until']} "
                        f"of {yr + 1}."))
    ita = core.rule("osha-ita-electronic-submission-march-2").parameters
    lo, hi = ita["form_300a_construction_range"]
    ita_req = log_req and peak_known and (lo <= peak_employees_last_year <= hi or
                                          peak_employees_last_year >= ita["form_300a_if_employees_gte"])
    dl.append(_deadline(f"Submit {yr} Form 300A data to OSHA (ITA)", ita_req, _md(ita["submit_by"], yr + 1), now,
                        now_date_only, ["osha-ita-electronic-submission-march-2"],
                        "Electronically on OSHA's Injury Tracking Application.",
                        f"Construction establishments with {lo}-{hi} employees (or {ita['form_300a_if_employees_gte']}+)"
                        f" submit by {ita['submit_by']} of the following year. OSHA counts the establishment's peak "
                        f"during {yr}; peak_employees_last_year is used as the estimate."
                        + ("" if peak_known else " Give peak_employees_last_year to evaluate.")))
    ret = P("log300-retention-5-years", "retention_years")
    dl.append(_deadline("Keep Forms 300, 301, 300A", log_req, date(yr + ret, 12, 31), now, now_date_only,
                        ["log300-retention-5-years"], "Keep and update Form 300 for newly found cases.",
                        f"End of {yr} + {ret} years = {yr + ret}-12-31."))

    # 6. Workplace crime
    if workplace_crime:
        wn = P("wvpp-workers-comp-notice-one-working-day", "workers_comp_notice_working_days")
        due = _add_working_days(kd, wn)
        dl.append(_deadline("Written workers' comp eligibility notice to crime victim", True, due, now, now_date_only,
                            ["wvpp-workers-comp-notice-one-working-day", "workers-comp-workplace-crime-notice"],
                            "Personal delivery or first-class mail; covers psychiatric injury too.",
                            f"{kd.isoformat()} + {wn} working day = {due.isoformat()}."))
        dl.append(_deadline("Enter the incident on the violent incident log", True, None, now, now_date_only,
                            ["wvpp-violent-incident-log", "wvpp-plan-review-annual"],
                            "Required if your company is covered by Labor Code 6401.9. No personal identifying "
                            "information; then investigate and review the WVPP.",
                            "No fixed deadline; do it now."))

    res = InjuryReportingResult(as_of=_fmt(now) if not now_date_only else now.date().isoformat(),
                                incident_datetime=_fmt(inc), employer_knew_at=_fmt(knew),
                                reportable_to_cal_osha=reportable, recordable=recordable, log300_required=log_req,
                                deadlines=dl)
    if reportable:
        res.report_contents = [_CONTENT_TEXT.get(f, f.replace("_", " ")) for f in
                               P("serious-injury-report-contents", "fields")]
        res.next_steps.append("Call Cal/OSHA now; do not wait for medical details. Say whether the scene was "
                              "altered, and leave it as is except to help the injured or prevent further harm.")
    res.next_steps += ["Investigate the cause under your IIPP and correct the hazard (8 CCR 3203(a)(5)-(6)).",
                       "Notify your workers' comp carrier promptly; Form 5020 is due to the insurer within "
                       f"{P('employer-report-form-5020-5-days', 'deadline_days')} days if it applies."]
    res.penalties = [
        Penalty(item="Late or missing Cal/OSHA serious injury/death report",
                detail="Minimum penalty for failing to report on time under 8 CCR 342(a) (8 CCR 336(a)(6)).",
                min_usd=P("failure-to-report-min-penalty-5000", "minimum_penalty_usd"),
                rule_ids=["failure-to-report-min-penalty-5000"]),
        Penalty(item="Log 300 / 300A recordkeeping or posting failures",
                detail="Regulatory violation (8 CCR 334(a)), per violation.",
                min_usd=P("calosha-penalty-amounts", "regulatory_min_proposed_usd"),
                max_usd=P("calosha-penalty-amounts", "regulatory_max_usd"),
                rule_ids=["calosha-penalty-amounts"]),
        Penalty(item="Form 5020 not filed", detail=P("workers-comp-injury-report-5-days", "failure") + ".",
                rule_ids=["workers-comp-injury-report-5-days"]),
    ]
    if serious_case:
        cs = P("cslb-discipline-safety-violations", "cslb_civil_penalty_range_usd")
        res.penalties.append(Penalty(item="CSLB license discipline", detail="If a Cal/OSHA safety violation caused the "
                                     "death or serious injury (B&P 7109.5).", min_usd=cs[0], max_usd=cs[1],
                                     rule_ids=["cslb-discipline-safety-violations"]))
        w = core.rule("willful-violation-death-criminal").parameters
        res.penalties.append(Penalty(item="Criminal prosecution (Labor Code 6425)",
                                     detail=f"Only if a willful violation caused death or permanent or prolonged "
                                            f"impairment: up to {w['max_imprisonment_years']} years and/or a fine up "
                                            f"to ${w['max_fine_usd']:,} (corporation/LLC up to "
                                            f"${w['max_fine_usd_corporation_or_llc']:,}). Call an attorney before "
                                            "giving statements.", max_usd=w["max_fine_usd_corporation_or_llc"],
                                     rule_ids=["willful-violation-death-criminal"]))
    for p in res.penalties:
        p.verified = _ok(p.rule_ids)
    if recordable:
        res.notes.append("Log 300 recordability follows 8 CCR 14300.7: "
                         + ", ".join(UNVERIFIED["log300_recording_criteria"]["criteria"]) + ".")
    if inc_date_only:
        res.notes.append("incident_datetime had no time; the 8-hour clock was started at 00:00 (earliest).")
    res.notes.append("Without an operative IIPP, Cal/OSHA will not reduce serious-violation penalties for good faith "
                     "or history.")
    res.cite("penalty-no-iipp-no-reduction", "iipp-written-program-required", "iipp-hazard-correction")
    _finish(res, [*dl, *res.penalties])
    if fatality or serious_case:
        res.notes.append("A death or serious injury usually brings a Cal/OSHA investigation: involve your attorney and "
                         "safety professional.")
    return res


# ================================================================ MCP tools
def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}

    @mcp.tool(name="generate_safety_programs", annotations=ro)
    def _generate_safety_programs(
        company_name: Annotated[str, Field(description="Legal company name to print on the programs")],
        employee_count: Annotated[int, Field(ge=1, description="Total employees now (company-wide)")],
        worksites_have_public_access: Annotated[bool, Field(description=(
            "True if the public can reach your jobsites, office or yard (affects the workplace violence exemption)"))],
        outdoor_work: Annotated[bool, Field(description="Any employees work outdoors (8 CCR 3395 heat rule)")] = True,
        heat_exposure: Annotated[bool, Field(description=(
            "Fully enclosed indoor work areas reach 82F or more while employees are present, e.g. attics, mechanical "
            "rooms, unconditioned buildings (8 CCR 3396). Outdoor heat is covered by outdoor_work"))] = False,
        excavation_max_depth_ft: Annotated[float, Field(ge=0, description="Deepest trench/excavation you dig, ft")] = 0,
        workers_enter_trench: Annotated[bool, Field(description="Workers go down into the trench/excavation")] = False,
        building_stories: Annotated[int, Field(ge=0, description="Stories of the tallest structure you build")] = 0,
        building_height_ft: Annotated[Optional[float], Field(description="Height of that structure in ft")] = None,
        demolition: Annotated[bool, Field(description="You demolish buildings or structures")] = False,
        scaffold_or_falsework_height_stories: Annotated[float, Field(ge=0, description=(
            "Tallest scaffold, falsework or vertical shoring you erect, in stories"))] = 0,
        silica_tasks: Annotated[bool, Field(description="Cutting, drilling, grinding concrete/masonry/stone")] = False,
        crane_use: Annotated[bool, Field(description="Cranes or derricks used by your crews")] = False,
        asbestos_or_lead: Annotated[bool, Field(description="Work that disturbs asbestos or lead")] = False,
        respirators_used: Annotated[bool, Field(description="Employees use required respirators")] = False,
        max_employees_at_one_site: Annotated[Optional[int], Field(description=(
            "Most employees at one place at the same time (default employee_count)"))] = None,
        peak_employees_last_year: Annotated[Optional[int], Field(description=(
            "Company-wide peak employees at any time last calendar year, for Log 300 (default employee_count)"))] = None,
        hazardous_chemicals: Annotated[bool, Field(description="Crews use fuels, cement, adhesives, coatings, solvents")] = True,
        max_fall_height_ft: Annotated[Optional[float], Field(description="Highest fall exposure, ft")] = None,
        conventional_fall_protection_infeasible: Annotated[bool, Field(description=(
            "Guardrails, nets and personal fall protection are infeasible or a greater hazard for some work"))] = False,
        naics_code: Annotated[Optional[str], Field(description="NAICS code, e.g. 238160 (roofing)")] = None,
        majority_language: Annotated[Optional[str], Field(description="Language most employees understand")] = None,
        last_toolbox_meeting: Annotated[Optional[str], Field(description="ISO date of the last toolbox meeting")] = None,
        shift_hours: Annotated[float, Field(gt=0, le=24, description="Typical shift length, for the water math")] = 8,
        as_of: Annotated[Optional[str], Field(description="ISO date to evaluate as of; default today in California")] = None,
    ) -> SafetyProgramsResult:
        """Cal/OSHA written-program generator for a California construction employer. Returns every program with
        status required / conditional / exempt / not_required and why: IIPP (8 CCR 3203, 1509), Code of Safe
        Practices (1509), outdoor and indoor heat illness plans (3395, 3396), Workplace Violence Prevention Plan
        (Labor Code 6401.9, with the small-site exemption test), HazCom (5194), respiratory protection (5144), silica
        exposure control plan (1532.3), emergency medical/action plan (1512), fall protection plan (1671.1, only when
        conventional protection is infeasible), excavation, crane and lead/asbestos procedures. Each program has its
        required elements with citations, the responsible person role, the licensed or designated people the law
        requires (competent person, qualified person, engineer, PLHCP) and a fill-in markdown outline
        (document_outline_md). Also returns the training schedule (toolbox meetings every 10 working days, next due
        date if last_toolbox_meeting is given), recordkeeping (Log 300 applicability by peak employees, 300A posting
        window, OSHA electronic submission), required postings, and Cal/OSHA permits to check."""
        return generate_safety_programs(
            company_name, employee_count, worksites_have_public_access, outdoor_work, heat_exposure,
            excavation_max_depth_ft, workers_enter_trench, building_stories, building_height_ft, demolition,
            scaffold_or_falsework_height_stories, silica_tasks, crane_use, asbestos_or_lead, respirators_used, as_of,
            max_employees_at_one_site=max_employees_at_one_site, peak_employees_last_year=peak_employees_last_year,
            hazardous_chemicals=hazardous_chemicals, max_fall_height_ft=max_fall_height_ft,
            conventional_fall_protection_infeasible=conventional_fall_protection_infeasible, naics_code=naics_code,
            majority_language=majority_language, shift_hours=shift_hours, last_toolbox_meeting=last_toolbox_meeting)

    @mcp.tool(name="check_cal_osha_permits", annotations=ro)
    def _check_cal_osha_permits(
        excavation_max_depth_ft: Annotated[float, Field(ge=0, description="Deepest trench/excavation on the job, ft")] = 0,
        workers_enter_trench: Annotated[bool, Field(description="Anyone must go down into it")] = False,
        building_stories: Annotated[int, Field(ge=0, description="Stories of the structure being built")] = 0,
        building_height_ft: Annotated[Optional[float], Field(description=(
            "Intended height of the structure when complete, ft (top incl. parapets to lowest grade)"))] = None,
        demolition: Annotated[bool, Field(description="The job includes demolition")] = False,
        demolition_height_ft: Annotated[Optional[float], Field(description=(
            "Height of the structure being demolished, ft (default building_height_ft)"))] = None,
        demolition_stories: Annotated[Optional[int], Field(description="Stories of the structure being demolished")] = None,
        scaffold_or_falsework_height_ft: Annotated[Optional[float], Field(description=(
            "Intended height of scaffold, falsework or vertical shoring when complete, ft"))] = None,
        scaffold_or_falsework_height_stories: Annotated[float, Field(ge=0, description="Same, in stories")] = 0,
        tower_crane: Annotated[bool, Field(description="You erect, climb or dismantle a tower crane")] = False,
        role: Annotated[Role, Field(description=(
            "project_administrator = GC, prime, owner-builder or CM; subcontractor = trade employer"))] = "project_administrator",
        self_performs_structural_work: Annotated[bool, Field(description=(
            "Subcontractor does steel/member erection, decking, curtain wall/precast, concrete on steel or "
            "structural/roof framing"))] = True,
        has_annual_permit: Annotated[bool, Field(description="You already hold a Cal/OSHA annual permit")] = False,
        exemption: Annotated[Exemption, Field(description=(
            "8 CCR 341(e) exemption that applies, if any. government_agency / puc_regulated_utility only when YOUR "
            "company is that agency or utility (a contractor on public works or utility work is not exempt); "
            "emergency_underground_repair, grave and swimming_pool_excavation exempt the trench permit only"))] = "none",
        asbestos_sqft: Annotated[float, Field(ge=0, description="Sq ft of asbestos-containing material disturbed")] = 0,
        has_asbestos_registration: Annotated[bool, Field(description="You hold a current Cal/OSHA asbestos registration")] = False,
        lead_disturbed_sqft: Annotated[float, Field(ge=0, description="Sq ft (or linear ft) of lead-containing material disturbed")] = 0,
        planned_start: Annotated[Optional[str], Field(description=(
            "ISO date or datetime work starts, e.g. 2026-12-14T07:00, to compute apply/notify-by dates"))] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date to evaluate as of; default today in California")] = None,
    ) -> PermitsResult:
        """Cal/OSHA 8 CCR 341 permit check for one California construction job. Returns each permit, registration
        and notification with required yes/no and the math: trench/excavation 5 ft+ that a worker must descend into
        (annual or project permit), building/structure intended over 36 ft or more than 3 stories (project permit
        for the GC/project administrator, annual permit for structural trades), scaffold/falsework/shoring over
        36 ft or 3 stories (plus civil engineer design over 14 ft), demolition over 36 ft, tower cranes, asbestos
        registration at 100 sq ft, lead 24-hour notice, NESHAP/air district 10-working-day demolition notice and
        811. Gives the fee per permit and total, lead times (project permit issued within 5 working days, so the
        latest apply date is 6 working days before start; annual permit 24-hour site notice), and latest
        apply/notify date when planned_start is given (deadline_passed if already late), plus the minimum penalty
        for working without a permit. Government/utility exemptions apply only to the agency or utility itself."""
        return check_cal_osha_permits(
            excavation_max_depth_ft, workers_enter_trench, building_stories, building_height_ft, demolition=demolition,
            demolition_height_ft=demolition_height_ft, demolition_stories=demolition_stories,
            scaffold_or_falsework_height_ft=scaffold_or_falsework_height_ft,
            scaffold_or_falsework_height_stories=scaffold_or_falsework_height_stories, tower_crane=tower_crane,
            role=role, self_performs_structural_work=self_performs_structural_work,
            has_annual_permit=has_annual_permit, exemption=exemption, asbestos_sqft=asbestos_sqft,
            has_asbestos_registration=has_asbestos_registration, lead_disturbed_sqft=lead_disturbed_sqft,
            planned_start=planned_start, as_of=as_of)

    @mcp.tool(name="injury_reporting_deadlines", annotations=ro)
    def _injury_reporting_deadlines(
        incident_datetime: Annotated[str, Field(description="When it happened, ISO local time e.g. 2026-11-20T14:30")],
        serious: Annotated[bool, Field(description=(
            "Amputation, loss of an eye, or serious permanent disfigurement"))] = False,
        fatality: Annotated[bool, Field(description="An employee died")] = False,
        hospitalized: Annotated[bool, Field(description=(
            "Admitted as an inpatient for more than observation or diagnostic testing"))] = False,
        employer_knew_at: Annotated[Optional[str], Field(description=(
            "When the employer (including a supervisor) knew, or with diligent inquiry would have known, if later "
            "than the incident (default incident_datetime)"))] = None,
        beyond_first_aid_or_lost_time: Annotated[bool, Field(description=(
            "Medical treatment beyond first aid or time lost beyond the day of injury"))] = True,
        peak_employees_last_year: Annotated[Optional[int], Field(description=(
            "Company peak employees at any time last calendar year (Log 300 exemption and OSHA submission)"))] = None,
        exigent_circumstances: Annotated[bool, Field(description=(
            "You can show exigent circumstances (allows the Cal/OSHA report up to 24 hours after the incident)"))] = False,
        public_road_outside_construction_zone: Annotated[bool, Field(description=(
            "Accident on a public street/highway outside a construction zone"))] = False,
        workplace_crime: Annotated[bool, Field(description="The employee was the victim of a crime at work")] = False,
        as_of: Annotated[Optional[str], Field(description="ISO date or datetime 'now'; default now in California")] = None,
    ) -> InjuryReportingResult:
        """Injury and death reporting deadlines for a California construction employer. Returns each deadline with
        due date/time, status (overdue / due_today / upcoming / do_now / not_required), hours or days left and the
        math: Cal/OSHA report within 8 hours of knowing of a death or serious injury (with exigent circumstances,
        up to 24 hours after the incident), DWC-1 claim form within 1 working day, Form 5020 to the insurer within
        5 days, Log 300/301 entry within 7 calendar days (unless size-exempt at 10 or fewer employees), Form 300A posting Feb 1-Apr 30, OSHA electronic
        submission by March 2, 5-year retention, and workplace-crime notices. Also returns what the Cal/OSHA report
        must contain, next steps and penalties ($5,000 minimum for a late serious-injury report, recordkeeping
        penalties, CSLB discipline, Labor Code 6425)."""
        return injury_reporting_deadlines(
            incident_datetime, serious, fatality, hospitalized, employer_knew_at=employer_knew_at,
            beyond_first_aid_or_lost_time=beyond_first_aid_or_lost_time,
            peak_employees_last_year=peak_employees_last_year, exigent_circumstances=exigent_circumstances,
            public_road_outside_construction_zone=public_road_outside_construction_zone,
            workplace_crime=workplace_crime, as_of=as_of)
