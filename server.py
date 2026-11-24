"""TradeComply CA: MCP server for California construction trade compliance.

Five tools let an AI agent check a contractor license, estimate permit fees, look up Cal/OSHA safety rules,
compute mechanics lien deadlines, and prequalify a subcontractor.

All regulatory data lives in tradecomply/rules.py and is UNVERIFIED until someone checks each value against the
cited source. Every result carries `verified` and a disclaimer. Not legal advice.

Run locally:      python server.py
Inspect tools:    fastmcp dev server.py
HTTP transport:   MCP_TRANSPORT=http python server.py     (PORT, default 8000; endpoint /mcp)
"""
import os
from typing import Annotated, Optional

from fastmcp import FastMCP
from pydantic import Field

from tradecomply import logic
from tradecomply.rules import CA_LIEN, CSLB, DISCLAIMER, FEDERAL_PENALTY_MAX_USD, PERMIT_FEE_SCHEDULE

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
    jurisdiction: Annotated[str, Field(description="City or county, e.g. 'Los Angeles'")],
    project_type: Annotated[str, Field(description="residential, commercial, tenant_improvement or solar")],
    valuation_usd: Annotated[float, Field(gt=0, description="Project valuation in US dollars")],
    square_footage: Annotated[Optional[float], Field(gt=0, description="Conditioned/project area, if known")] = None,
) -> logic.PermitFeeResult:
    """Rough building permit fee estimate. Placeholder rates until real schedules are loaded."""
    return logic.estimate_permit_fees(jurisdiction, project_type, valuation_usd, square_footage)


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
    notice_of_completion_date: Annotated[Optional[str], Field(description="ISO date a notice of completion was recorded, if any")] = None,
) -> logic.LienDeadlineResult:
    """California mechanics lien deadlines: preliminary notice, lien recording, and foreclosure, with days left."""
    return logic.lien_deadlines(claimant_role, last_furnishing_date, first_furnishing_date, completion_date,
                                notice_of_completion_date)


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


@mcp.resource("tradecomply://disclaimer")
def disclaimer() -> str:
    """Disclaimer and which data sets have been verified."""
    rows = [("CA lien rules", CA_LIEN), ("CSLB constants", CSLB), ("Permit fee schedules", PERMIT_FEE_SCHEDULE),
            ("Federal OSHA penalties", FEDERAL_PENALTY_MAX_USD)]
    status = "\n".join(f"- {name}: {'verified ' + str(d.get('checked_on')) if d.get('verified') else 'UNVERIFIED'}"
                       for name, d in rows)
    return f"{DISCLAIMER}\n\nData status:\n{status}\n- Cal/OSHA safety table: UNVERIFIED summaries; read the linked section."


if __name__ == "__main__":
    if os.getenv("MCP_TRANSPORT", "stdio").lower() in ("http", "streamable-http"):
        mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    else:
        mcp.run()
