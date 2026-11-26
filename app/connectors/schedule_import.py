"""Schedule import: read a Primavera P6 (XER or P6 XML) or Microsoft Project (MSPDI XML) schedule export, find the
dates that drive California lien and notice deadlines, put them on the project, and return the deadline list.

What it reads (no network, no vendor account: the customer exports a file and uploads it):

Microsoft Project XML (MSPDI), File > Save As > XML Format (*.xml). From Microsoft's Project XML Data Interchange
reference (github.com/MicrosoftDocs/office-developer-msproject-xml-docs, the source of learn.microsoft.com
/office-project/xml-data-interchange, read this session): the root element is Project (XSD targetNamespace
http://schemas.microsoft.com/project/2007 in the 2007 schema; any http://schemas.microsoft.com/project namespace is
accepted), with Name, Title, StartDate, FinishDate, StatusDate and Tasks/Task. Task children used: UID (xsd:integer,
required), ID, Name (max 512), WBS, OutlineNumber, OutlineLevel, Start, Finish, ActualStart, ActualFinish
(xsd:dateTime "YYYY-MM-DDTHH:MM:SS"), Milestone, Summary, IsNull (xsd:boolean, 1/0), PercentComplete. Nested
elements (Baseline, ExtendedAttribute, PredecessorLink, TimephasedData) are skipped, so a baseline Start never
overrides the task's own Start.

Primavera P6 XER (Oracle's tab-delimited export). From Oracle's XER Import/Export Data Map Guide (as summarized in
tradecomply/data/integrations.json; docs.oracle.com was not reachable this session): TASK.task_code (Activity ID),
task_name, target_start_date (Planned Start), act_start_date, act_end_date, early_start_date, clndr_id. The record
syntax (ERMHDR header line; %T table, %F field names, %R record, %E end; tab separated), the yyyy-MM-dd HH:mm date
form, target_end_date (Planned Finish), early_end_date, restart_date/reend_date, task_type values TT_Task, TT_Rsrc,
TT_LOE, TT_Mile, TT_FinMile, TT_WBS and status_code values TK_NotStart, TK_Active, TK_Complete come from MPXJ's
reader (github.com/joniles/mpxj: XerFile.java, ActivityTypeHelper.java, ActivityStatusHelper.java), a widely used
open-source implementation, not an Oracle spec: listed in `unconfirmed`. Text is read as UTF-8 when it decodes as
UTF-8, otherwise as Windows-1252 (MPXJ's default for XER).

Primavera P6 XML. Root APIBusinessObjects in namespace http://xmlns.oracle.com/Primavera/P6/V<version>/API/
BusinessObjects, Project (Id, Name, ObjectId, DataDate, PlannedStartDate, ScheduledFinishDate) and Activity (nested
in Project or at the root with ProjectObjectId): ObjectId, Id, Name, Type ("Task Dependent", "Resource Dependent",
"Level of Effort", "Start Milestone", "Finish Milestone", "WBS Summary"), Status ("Not Started", "In Progress",
"Completed"), PlannedStartDate, PlannedFinishDate, StartDate, FinishDate, ActualStartDate, ActualFinishDate
("yyyy-MM-ddTHH:mm:ss"). Element names from MPXJ's classes generated from Oracle's p6apibo.xsd (not publicly
downloadable); BaselineProject activities are skipped.

Safety: files are capped at the server's request size (8 MB by default). XML is parsed with expat directly: any
DOCTYPE, entity or notation declaration is refused (no XXE, no entity expansion), external entities and parameter
entities are never read, nesting depth and element count are capped, and only the few fields above are kept. A
.mpp, zip, PDF or other file gets a plain "export it as XML/XER" message. Nothing from the file is logged.

How dates are mapped (activity names, case-insensitive; a milestone's actual date when it has one):
  construction start   notice to proceed / NTP / mobilization / construction start / groundbreaking -> start_date
  first furnishing     "first furnishing" / "first delivery" milestone, or the earliest start of your activities
  substantial / final  "substantial completion", "final completion", "final acceptance", "project completion"
  completion           -> completion_date: the EARLIER of substantial and final completion, which can only make the
                          lien and stop payment notice deadlines earlier (safer); Civil Code 8180 completion (actual
                          completion, occupation or use with labor stopped, 60 days' cessation, or a recorded notice
                          after 30 days' cessation) may be later
  notice of completion "notice of completion" / "notice of cessation" / NOC -> noc_date
  last furnishing      "last furnishing" / "last day of work" milestone, or the latest finish of your activities
"Your activities": with activity_filter (words in your activity names, e.g. "electrical, lighting"), the matching
ones; with no filter, every activity when you are the direct contractor (a subcontractor's own first and last
furnishing dates can't be read from a whole-project schedule without one).

Only dates the schedule records as ACTUAL are saved to the project (start_date may also be the planned start), and by
default only into empty fields (apply=missing; apply=overwrite replaces, apply=none previews). Planned dates still
produce a projected deadline list, marked "projected". The deadline list runs the same rules as the MCP tools:
calculate_lien_deadline (tradecomply.logic.lien_deadlines, via app.dashboard.project_deadlines),
stop_payment_notice_deadline (tradecomply.payments) and project_setup_checklist (tradecomply.project_setup, when the
project has a contract price and a city or county).

HTTP API (`router`; app/main.py must include it before the catch-all MCP mount at "/", as for the Docusign and Procore
routers). The settings page's schedule_import panel finds it in /api/openapi.json (first POST path with "schedule"
and "import"; multipart field `file`; {project_id} gives a project picker):
  POST /api/integrations/schedule_import/projects/{project_id}/upload   [owner/office, subscribed]
       multipart: file (required); optional apply=missing|overwrite|none, claimant_role, activity_filter,
       source_project (XER/P6 XML with several projects), owner_type, is_home_improvement, residential_units
No vendor HTTP and no operator credentials (required_env is empty: always "configured on this server").
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from xml.parsers import expat
from zoneinfo import ZoneInfo

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from tradecomply import logic, payments, project_setup

from .. import dashboard
from ..auth import Principal, RateLimiter, office_up, platform_of
from ..billing import require_entitled
from . import ConfigField, Connector, ConnectorContext, TestResult

log = logging.getLogger("tradecomply.connectors.schedule_import")
CA_TZ = ZoneInfo("America/Los_Angeles")

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
MAX_ACTIVITIES = 50000
MAX_PROJECTS = 200
MAX_XML_DEPTH = 64
MAX_XML_ELEMENTS = 3_000_000
MAX_FIELD_CHARS = 2000
MIN_YEAR, MAX_YEAR = 1990, 2100
APPLY_MODES = ("missing", "overwrite", "none")
FORMAT_LABELS = {"mspdi": "Microsoft Project XML (MSPDI)", "p6_xml": "Primavera P6 XML", "p6_xer": "Primavera P6 XER"}
MSPDI_NS = "http://schemas.microsoft.com/project"
P6_NS_RE = re.compile(r"^http://xmlns\.oracle\.com/Primavera/P6(?:/[^/]+)*/API/BusinessObjects$")
DISCLAIMER = ("Dates come from your schedule file; deadlines from TradeComply's California rules. Check the dates "
              "against your daily logs and recorded documents. Not legal advice.")

_upload_limiter = RateLimiter(limit=30, window_s=60 * 60)       # uploads per tenant per hour


class ScheduleError(ValueError):
    """The file can't be read as a schedule. The message is safe to show (no file content beyond counts)."""


# ---------------------------------------------------------------------------- dates
_DT = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?"
                 r"\s*(Z|[+-]\d{2}:?\d{2})?\s*$")


def parse_date(value) -> date | None:
    """The calendar date of a schedule date/time. Times without a zone are the schedule's own local time (taken as
    is); a value with Z or an offset is converted to California time first. Out-of-range or malformed -> None."""
    m = _DT.match(str(value or ""))
    if not m:
        return None
    y, mo, d, hh, mm, ss, tz = m.groups()
    try:
        if tz:
            off = tz.replace(":", "")
            sign = 1 if off == "Z" or off[0] == "+" else -1
            minutes = 0 if off == "Z" else sign * (int(off[1:3]) * 60 + int(off[3:5]))
            dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0),
                          tzinfo=timezone(timedelta(minutes=minutes)))
            day = dt.astimezone(CA_TZ).date()
        else:
            day = date(int(y), int(mo), int(d))
    except (ValueError, OverflowError):
        return None
    return day if MIN_YEAR <= day.year <= MAX_YEAR else None


def _flag(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "-1")


# ---------------------------------------------------------------------------- the model
@dataclass
class Activity:
    uid: str
    code: str
    name: str
    kind: str = "task"                 # task | milestone | summary | loe
    status: Optional[str] = None       # not_started | in_progress | completed
    planned_start: Optional[date] = None
    planned_finish: Optional[date] = None
    start: Optional[date] = None       # the schedule's current (early/remaining) dates
    finish: Optional[date] = None
    actual_start: Optional[date] = None
    actual_finish: Optional[date] = None

    def anchor(self, which: str) -> tuple[date | None, str | None]:
        """(date, basis) for the activity's start or finish: actual > scheduled > planned. A milestone has one
        date, so either end falls back to the other."""
        if which == "start":
            chain = [(self.actual_start, "actual"), (self.start, "scheduled"), (self.planned_start, "planned")]
            if self.kind == "milestone":
                chain += [(self.actual_finish, "actual"), (self.finish, "scheduled"), (self.planned_finish, "planned")]
        else:
            chain = [(self.actual_finish, "actual")]
            if self.kind == "milestone":
                chain.append((self.actual_start, "actual"))
            chain += [(self.finish, "scheduled"), (self.planned_finish, "planned")]
            if self.kind == "milestone":
                chain += [(self.start, "scheduled"), (self.planned_start, "planned")]
        for d, basis in chain:
            if d:
                return d, basis
        return None, None

    def label(self) -> dict:
        return {"code": self.code[:60], "name": self.name[:200]}


@dataclass
class Schedule:
    format: str
    project_name: Optional[str] = None
    project_code: Optional[str] = None
    data_date: Optional[date] = None
    project_start: Optional[date] = None
    project_finish: Optional[date] = None
    activities: list[Activity] = field(default_factory=list)
    projects_in_file: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- safe XML
class _Forbidden(Exception):
    pass


def _local(name: str) -> tuple[str, str]:
    ns, _, local = name.rpartition("}")
    return ns, local


class _Collector:
    """Streams an XML document through expat and keeps only "records": elements matched by `is_record(path)` (path =
    tuple of local names from the root), each as {child local name: text} for its direct, text-only children.
    Records may nest (a P6 Project holds Activities): each carries "_id" and "_parent" (the enclosing record's _id).
    Text-only direct children of the root element go to root_fields."""

    def __init__(self, is_record, fields: set[str]):
        self.is_record, self.fields = is_record, fields
        self.path: list[str] = []
        self.root: str | None = None
        self.root_ns: str | None = None
        self.records: list[tuple[tuple[str, ...], dict]] = []
        self.root_fields: dict[str, str] = {}
        self._stack: list[tuple[int, dict]] = []      # (depth, record) of the open records
        self._field: tuple[int, str] | None = None
        self._buf: list[str] = []
        self._buf_len = 0
        self._elements = 0
        self._next_id = 0

    def start(self, name, attrs):
        ns, local = _local(name)
        self._elements += 1
        if self._elements > MAX_XML_ELEMENTS:
            raise ScheduleError("The file has too many XML elements to be a schedule export.")
        if self.root is None:
            self.root, self.root_ns = local, ns
        self.path.append(local)
        depth = len(self.path)
        if depth > MAX_XML_DEPTH:
            raise ScheduleError("The XML is nested too deeply to be a schedule export.")
        self._field = None                             # an element inside a field: the field isn't text-only
        if self.is_record(tuple(self.path)):
            self._next_id += 1
            parent = self._stack[-1][1]["_id"] if self._stack else None
            self._stack.append((depth, {"_id": self._next_id, "_parent": parent}))
            return
        owner_depth = self._stack[-1][0] if self._stack else 1
        if depth == owner_depth + 1 and local in self.fields:
            self._field, self._buf, self._buf_len = (depth, local), [], 0

    def data(self, text):
        if self._field is not None and self._buf_len < MAX_FIELD_CHARS:
            self._buf.append(text[:MAX_FIELD_CHARS - self._buf_len])
            self._buf_len += len(text)

    def end(self, name):
        depth = len(self.path)
        if self._field is not None and self._field[0] == depth:
            local = self._field[1]
            value = "".join(self._buf).strip()
            (self._stack[-1][1] if self._stack else self.root_fields).setdefault(local, value)
        self._field = None
        if self._stack and self._stack[-1][0] == depth:
            _, rec = self._stack.pop()
            self.records.append((tuple(self.path), rec))
            if len(self.records) > MAX_ACTIVITIES + MAX_PROJECTS:
                raise ScheduleError(f"The schedule has more than {MAX_ACTIVITIES} activities.")
        self.path.pop()


def _forbid(*_args):
    raise _Forbidden()


def parse_xml_safely(data: bytes, is_record, fields: set[str]) -> _Collector:
    """expat with every DTD feature refused: a DOCTYPE, ENTITY or NOTATION declaration, an external entity reference
    or an undefined entity stops the parse (defusedxml's forbid_dtd behavior, without the dependency)."""
    col = _Collector(is_record, fields)
    p = expat.ParserCreate(namespace_separator="}")
    p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    p.StartDoctypeDeclHandler = _forbid
    p.EntityDeclHandler = _forbid
    p.UnparsedEntityDeclHandler = _forbid
    p.NotationDeclHandler = _forbid
    p.ExternalEntityRefHandler = _forbid
    p.buffer_text = True
    p.StartElementHandler = col.start
    p.EndElementHandler = col.end
    p.CharacterDataHandler = col.data
    try:
        p.Parse(data, True)
    except _Forbidden:
        raise ScheduleError("The file declares a DTD or entities, which schedule exports never do, so it was not "
                            "read.") from None
    except expat.ExpatError as e:
        raise ScheduleError(f"The file is not well-formed XML (line {e.lineno}). Export it again from your "
                            "scheduling software.") from None
    except (UnicodeError, LookupError):
        raise ScheduleError("The file's character encoding can't be read. Export it again as UTF-8 XML.") from None
    if col.root is None:
        raise ScheduleError("The file has no XML content.")
    return col


# ---------------------------------------------------------------------------- Microsoft Project XML (MSPDI)
MSPDI_TASK_FIELDS = {"UID", "ID", "Name", "WBS", "OutlineNumber", "OutlineLevel", "Start", "Finish", "ActualStart",
                     "ActualFinish", "Milestone", "Summary", "IsNull", "Active", "PercentComplete"}
MSPDI_PROJECT_FIELDS = {"Name", "Title", "StartDate", "FinishDate", "StatusDate", "CurrentDate"}


def parse_mspdi(data: bytes) -> Schedule:
    col = parse_xml_safely(data, lambda path: path[-3:] == ("Project", "Tasks", "Task") and len(path) == 3,
                           MSPDI_TASK_FIELDS | MSPDI_PROJECT_FIELDS)
    if col.root != "Project" or not (col.root_ns or "").startswith(MSPDI_NS):
        raise ScheduleError("This XML is not a Microsoft Project XML (MSPDI) export.")
    rf = col.root_fields
    sched = Schedule("mspdi", project_name=(rf.get("Title") or rf.get("Name") or None),
                     data_date=parse_date(rf.get("StatusDate")), project_start=parse_date(rf.get("StartDate")),
                     project_finish=parse_date(rf.get("FinishDate")))
    for _, t in col.records:
        if _flag(t.get("IsNull")) or (t.get("Active") is not None and t.get("Active").strip() == "0"):
            continue
        if (t.get("UID") or "").strip() == "0" or (t.get("OutlineLevel") or "").strip() == "0":
            sched.project_start = sched.project_start or parse_date(t.get("Start"))
            sched.project_finish = sched.project_finish or parse_date(t.get("Finish"))
            continue                                   # the project summary task
        kind = "summary" if _flag(t.get("Summary")) else ("milestone" if _flag(t.get("Milestone")) else "task")
        a = Activity(uid=(t.get("UID") or "")[:40], code=(t.get("WBS") or t.get("OutlineNumber") or t.get("ID") or "")[:60],
                     name=(t.get("Name") or "")[:512], kind=kind, start=parse_date(t.get("Start")),
                     finish=parse_date(t.get("Finish")), actual_start=parse_date(t.get("ActualStart")),
                     actual_finish=parse_date(t.get("ActualFinish")))
        pct = (t.get("PercentComplete") or "").strip()
        a.status = ("completed" if pct == "100" or a.actual_finish else
                    "in_progress" if a.actual_start else "not_started")
        sched.activities.append(a)
    return sched


# ---------------------------------------------------------------------------- Primavera P6 XML
P6_ACTIVITY_FIELDS = {"ObjectId", "Id", "Name", "Type", "Status", "PlannedStartDate", "PlannedFinishDate", "StartDate",
                      "FinishDate", "ActualStartDate", "ActualFinishDate", "ProjectObjectId"}
P6_PROJECT_FIELDS = {"ObjectId", "Id", "Name", "DataDate", "PlannedStartDate", "ScheduledFinishDate", "StartDate",
                     "FinishDate"}
P6_TYPES = {"start milestone": "milestone", "finish milestone": "milestone", "level of effort": "loe",
            "wbs summary": "summary"}
P6_STATUS = {"not started": "not_started", "in progress": "in_progress", "completed": "completed"}


def _p6_record(path: tuple[str, ...]) -> bool:
    return (len(path) == 2 and path[1] in ("Project", "Activity")) or (len(path) == 3 and path[1:] == ("Project",
                                                                                                         "Activity"))


def parse_p6_xml(data: bytes, source_project: str | None = None) -> Schedule:
    col = parse_xml_safely(data, _p6_record, P6_ACTIVITY_FIELDS | P6_PROJECT_FIELDS)
    if col.root != "APIBusinessObjects" or not P6_NS_RE.match(col.root_ns or ""):
        raise ScheduleError("This XML is not a Primavera P6 XML export (APIBusinessObjects).")
    projects = [rec for path, rec in col.records if path[-1] == "Project"]
    by_id = {rec["_id"]: rec for rec in projects}
    activities = []
    for path, rec in col.records:
        if path[-1] != "Activity":
            continue
        parent = by_id.get(rec["_parent"])            # nested in a Project, else ProjectObjectId names it
        activities.append(((parent or {}).get("ObjectId") or rec.get("ProjectObjectId") or "", _p6_activity(rec)))
    return _choose_project(
        "p6_xml",
        [{"key": p.get("ObjectId") or p.get("Id") or "", "code": p.get("Id"), "name": p.get("Name"),
          "data_date": parse_date(p.get("DataDate")),
          "start": parse_date(p.get("PlannedStartDate") or p.get("StartDate")),
          "finish": parse_date(p.get("ScheduledFinishDate") or p.get("FinishDate"))} for p in projects],
        activities, source_project)


def _p6_activity(r: dict) -> Activity:
    return Activity(uid=(r.get("ObjectId") or "")[:40], code=(r.get("Id") or "")[:60], name=(r.get("Name") or "")[:512],
                    kind=P6_TYPES.get((r.get("Type") or "").strip().lower(), "task"),
                    status=P6_STATUS.get((r.get("Status") or "").strip().lower()),
                    planned_start=parse_date(r.get("PlannedStartDate")),
                    planned_finish=parse_date(r.get("PlannedFinishDate")), start=parse_date(r.get("StartDate")),
                    finish=parse_date(r.get("FinishDate")), actual_start=parse_date(r.get("ActualStartDate")),
                    actual_finish=parse_date(r.get("ActualFinishDate")))


# ---------------------------------------------------------------------------- Primavera P6 XER
XER_TYPES = {"TT_MILE": "milestone", "TT_FINMILE": "milestone", "TT_LOE": "loe", "TT_WBS": "summary"}
XER_STATUS = {"TK_NOTSTART": "not_started", "TK_ACTIVE": "in_progress", "TK_COMPLETE": "completed"}
XER_TABLES = {"PROJECT", "TASK"}


def _decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse_xer(data: bytes, source_project: str | None = None) -> Schedule:
    text = _decode_text(data)
    tables: dict[str, list[dict]] = {"PROJECT": [], "TASK": []}
    table, fields, header_seen, ended, skipped = None, None, False, False, 0
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if not header_seen:
            if not line.startswith("ERMHDR"):
                raise ScheduleError("This is not a Primavera P6 XER file (it must start with ERMHDR).")
            header_seen = True
            continue
        parts = line.split("\t")
        mark = parts[0]
        if mark == "%T":
            table, fields = (parts[1].strip().upper() if len(parts) > 1 else None), None
        elif mark == "%F":
            fields = [f.strip().lower() for f in parts[1:]]
        elif mark == "%R":
            if table in XER_TABLES and fields:
                values = parts[1:] + [""] * (len(fields) - len(parts) + 1)
                tables[table].append(dict(zip(fields, (v.strip() for v in values))))
                if len(tables["TASK"]) > MAX_ACTIVITIES or len(tables["PROJECT"]) > MAX_PROJECTS:
                    raise ScheduleError(f"The schedule has more than {MAX_ACTIVITIES} activities.")
        elif mark == "%E":
            ended = True
            break
        else:
            skipped += 1                               # continuation of a multi-line memo field, or junk
    if not header_seen:
        raise ScheduleError("The file is empty.")
    if not tables["TASK"]:
        raise ScheduleError("The XER file has no TASK table (activities). Export the project with its activities.")
    sched = _choose_project(
        "p6_xer",
        [{"key": p.get("proj_id") or "", "code": p.get("proj_short_name"), "name": p.get("proj_short_name"),
          "data_date": parse_date(p.get("last_recalc_date")), "start": parse_date(p.get("plan_start_date")),
          "finish": parse_date(p.get("scd_end_date") or p.get("plan_end_date"))} for p in tables["PROJECT"]],
        [(t.get("proj_id") or "", _xer_activity(t)) for t in tables["TASK"]], source_project)
    if not ended:
        sched.warnings.append("The XER file has no %E end line; it may have been cut off. Export it again if "
                              "activities are missing.")
    return sched


def _xer_activity(t: dict) -> Activity:
    return Activity(uid=(t.get("task_id") or "")[:40], code=(t.get("task_code") or "")[:60],
                    name=(t.get("task_name") or "")[:512],
                    kind=XER_TYPES.get((t.get("task_type") or "").strip().upper(), "task"),
                    status=XER_STATUS.get((t.get("status_code") or "").strip().upper()),
                    planned_start=parse_date(t.get("target_start_date")),
                    planned_finish=parse_date(t.get("target_end_date")),
                    start=parse_date(t.get("early_start_date") or t.get("restart_date")),
                    finish=parse_date(t.get("early_end_date") or t.get("reend_date")),
                    actual_start=parse_date(t.get("act_start_date")), actual_finish=parse_date(t.get("act_end_date")))


def _choose_project(fmt: str, projects: list[dict], activities: list[tuple[str, Activity]],
                    source_project: str | None) -> Schedule:
    """Pick one project from a file that can hold several (XER, P6 XML): the one named by source_project (its id or
    short name), else the one with the most activities."""
    counts: dict[str, int] = {}
    for key, _ in activities:
        counts[key] = counts.get(key, 0) + 1
    listed = [{"key": p["key"], "code": p.get("code"), "name": p.get("name"), "activities": counts.get(p["key"], 0)}
              for p in projects]
    chosen = None
    if source_project:
        want = source_project.strip().lower()
        chosen = next((p for p in projects if want in (str(p["key"]).lower(), str(p.get("code") or "").lower())),
                      None)
        if chosen is None:
            raise ScheduleError(f"No project {source_project[:60]!r} in this file. It has: "
                                + ", ".join(str(p.get("code") or p["key"]) for p in projects[:20]))
    elif projects:
        chosen = max(projects, key=lambda p: counts.get(p["key"], 0))
    key = chosen["key"] if chosen else None
    sched = Schedule(fmt, project_name=(chosen or {}).get("name"), project_code=(chosen or {}).get("code"),
                     data_date=(chosen or {}).get("data_date"), project_start=(chosen or {}).get("start"),
                     project_finish=(chosen or {}).get("finish"),
                     projects_in_file=[{k: v for k, v in p.items() if k != "key"} for p in listed])
    sched.activities = [a for k, a in activities if key is None or k == key or (not k and len(projects) <= 1)]
    if len(projects) > 1:
        sched.warnings.append(f"The file holds {len(projects)} projects; read {sched.project_code or key!s} "
                              f"({len(sched.activities)} activities). Send source_project to pick another.")
    return sched


# ---------------------------------------------------------------------------- detection
def parse_schedule(data: bytes, source_project: str | None = None) -> Schedule:
    """Detect the format and parse. Raises ScheduleError with a plain message."""
    if not data or not data.strip():
        raise ScheduleError("The file is empty.")
    head = data[:8]
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise ScheduleError("This looks like a Microsoft Project .mpp file. In Project choose File > Save As > XML "
                            "Format (*.xml) and upload that file.")
    if head.startswith(b"PK\x03\x04"):
        raise ScheduleError("This is a zip or Office file. Upload the .xer or .xml export itself, not a compressed "
                            "file or spreadsheet.")
    if head.startswith(b"%PDF"):
        raise ScheduleError("This is a PDF. Export the schedule from Primavera P6 as XER or P6 XML, or from "
                            "Microsoft Project as XML, and upload that.")
    stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith(b"ERMHDR"):
        sched = parse_xer(data, source_project)
    elif stripped.startswith(b"<") or data.startswith((b"\xff\xfe", b"\xfe\xff")):
        sniff = data[:4096].decode("utf-8", errors="ignore") if not data.startswith((b"\xff\xfe", b"\xfe\xff")) \
            else data[:8192].decode("utf-16", errors="ignore")
        if "APIBusinessObjects" in sniff:
            sched = parse_p6_xml(data, source_project)
        else:
            sched = parse_mspdi(data)
    else:
        raise ScheduleError("This file isn't a Primavera P6 (XER or XML) or Microsoft Project XML export.")
    if not sched.activities:
        raise ScheduleError("No activities were found in the schedule.")
    return sched


# ---------------------------------------------------------------------------- milestones -> project dates
PATTERNS = {
    "notice_of_completion": re.compile(r"\bnotice\s+of\s+(completion|cessation)\b|\bN\.?O\.?C\b", re.I),
    "substantial_completion": re.compile(r"\bsubstantial(ly)?\s+complet", re.I),
    "final_completion": re.compile(r"\bfinal\s+(completion|acceptance)\b|\bproject\s+(completion|complete)\b|"
                                   r"\bcompletion\s+of\s+(the\s+)?(work|project|contract)\b", re.I),
    "first_furnishing": re.compile(r"\bfirst\s+(day\s+of\s+)?(furnish\w*|deliver\w*)\b", re.I),
    "last_furnishing": re.compile(r"\blast\s+(day\s+of\s+)?(furnish\w*|deliver\w*|work)\b", re.I),
    "construction_start": re.compile(r"\bnotice\s+to\s+proceed\b|\bN\.?T\.?P\b|\bmobiliz\w*|"
                                     r"\b(construction|project|work)\s+start\w*|\bstart\s+(of\s+)?(construction|work)\b|"
                                     r"\bbreak(ing)?\s+ground\b|\bground\s*breaking\b", re.I),
}
ANCHOR = {"notice_of_completion": "finish", "substantial_completion": "finish", "final_completion": "finish",
          "first_furnishing": "start", "last_furnishing": "finish", "construction_start": "start"}
PICK_LATEST = {"notice_of_completion", "substantial_completion", "final_completion", "last_furnishing"}
MILESTONE_LABELS = {"construction_start": "Construction start / notice to proceed",
                    "first_furnishing": "First furnishing", "substantial_completion": "Substantial completion",
                    "final_completion": "Final completion / acceptance", "notice_of_completion": "Notice of completion",
                    "last_furnishing": "Last furnishing"}
FIELD_LABELS = {"start_date": "start date", "first_furnishing_date": "first furnishing date",
                "completion_date": "completion date", "noc_date": "notice of completion date",
                "last_furnishing_date": "last furnishing date"}
LIEN_FIELDS = ("first_furnishing_date", "last_furnishing_date", "completion_date", "noc_date")


@dataclass
class Found:
    date: date
    basis: str
    activity: Optional[Activity] = None
    how: str = ""
    matches: int = 1

    def view(self) -> dict:
        return {"date": self.date.isoformat(), "basis": self.basis, "how": self.how, "matches": self.matches,
                "activity": self.activity.label() if self.activity else None}


def _keywords(text: str | None) -> list[str]:
    return [w.strip().lower() for w in re.split(r"[,;\n]", text or "") if len(w.strip()) >= 2][:20]


def find_milestones(sched: Schedule, *, role: str, activity_filter: str | None, today: date) -> tuple[dict, list[str]]:
    """{key: Found} for the milestone keys above, plus warnings."""
    warnings: list[str] = []
    work = [a for a in sched.activities if a.kind in ("task", "milestone")]
    for a in work:                                     # an "actual" date in the future is a data error
        for attr in ("actual_start", "actual_finish"):
            d = getattr(a, attr)
            if d and d > today:
                setattr(a, attr, None)
                warnings.append(f"Activity {a.code or a.uid} has an actual date after today; it was treated as "
                                "planned.")
    found: dict[str, Found] = {}
    for key, pattern in PATTERNS.items():
        hits = []
        for a in work:
            if not pattern.search(a.name):
                continue
            if key in ("substantial_completion", "final_completion") and PATTERNS["notice_of_completion"].search(a.name):
                continue                               # "Record notice of completion" is not completion itself
            d, basis = a.anchor(ANCHOR[key])
            if d:
                hits.append((d, basis, a))
        if not hits:
            continue
        milestones = [h for h in hits if h[2].kind == "milestone"] or hits
        d, basis, a = (max if key in PICK_LATEST else min)(milestones, key=lambda h: h[0])
        found[key] = Found(d, basis, a, how=f"activity named like '{MILESTONE_LABELS[key].lower()}'",
                           matches=len(hits))
        if len(milestones) > 1 and key in ("substantial_completion", "notice_of_completion"):
            warnings.append(f"{len(milestones)} activities look like {MILESTONE_LABELS[key].lower()}; used the "
                            f"{'latest' if key in PICK_LATEST else 'earliest'} ({d.isoformat()}).")
    words = _keywords(activity_filter)
    mine = ([a for a in work if any(w in a.name.lower() or w in a.code.lower() for w in words)] if words
            else (work if role == "direct_contractor" else []))
    if words and not mine:
        warnings.append("No activity matched your activity filter, so your own first and last furnishing dates "
                        "could not be read.")
    elif not words and role != "direct_contractor" and not {"first_furnishing", "last_furnishing"} <= found.keys():
        warnings.append("You are not the direct contractor on this project: give an activity filter (words in your "
                        "activity names, e.g. 'electrical') so your own first and last furnishing dates can be read.")
    if mine:
        how = "your activities (filter)" if words else "all activities"
        if "first_furnishing" not in found:
            starts = [(a.anchor("start"), a) for a in mine if a.anchor("start")[0]]
            if starts:
                (d, basis), a = min(starts, key=lambda s: s[0][0])
                found["first_furnishing"] = Found(d, basis, a, how=f"earliest start of {how}", matches=len(mine))
        if "last_furnishing" not in found:
            ends = [(a.anchor("finish"), a) for a in mine if a.anchor("finish")[0]]
            if ends:
                (d, basis), a = max(ends, key=lambda s: s[0][0])
                if any(not x.actual_finish and not (x.kind == "milestone" and x.actual_start) for x in mine):
                    basis = "scheduled" if basis == "actual" else basis    # work remains: not the last day yet
                found["last_furnishing"] = Found(d, basis, a, how=f"latest finish of {how}", matches=len(mine))
    if "construction_start" not in found:
        starts = [(a.anchor("start"), a) for a in work if a.anchor("start")[0]]
        if starts:
            (d, basis), a = min(starts, key=lambda s: s[0][0])
            found["construction_start"] = Found(d, basis, a, how="earliest activity start", matches=len(work))
    return found, warnings


def propose_dates(found: dict) -> dict:
    """{project field: Found} for every field the schedule gives a date for (any basis)."""
    out = {}
    if "construction_start" in found:
        out["start_date"] = found["construction_start"]
    if "first_furnishing" in found:
        out["first_furnishing_date"] = found["first_furnishing"]
    completions = [found[k] for k in ("substantial_completion", "final_completion") if k in found]
    actual = [f for f in completions if f.basis == "actual"]
    if actual or completions:
        out["completion_date"] = min(actual or completions, key=lambda f: f.date)   # earlier = safer deadlines
    if "notice_of_completion" in found:
        out["noc_date"] = found["notice_of_completion"]
    if "last_furnishing" in found:
        out["last_furnishing_date"] = found["last_furnishing"]
    return out


def plan_apply(row: dict, proposed: dict, mode: str, today: date) -> tuple[dict, dict, dict, list[str]]:
    """What to write to the project: (values, applied {field: iso}, kept {field: {...}}, problems). Only actual dates
    (start_date: any) go in; `missing` fills empty fields, `overwrite` replaces, `none` writes nothing. A merged set of
    dates the lien rules reject (e.g. first furnishing after last) is not written."""
    values, applied, kept, problems = {}, {}, {}, []
    if mode == "none":
        return values, applied, kept, problems
    for fld, f in proposed.items():
        if fld != "start_date" and f.basis != "actual":
            continue
        new, cur = f.date.isoformat(), (row.get(fld) or None)
        if cur == new:
            continue
        if cur and mode == "missing":
            kept[fld] = {"project": cur, "schedule": new, "basis": f.basis}
            continue
        values[fld] = new
    if values:
        merged = {**row, **values}
        its, _ = dashboard.project_deadlines(merged, today)
        bad = [i for i in its if i["kind"] == "data_problem"]
        if bad:
            problems.append("Not saved: with the schedule's dates the project would have conflicting dates ("
                            + bad[0]["detail"] + ").")
            return {}, {}, kept, problems
        applied = dict(values)
    return values, applied, kept, problems


# ---------------------------------------------------------------------------- deadlines
def _deadline(kind, title, day, basis, tool, detail="", citations=(), verified=False) -> dict:
    return {"kind": kind, "title": title, "date": day if isinstance(day, str) else day.isoformat(), "basis": basis,
            "source_tool": tool, "detail": detail, "citations": [c for c in citations if c][:6],
            "verified": bool(verified)}


def deadlines_for(row: dict, proposed: dict, today: date, setup_args: dict) -> tuple[list[dict], dict, list[str]]:
    """The deadline list for the project's dates, with any date the project still lacks taken from the schedule
    (then marked projected). Returns (deadlines, project_setup summary, notes)."""
    notes: list[str] = []
    eff, projected = dict(row), []
    for fld, f in proposed.items():
        if not eff.get(fld):
            eff[fld] = f.date.isoformat()
            if fld in LIEN_FIELDS or fld == "start_date":
                projected.append(fld)
    lien_basis = "projected" if any(f in projected for f in LIEN_FIELDS) else "project dates"
    out: list[dict] = []
    its, lien_notes = dashboard.project_deadlines(eff, today)       # calculate_lien_deadline's rules
    notes.extend(lien_notes)
    for it in its:
        if it["kind"] == "data_problem":
            notes.append(it["detail"])
            continue
        out.append(_deadline(it["kind"], it["title"], it["date"], lien_basis, "calculate_lien_deadline",
                             it.get("detail") or "", [c.get("citation") for c in it.get("citations") or []],
                             it.get("verified")))
    role = (eff.get("extra") or {}).get("claimant_role") or "direct_contractor"
    if role in payments.Claimant.__args__ and (eff.get("completion_date") or eff.get("noc_date")
                                               or eff.get("last_furnishing_date")):
        try:
            r = payments.stop_payment_notice_deadline(
                "public" if eff.get("public_or_private") == "public" else "private", role,
                completion_date=eff.get("completion_date") or None, noc_recorded_date=eff.get("noc_date") or None,
                last_furnishing_date=eff.get("last_furnishing_date") or None,
                first_furnishing_date=eff.get("first_furnishing_date") or None, as_of=today,
                construction_lender=bool(eff.get("lender_name")))
            if r.applicable and r.deadline:
                out.append(_deadline("stop_payment_notice", "Last day to give a stop payment notice", r.deadline,
                                     lien_basis, "stop_payment_notice_deadline",
                                     f"Rule applied: {r.deadline_rule_applied}." + (
                                         f" Act by {r.act_by_date}." if r.act_by_date != r.deadline else ""),
                                     [c.citation for c in r.citations], r.verified))
            elif not r.applicable:
                notes.append("Stop payment notice: not available to you on this project (see "
                             "stop_payment_notice_deadline).")
        except ValueError as e:
            notes.append(f"Stop payment notice: {e}")
    setup = run_project_setup(eff, role, today, setup_args, "start_date" in projected)
    for item in setup.pop("_items", []):
        out.append(item)
    out.sort(key=lambda d: (d["date"], d["kind"], d["title"]))
    return out, setup, notes


def run_project_setup(eff: dict, role: str, today: date, args: dict, start_projected: bool) -> dict:
    summary = {"ran": False, "assumptions": [], "notes": []}
    if role not in ("direct_contractor", "subcontractor"):
        summary["notes"].append("The project setup checklist is for contractors; skipped for this role.")
        return summary
    price = eff.get("contract_price")
    if not price or price <= 0 or not (eff.get("city") or eff.get("county")):
        summary["notes"].append("Add the contract price and the city or county to the project to get the dated "
                                "project setup checklist (permits, Cal/OSHA, notices) from these schedule dates.")
        return summary
    extra = eff.get("extra") or {}
    public = eff.get("public_or_private") == "public"
    owner = args.get("owner_type") or extra.get("owner_type") or ("public_agency" if public else None)
    if owner not in project_setup.OwnerType.__args__:
        owner = "private_company"
        summary["assumptions"].append("Owner type not given: assumed a private company (send owner_type).")
    hic = args.get("is_home_improvement")
    hic = bool(extra.get("is_home_improvement")) if hic is None else bool(hic)
    units = args.get("residential_units")
    if units is None:
        units = extra.get("residential_units")
    try:
        units = max(0, int(units or 0))
    except (TypeError, ValueError):
        units = 0
    if hic and units < 1:
        units = 1
        summary["assumptions"].append("Home improvement work: assumed 1 dwelling unit.")
    if units == 0 and args.get("residential_units") is None and extra.get("residential_units") is None:
        summary["assumptions"].append("Residential units not given: assumed a nonresidential building (send "
                                      "residential_units).")
    end = eff.get("completion_date") or None
    start = eff.get("start_date") or None
    if start and end and end < start:
        end = None
    try:
        r = project_setup.project_setup_checklist(
            eff.get("name") or "Project", eff.get("city") or "", eff.get("county") or "", owner, hic, units,
            float(price), start_date=start, contractor_role=role, has_construction_lender=bool(eff.get("lender_name")),
            as_of=today, estimated_completion_date=end)
    except ValueError as e:
        summary["notes"].append(f"Project setup checklist not run: {e}")
        return summary
    basis = "projected" if start_projected else "project dates"
    items = [_deadline("project_setup", i.title, i.due_date, basis, "project_setup_checklist", i.why[:500],
                       [i.citation], i.verified) for ph in r.phases if ph.applies for i in ph.items if i.due_date]
    summary.update(ran=True, assumptions=summary["assumptions"] + list(r.assumptions)[:10], items_dated=len(items),
                   owner_type=owner, home_improvement=hic, residential_units=units, _items=items[:80])
    return summary


# ---------------------------------------------------------------------------- the whole import
def import_schedule(store, tenant_id: str, project_id: int, data: bytes, *, mode: str = "missing",
                    claimant_role: str | None = None, activity_filter: str | None = None,
                    source_project: str | None = None, setup_args: dict | None = None,
                    today: date | None = None, file_name: str | None = None) -> dict | None:
    """Parse, map, apply (per mode) and compute deadlines for one project of one tenant. None if the project isn't
    this tenant's. Raises ScheduleError for an unreadable file."""
    today = today or logic.today_ca()
    if mode not in APPLY_MODES:
        raise ScheduleError(f"apply must be one of {', '.join(APPLY_MODES)}")
    if claimant_role is not None and claimant_role not in dashboard.CLAIMANT_ROLES:
        raise ScheduleError(f"claimant_role must be one of {', '.join(dashboard.CLAIMANT_ROLES)}")
    row = store.get("projects", tenant_id, project_id)
    if not row:
        return None
    sched = parse_schedule(data, source_project)
    role = claimant_role or (row.get("extra") or {}).get("claimant_role") or "direct_contractor"
    if role not in dashboard.CLAIMANT_ROLES:
        role = "direct_contractor"
    found, warnings = find_milestones(sched, role=role, activity_filter=activity_filter, today=today)
    proposed = propose_dates(found)
    result: dict = {}

    def change(current: dict) -> dict:
        values, applied, kept, problems = plan_apply(current, proposed, mode, today)
        result.update(applied=applied, kept=kept, problems=problems)
        if claimant_role and mode != "none":
            values.setdefault("extra", dict(current.get("extra") or {}))["claimant_role"] = claimant_role
        if mode != "none" and (applied or claimant_role):
            extra = dict(values.get("extra") or current.get("extra") or {})
            extra["schedule_import"] = {
                "format": sched.format, "imported": datetime.now(CA_TZ).isoformat(timespec="seconds"),
                "source_project": (sched.project_code or sched.project_name or "")[:100],
                "activities": len(sched.activities), "applied": sorted(applied),
                "milestones": {k: {"date": f.date.isoformat(), "basis": f.basis} for k, f in found.items()}}
            values["extra"] = extra
        return values

    if mode == "none":
        change(row)
        updated = row
    else:
        updated = store.modify("projects", tenant_id, project_id, change)
        if updated is None:
            return None
    if claimant_role and mode == "none":
        updated = {**updated, "extra": {**(updated.get("extra") or {}), "claimant_role": claimant_role}}
    deadlines, setup, notes = deadlines_for(updated, proposed, today, setup_args or {})
    warnings = sched.warnings + warnings + result.get("problems", [])
    applied = result.get("applied", {})
    label = FORMAT_LABELS[sched.format]
    msg = f"Read {len(sched.activities)} activities from the {label} file"
    msg += f" (project {sched.project_code or sched.project_name})." if (sched.project_code or sched.project_name) \
        else "."
    if mode == "none":
        msg += " Preview only: nothing was saved."
    elif applied:
        msg += " Saved to the project: " + ", ".join(FIELD_LABELS[f] for f in applied) + "."
    else:
        msg += " No project dates changed."
    if result.get("kept"):
        msg += f" Kept {len(result['kept'])} date(s) you had already entered (send apply=overwrite to replace them)."
    projected = sum(1 for d in deadlines if d["basis"] == "projected")
    msg += f" {len(deadlines)} deadline(s)" + (f", {projected} projected from planned dates." if projected else ".")
    return {
        "message": msg, "project_id": project_id, "apply_mode": mode, "claimant_role": role,
        "file": {"format": sched.format, "format_label": label, "name": (file_name or "")[:120] or None,
                 "project_name": sched.project_name, "project_code": sched.project_code,
                 "data_date": sched.data_date.isoformat() if sched.data_date else None,
                 "project_start": sched.project_start.isoformat() if sched.project_start else None,
                 "project_finish": sched.project_finish.isoformat() if sched.project_finish else None,
                 "activities": len(sched.activities),
                 "milestone_activities": sum(1 for a in sched.activities if a.kind == "milestone"),
                 "projects_in_file": sched.projects_in_file[:MAX_PROJECTS]},
        "milestones": {k: f.view() for k, f in found.items()},
        "proposed": {fld: {"value": f.date.isoformat(), "basis": f.basis, "from": MILESTONE_LABELS.get(
            next((k for k, v in found.items() if v is f), ""), ""), "saved_if_applied": fld == "start_date"
            or f.basis == "actual"} for fld, f in proposed.items()},
        "applied": applied, "kept": result.get("kept", {}),
        "project": {k: updated.get(k) for k in ("id", "name", "start_date", "first_furnishing_date",
                                                 "last_furnishing_date", "completion_date", "noc_date",
                                                 "public_or_private")},
        "deadlines": deadlines, "project_setup": setup, "warnings": warnings[:50], "notes": notes[:50],
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------- the connector
_SAMPLE_XER = ("ERMHDR\t19.12\t2026-11-20\tProject\tadmin\tAdmin\tdbxDatabaseNoName\tProject Management\tUSD\n"
               "%T\tTASK\n%F\ttask_id\tproj_id\ttask_code\ttask_name\ttask_type\tstatus_code\ttarget_start_date\n"
               "%R\t1\t1\tA1000\tNotice to Proceed\tTT_Mile\tTK_NotStart\t2026-12-07 08:00\n%E\n").encode()


class ScheduleImportConnector(Connector):
    provider = "schedule_import"
    display_name = "Schedule import (Primavera P6, Microsoft Project)"
    catalog_id = "oracle-primavera"
    description = ("Upload a Primavera P6 (XER or XML) or Microsoft Project (XML) schedule export. TradeComply reads "
                   "the start, completion, notice of completion and furnishing dates, saves the actual ones to the "
                   "project and lists the lien, stop payment notice and project setup deadlines.")
    category = "project_management"
    docs_url = "https://learn.microsoft.com/en-us/office-project/xml-data-interchange/task-element"
    required_env = ()
    config_fields = (
        ConfigField("apply_mode", "Saving dates to the project", type="select", default="missing",
                    options=(("missing", "Fill in dates the project doesn't have yet"),
                             ("overwrite", "Replace project dates with the schedule's actual dates"),
                             ("none", "Preview only; never save")),
                    help="Only dates the schedule records as actual are saved (the start date may be planned)."),
        ConfigField("activity_filter", "Words in your activity names (subcontractors)", max_length=200,
                    placeholder="electrical, lighting, switchgear",
                    help="Used to find your own first and last furnishing dates in the GC's schedule."),
        ConfigField("owner_type", "Usual owner type (project setup checklist)", type="select",
                    options=(("private_individual", "Homeowner / individual"), ("private_company", "Private company"),
                             ("public_agency", "Public agency"))),
    )
    unconfirmed = (
        "Primavera XER record syntax (ERMHDR, %T/%F/%R/%E), the yyyy-MM-dd HH:mm date form, target_end_date, "
        "early_end_date, task_type and status_code values come from MPXJ's open-source reader, not an Oracle spec "
        "(docs.oracle.com could not be read). Confirm against your own export.",
        "P6 XML element names come from MPXJ's classes generated from Oracle's p6apibo.xsd; the XML namespace changes "
        "with the P6 version.",
        "Microsoft Project exports are read in any http://schemas.microsoft.com/project namespace; the 2007 schema "
        "documents http://schemas.microsoft.com/project/2007. Task Active (inactive tasks, Project 2010+) is not in the "
        "2007 schema pages read; inactive tasks are skipped when it is 0.",
        "Activity names are matched by wording (notice to proceed, substantial completion, notice of completion, "
        "first/last furnishing); check the milestones TradeComply found before relying on the dates.",
    )

    def test(self) -> TestResult:
        sched = parse_schedule(_SAMPLE_XER)
        ok = len(sched.activities) == 1 and sched.activities[0].planned_start == date(2026, 12, 7)
        return TestResult(ok, "Schedule import is ready. Upload a Primavera P6 XER or XML file or a Microsoft Project "
                              "XML file on a project." if ok else "The schedule reader failed its self-check.",
                          details={"formats": list(FORMAT_LABELS.values()), "max_file_mb": DEFAULT_MAX_BYTES // 2 ** 20})


# ---------------------------------------------------------------------------- HTTP route
router = APIRouter(tags=["integrations"])


def _bool_form(value: str | None) -> bool | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise HTTPException(422, "is_home_improvement must be true or false")


@router.post("/api/integrations/schedule_import/projects/{project_id}/upload")
async def upload_schedule(project_id: int, request: Request, file: UploadFile = File(...),
                          apply: Optional[str] = Form(None), claimant_role: Optional[str] = Form(None),
                          activity_filter: Optional[str] = Form(None, max_length=200),
                          source_project: Optional[str] = Form(None, max_length=100),
                          owner_type: Optional[str] = Form(None), is_home_improvement: Optional[str] = Form(None),
                          residential_units: Optional[int] = Form(None, ge=0, le=100000),
                          p: Principal = Depends(office_up)):
    """Upload a schedule export (P6 XER, P6 XML or Microsoft Project XML) for one project: returns the milestones
    found, the dates saved to the project and the deadline list (lien, stop payment notice, project setup)."""
    platform = platform_of(request)
    await anyio.to_thread.run_sync(require_entitled, platform, p)
    key = f"tenant|{p.tenant_id}"
    wait = _upload_limiter.retry_after(key)
    if wait:
        raise HTTPException(429, f"Too many schedule uploads in an hour. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    if owner_type is not None and owner_type not in project_setup.OwnerType.__args__:
        raise HTTPException(422, f"owner_type must be one of {', '.join(project_setup.OwnerType.__args__)}")
    limit = min(platform.settings.max_body_bytes or DEFAULT_MAX_BYTES, 64 * 1024 * 1024)
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"The schedule file is larger than {limit // 2 ** 20} MB.")
    _upload_limiter.hit(key)
    inst = ScheduleImportConnector(ConnectorContext(platform, p.tenant_id, p.user_id, p.label))
    config = await anyio.to_thread.run_sync(inst.config)
    mode = (apply or config.get("apply_mode") or "missing").strip().lower()
    hic = _bool_form(is_home_improvement)
    setup_args = {"owner_type": owner_type or config.get("owner_type"), "is_home_improvement": hic,
                  "residential_units": residential_units}
    try:
        result = await anyio.to_thread.run_sync(lambda: import_schedule(
            platform.store, p.tenant_id, project_id, data, mode=mode, claimant_role=claimant_role or None,
            activity_filter=activity_filter if activity_filter is not None else config.get("activity_filter"),
            source_project=source_project or None, setup_args=setup_args, file_name=file.filename))
    except ScheduleError as e:
        _audit(platform, p, {"project_id": project_id, "error": "unreadable"}, ok=False)
        raise HTTPException(422, str(e))
    if result is None:
        raise HTTPException(404, "Project not found")
    _audit(platform, p, {"project_id": project_id, "format": result["file"]["format"],
                         "activities": result["file"]["activities"], "apply": mode,
                         "applied": sorted(result["applied"])})
    if result["applied"] and inst.status()["status"] != "connected":
        await anyio.to_thread.run_sync(lambda: inst.save(status="connected", last_error=None))
    return result


def _audit(platform, p: Principal, args: dict, ok: bool = True) -> None:
    try:
        platform.store.audit(p.tenant_id, p.label, "integrations.schedule_import", args, None, ok=ok,
                             source="integrations")
    except Exception:      # noqa: BLE001 - the import already happened
        log.exception("schedule import: audit write failed")
