"""Tenant dashboard: license health, sub risk, lien deadlines, COI and license expiries, toolbox talk cadence, and
the same deadlines as an iCalendar feed. All rules come from tradecomply.logic / rules / core, never from here."""
from datetime import date, timedelta
from typing import get_args

from tradecomply import core, logic
from tradecomply.rules import CA_LIEN

CLAIMANT_ROLES = get_args(logic.Role)
ROLE_LABELS = {
    "direct_contractor": "Prime / GC (hired by the owner)",
    "subcontractor": "Subcontractor",
    "supplier": "Material supplier",
    "equipment_lessor": "Equipment rental",
    "design_professional": "Architect / engineer / surveyor",
    "laborer": "Laborer (wages only)",
}
RAG_ORDER = {"red": 0, "amber": 1, "green": 2}
PAST_WINDOW_DAYS = 30           # past-due items older than this drop off the dashboard (still in the calendar)
TOOLBOX_RULE = "toolbox-tailgate-meetings-10-working-days"
LICENSE_RULE = "license-renewal-every-2-years"


def _d(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _rule_citation(rule_id: str) -> tuple[list[dict], bool, str | None]:
    try:
        c = core.rule(rule_id).citation
        return [{"citation": c.citation, "source": c.source, "status": c.status, "checked_on": c.checked_on}], \
            c.verified, c.checked_on
    except KeyError:
        return [], False, None


def _toolbox_interval() -> int:
    try:
        return int(core.rule(TOOLBOX_RULE).param("max_interval_working_days"))
    except (KeyError, ValueError, TypeError):
        return 10


def rag_for_days(days: int, amber_within: int, red_within: int = 14) -> str:
    if days < 0 or days <= red_within:
        return "red"
    if days <= amber_within:
        return "amber"
    return "green"


def lien_rag(days: int) -> str:
    return {"expired": "red", "critical": "red", "urgent": "amber", "normal": "green"}[logic._risk(days)]


def add_working_days(start: date, n: int) -> date:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _weekdays_through(ordinal: int) -> int:
    """Mon-Fri days from 0001-01-01 (a Monday, ordinal 1) through the given day ordinal, in constant time."""
    weeks, rest = divmod(ordinal, 7)
    return weeks * 5 + min(rest, 5)


def working_days_between(a: date, b: date) -> int:
    """Signed count of Mon-Fri days after a up to and including b; for b < a, minus the Mon-Fri days from b up to
    the day before a. Constant time, so a far-off date can't make the dashboard spin."""
    oa, ob = a.toordinal(), b.toordinal()
    if ob >= oa:
        return _weekdays_through(ob) - _weekdays_through(oa)
    return _weekdays_through(ob - 1) - _weekdays_through(oa - 1)


def _item(kind: str, title: str, when: date, today: date, rag: str, detail: str = "", project: dict | None = None,
          sub: dict | None = None, verified: bool = False, citations=None, checked_on: str | None = None) -> dict:
    return {
        "key": f"{kind}:{project['id'] if project else ''}:{sub['id'] if sub else ''}:{when.isoformat()}",
        "kind": kind, "title": title, "date": when.isoformat(), "days_left": (when - today).days, "rag": rag,
        "detail": detail, "project_id": project["id"] if project else None,
        "project_name": project["name"] if project else None, "sub_id": sub["id"] if sub else None,
        "sub_name": sub["name"] if sub else None, "verified": bool(verified), "checked_on": checked_on,
        "citations": citations or [],
    }


# ---------------------------------------------------------------------------- license + subs
def license_card(cslb_license: str | None) -> dict:
    if not cslb_license:
        return {"rag": "amber", "license_number": None, "problems": ["Add your CSLB license number in Settings."],
                "license": None}
    lic = logic.verify_license(cslb_license)
    problems, rag = [], "green"
    if not lic.found:
        partial = lic.data_source == "CSLB master file (partial copy)"
        problems.append("Not in our copy of CSLB data; confirm on the CSLB site." if partial
                        else "No CSLB record found for this number.")
        rag = "amber" if partial else "red"
    else:
        if lic.status != "active":
            problems.append(f"License is {lic.status}.")
            rag = "red"
        if lic.workers_comp == "none":
            problems.append("No workers' comp policy or exemption on file with CSLB (suspension risk).")
            rag = "red"
        mandatory = logic._wc_mandatory_class(lic.classifications)
        if lic.workers_comp == "exempt" and mandatory:
            problems.append(f"Class {mandatory} must carry workers' comp even with no employees.")
            rag = "red"
        if not lic.contractor_bond_on_file:
            problems.append("Contractor bond not on file with CSLB.")
            rag = "red"
        if lic.expires_in_days is not None and lic.expires_in_days <= 60 and rag != "red":
            problems.append(f"License expires in {lic.expires_in_days} days. Renew before the date.")
            rag = "red" if lic.expires_in_days <= 14 else "amber"
        for flag in lic.disciplinary_actions:
            problems.append(flag)
            rag = "red" if rag == "red" else "amber"
    return {"rag": rag, "license_number": cslb_license, "problems": problems, "license": lic.model_dump()}


SUB_RAG = {"low": "green", "medium": "amber", "high": "red", "do_not_use": "red"}


def sub_check(sub: dict) -> dict:
    base = {"sub_id": sub["id"], "name": sub["name"], "license_number": sub.get("license_number"),
            "required_class": sub.get("required_class"), "coi_expiry": sub.get("coi_expiry"),
            "project_id": sub.get("project_id")}
    if not sub.get("license_number"):
        return {**base, "rag": "red", "overall_risk": "do_not_use", "problems": ["No CSLB license number on file"],
                "recommended_action": "Get the sub's CSLB license number before award.", "license": None,
                "verified": False}
    r = logic.check_subcontractor(sub["license_number"], sub.get("required_class") or None,
                                  sub.get("coi_expiry") or None, bool(sub.get("wc_required", True)))
    problems = list(r.problems)
    if not sub.get("w9_on_file"):
        problems.append("W-9 not on file")
    return {**base, "rag": SUB_RAG[r.overall_risk], "overall_risk": r.overall_risk, "problems": problems,
            "recommended_action": r.recommended_action, "verified": r.license.verified,
            "license": {"found": r.license.found, "business_name": r.license.business_name,
                        "status": r.license.status, "classifications": r.license.classifications,
                        "expiration_date": r.license.expiration_date, "expires_in_days": r.license.expires_in_days,
                        "workers_comp": r.license.workers_comp, "data_source": r.license.data_source}}


# ---------------------------------------------------------------------------- lien calendar
def project_deadlines(project: dict, today: date) -> tuple[list[dict], list[str]]:
    items, notes = [], []
    if project.get("status") == "closed":
        return items, ["Project is closed; no deadlines tracked."]
    extra = project.get("extra") or {}
    role = extra.get("claimant_role") or "direct_contractor"
    if role not in CLAIMANT_ROLES:
        role = "direct_contractor"
    if project.get("public_or_private") == "public":
        notes.append("Public project: mechanics liens don't attach to public property, so no lien calendar is "
                     "computed. Use stop payment notices / payment bond claims and the public works tools.")
        return items, notes
    first = project.get("first_furnishing_date") or None
    last = project.get("last_furnishing_date") or None
    completion = project.get("completion_date") or None
    noc = project.get("noc_date") or None
    start = project.get("start_date") or None
    try:
        if last or completion:
            r = logic.lien_deadlines(role, last or completion, first, completion, noc, today=today,
                                     construction_start_date=start)
        elif first or (role == "design_professional" and start):
            r = logic.lien_deadlines(role, first or start, first, today=today, construction_start_date=start)
            notes.append("Add the last date you furnished work (or the completion date) to compute the lien "
                         "recording deadline.")
        else:
            notes.append("Add the first furnishing date and completion date to build this project's lien calendar.")
            r = None
    except (ValueError, OverflowError) as e:
        items.append(_item("data_problem", "Check this project's dates", today, today, "amber",
                           str(e) if isinstance(e, ValueError) else "A date on this project is out of range.",
                           project=project))
        return items, notes
    cites = [{"citation": c} for c in (r.citations if r else [])]
    if r:
        notes.extend(r.notes)
        if r.preliminary_notice_deadline:
            d = _d(r.preliminary_notice_deadline)
            items.append(_item("prelim_notice", "Serve 20-day preliminary notice", d, today, lien_rag((d - today).days),
                               "Serve before this date to protect lien and payment rights for all of your work.",
                               project=project, verified=r.verified, citations=cites))
        if last or completion:
            d = _d(r.lien_recording_deadline)
            items.append(_item("lien_recording", "Last day to record a mechanics lien", d, today,
                               lien_rag((d - today).days), f"Rule applied: {r.lien_rule_applied}. Record before "
                               "this date, not on it.", project=project, verified=r.verified, citations=cites))
            d = _d(r.foreclosure_deadline_if_recorded_on_last_day)
            items.append(_item("lien_foreclosure", "Lien foreclosure suit deadline (if lien recorded on the last day)",
                               d, today, lien_rag((d - today).days),
                               "File suit to enforce the lien within 90 days after recording it.", project=project,
                               verified=r.verified, citations=cites))
    if role == "direct_contractor" and project.get("lender_name") and first:
        d = _d(first) + timedelta(days=CA_LIEN["prelim_notice_days_after_first_furnishing"])
        items.append(_item("prelim_notice", f"Preliminary notice to the construction lender ({project['lender_name']})",
                           d, today, lien_rag((d - today).days),
                           "Direct contractors serve a preliminary notice on the construction lender.",
                           project=project, verified=bool(CA_LIEN.get("verified")),
                           citations=[{"citation": CA_LIEN["sources"]["preliminary_notice"]}]))
    return items, notes


def _human_date(iso: str | None) -> str:
    d = _d(iso)
    return f"{d:%b} {d.day}, {d.year}" if d else str(iso)


def toolbox_item(project: dict, last_talk: str | None, today: date) -> dict | None:
    if project.get("status") != "active":
        return None
    start = _d(project.get("start_date")) or _d(project.get("created"))
    if start and start > today:
        return None
    interval = _toolbox_interval()
    base = _d(last_talk) or start or today
    try:
        due = add_working_days(base, interval)
    except OverflowError:
        return _item("data_problem", "Check this project's toolbox talk dates", today, today, "amber",
                     f"A toolbox talk is logged with an impossible date ({last_talk}). Fix or delete that log.",
                     project=project)
    left = working_days_between(today, due)
    cites, verified, checked = _rule_citation(TOOLBOX_RULE)
    if left < 0:
        title = "Toolbox talk overdue" if last_talk else "No toolbox talk logged yet"
        rag = "red"
    else:
        title, rag = "Next toolbox talk due", ("amber" if left <= 2 else "green")
    detail = (f"Cal/OSHA requires toolbox/tailgate safety meetings at least every {interval} working days. "
              + (f"Last one logged {_human_date(last_talk)}." if last_talk else "None logged for this project.")
              + " Working days count Monday-Friday; holidays are not excluded.")
    return _item("toolbox_talk", title, due, today, rag, detail, project=project, verified=verified,
                 citations=cites, checked_on=checked)


# ---------------------------------------------------------------------------- dashboard
def build(store, tenant: dict, today: date | None = None, include_past: bool = False) -> dict:
    today = today or logic.today_ca()
    tid = tenant["id"]
    company = license_card(tenant.get("cslb_license"))
    projects = store.list("projects", tid, order="id asc")
    subs = store.list("subs", tid, order="name asc")
    items: list[dict] = []
    project_notes: dict[int, list[str]] = {}

    lic = company.get("license") or {}
    if lic.get("found") and lic.get("expiration_date"):
        d = _d(lic["expiration_date"])
        if d and (d - today).days <= 60:
            cites, _, checked = _rule_citation(LICENSE_RULE)
            items.append(_item("license_expiry", "Your CSLB license expires", d, today,
                               rag_for_days((d - today).days, 60), "Renew with CSLB before this date.",
                               verified=bool(lic.get("verified")), citations=cites, checked_on=checked))

    for p in projects:
        its, notes = project_deadlines(p, today)
        items.extend(its)
        project_notes[p["id"]] = notes
    last_talks = store.last_field_log_dates(tid, "toolbox_talk")
    for p in projects:
        it = toolbox_item(p, last_talks.get(p["id"]), today)
        if it:
            items.append(it)

    sub_rows = []
    for s in subs:
        chk = sub_check(s)
        sub_rows.append(chk)
        coi = _d(s.get("coi_expiry"))
        if coi and (coi - today).days <= 30:
            items.append(_item("coi_expiry", "Sub insurance certificate (COI) expires" if coi >= today
                               else "Sub insurance certificate (COI) expired", coi, today,
                               "red" if coi < today else "amber",
                               "Get a current certificate of liability insurance before they keep working.", sub=s))
        sl = chk.get("license") or {}
        exp = _d(sl.get("expiration_date"))
        if sl.get("found") and exp and (exp - today).days <= 60:
            cites, _, checked = _rule_citation(LICENSE_RULE)
            items.append(_item("license_expiry", "Sub's CSLB license expires" if exp >= today
                               else "Sub's CSLB license expired", exp, today, rag_for_days((exp - today).days, 60),
                               "An expired license means they can't legally do the work.", sub=s,
                               verified=chk.get("verified", False), citations=cites, checked_on=checked))

    if not include_past:
        items = [i for i in items if i["days_left"] >= -PAST_WINDOW_DAYS or i["kind"] in
                 ("toolbox_talk", "coi_expiry", "data_problem")]
    items.sort(key=lambda i: (i["date"], RAG_ORDER[i["rag"]], i["title"]))
    counts = {k: sum(1 for i in items if i["rag"] == k) for k in RAG_ORDER}
    sub_counts = {k: sum(1 for s in sub_rows if s["rag"] == k) for k in RAG_ORDER}
    overall = {k: counts[k] + sub_counts[k] + (1 if company["rag"] == k else 0) for k in RAG_ORDER}
    return {"today": today.isoformat(), "company": company, "items": items, "counts": counts,
            "subs": sub_rows, "sub_counts": sub_counts, "overall": overall,
            "projects": [{"id": p["id"], "name": p["name"], "status": p["status"],
                          "public_or_private": p["public_or_private"], "notes": project_notes.get(p["id"], [])}
                         for p in projects]}


# ---------------------------------------------------------------------------- iCalendar
def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace(
        "\n", "\\n")


def _fold(line: str) -> str:
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def to_ics(items: list[dict], company_name: str, stamp: str) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TradeComply CA//Compliance deadlines//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_escape('TradeComply: ' + company_name)}"]
    for it in items:
        d = date.fromisoformat(it["date"])
        if d >= date.max:
            continue
        where = " / ".join(filter(None, [it.get("project_name"), it.get("sub_name")]))
        summary = f"{it['title']}" + (f" - {where}" if where else "")
        desc = it.get("detail", "")
        if it.get("citations"):
            desc += "\nSource: " + "; ".join(filter(None, (c.get("citation") for c in it["citations"])))
        desc += "\n" + ("Verified against official source" + (f" on {it['checked_on']}" if it.get("checked_on") else "")
                        if it.get("verified") else "Not yet verified") + ". Not legal advice."
        uid = it["key"].replace(":", "-") + "@tradecomply"
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
                  f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
                  f"SUMMARY:{_ics_escape(summary)}", f"DESCRIPTION:{_ics_escape(desc)}",
                  "TRANSP:TRANSPARENT"]
        if it["kind"] in ("prelim_notice", "lien_recording", "lien_foreclosure", "license_expiry", "coi_expiry"):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(summary)}", "TRIGGER:-P7D",
                      "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
