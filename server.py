"""TradeComply CA: MCP server for California construction trade compliance.

TradeComply CA: California construction compliance tools for AI agents (licenses, liens, payments, payroll, safety,
public works, permits, contracts, business filings, project setup, jobsite safety, HR mandates, accessibility,
equipment emissions, city jobsite rules, worker certifications, federal projects and a searchable verified rulebook).

Values come from tradecomply/data/rulebook.json (checked against official sources) and tradecomply/rules.py. Every
result carries `verified`, citations and a disclaimer. Not legal advice.

Run locally:      python server.py
Inspect tools:    fastmcp dev server.py
HTTP transport:   MCP_TRANSPORT=http python server.py     (PORT, default 8000; endpoint /mcp)
"""
import importlib
import os
from typing import Annotated, Optional

from fastmcp import FastMCP
from pydantic import Field

from tradecomply import logic
from tradecomply.rules import (CA_LIEN, CA_STATE_PERMIT_FEES, CAL_OSHA_PENALTY_MAX_USD, CSLB, DISCLAIMER,
                               FEDERAL_PENALTY_MAX_USD, SAN_DIEGO_FEES)

mcp = FastMCP(
    name="TradeComply CA",
    instructions=(
        "California construction compliance tools. Always show the user the returned disclaimer. Never present output "
        "as legal advice. When a result has verified=false, say the data is unconfirmed and point to the official "
        "source or link in the result. For lien deadlines, tell the user to act before the date, not on it."
    ),
)

READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


@mcp.tool(annotations=READ_ONLY)
def verify_contractor_license(
    license_number: Annotated[str, Field(description="California CSLB license number, digits only")],
) -> logic.LicenseResult:
    """Look up a California contractor license: status, classifications, expiration, workers' comp and bond."""
    return logic.verify_license(license_number)


@mcp.tool(annotations=READ_ONLY)
def estimate_permit_fees(
    jurisdiction: Annotated[str, Field(description="City, e.g. 'San Diego'. Only cities with a verified schedule get a number")],
    project_type: Annotated[logic.ProjectType, Field(description="Kind of building work")],
    square_footage: Annotated[float, Field(gt=0, description="Project area in sq ft, including exterior walls")],
    valuation_usd: Annotated[float, Field(gt=0, description="Construction valuation (drives the state fees)")],
    stories: Annotated[int, Field(ge=1, description="Number of stories (affects the state seismic fee)")] = 1,
    built_before_1978: Annotated[bool, Field(description="Existing structure built before 1978 (lead fee)")] = False,
) -> logic.PermitFeeResult:
    """Building permit fee estimate with line items. San Diego uses the official IB-501 tables; other cities get a
    link to their official calculator instead of a guess."""
    return logic.estimate_permit_fees(jurisdiction, project_type, square_footage, valuation_usd, stories,
                                      built_before_1978)


@mcp.tool(annotations=READ_ONLY)
def lookup_safety_standard(
    hazard: Annotated[str, Field(description="Hazard or task, e.g. 'fall protection', 'trench', 'silica'")],
) -> logic.SafetyResult:
    """Find the Cal/OSHA (Title 8) construction safety rule for a hazard, with the federal reference and PPE."""
    return logic.lookup_safety_standard(hazard)


@mcp.tool(annotations=READ_ONLY)
def calculate_lien_deadline(
    claimant_role: Annotated[logic.Role, Field(description="Who is claiming the lien")],
    last_furnishing_date: Annotated[str, Field(description="ISO date you last furnished labor or materials")],
    first_furnishing_date: Annotated[Optional[str], Field(description="ISO date you FIRST furnished (for the 20-day preliminary notice)")] = None,
    completion_date: Annotated[Optional[str], Field(description="ISO date the whole project was completed, if known")] = None,
    notice_of_completion_date: Annotated[Optional[str], Field(description="ISO date a notice of completion or notice of cessation was recorded, if any")] = None,
    construction_start_date: Annotated[Optional[str], Field(description="ISO date construction started (design professionals only)")] = None,
) -> logic.LienDeadlineResult:
    """California mechanics lien deadlines: preliminary notice, lien recording, and foreclosure, with days left."""
    return logic.lien_deadlines(claimant_role, last_furnishing_date, first_furnishing_date, completion_date,
                                notice_of_completion_date, construction_start_date=construction_start_date)


@mcp.tool(annotations=READ_ONLY)
def check_subcontractor_compliance(
    license_number: Annotated[str, Field(description="Subcontractor's CSLB license number")],
    required_classification: Annotated[Optional[str], Field(description="License class the work needs, e.g. 'C-10'")] = None,
    liability_insurance_expiry: Annotated[Optional[str], Field(description="ISO expiry date from their insurance certificate")] = None,
    will_have_employees_on_site: Annotated[bool, Field(description="Will they bring employees? Affects workers' comp")] = True,
) -> logic.SubComplianceResult:
    """Prequalify a subcontractor: license, classification, insurance, workers' comp and bond, with a risk rating."""
    return logic.check_subcontractor(license_number, required_classification, liability_insurance_expiry,
                                     will_have_employees_on_site)


MODULES = ("rulebook_tools", "licensing", "contracts", "payments", "payroll", "safety_programs", "public_works",
           "building", "business_ops", "project_setup", "jobsite_safety", "hr_compliance", "accessibility",
           "equipment_emissions", "local_rules", "trade_certifications", "federal_projects")
for _name in MODULES:
    importlib.import_module(f"tradecomply.{_name}").register(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    from starlette.responses import JSONResponse
    return JSONResponse({"ok": True, "server": "TradeComply CA"})


@mcp.resource("tradecomply://disclaimer")
def disclaimer() -> str:
    """Disclaimer and which data sets have been verified."""
    rows = [("CA lien rules", CA_LIEN), ("CSLB constants", CSLB), ("San Diego permit fees", SAN_DIEGO_FEES), ("CA state permit fees", CA_STATE_PERMIT_FEES),
            ("Federal OSHA penalties", FEDERAL_PENALTY_MAX_USD), ("Cal/OSHA penalties", CAL_OSHA_PENALTY_MAX_USD)]
    status = "\n".join(f"- {name}: {'verified ' + str(d.get('checked_on')) if d.get('verified') else 'UNVERIFIED'}"
                       f"{' (last checked ' + d['checked_on'] + ')' if d.get('checked_on') and not d.get('verified') else ''}"
                       for name, d in rows)
    return (f"{DISCLAIMER}\n\nData status:\n{status}\n- Cal/OSHA safety table: fall protection, silica, excavation, "
            "ladders verified 2026-11-25 against dir.ca.gov Title 8; scaffold bullets UNVERIFIED.")


if __name__ == "__main__":
    if os.getenv("MCP_TRANSPORT", "stdio").lower() in ("http", "streamable-http"):
        mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    else:
        mcp.run()
