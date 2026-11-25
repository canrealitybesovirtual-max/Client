"""Rulebook tools: let an AI agent search and read the California rules behind every TradeComply answer.

  search_california_rules  keyword search (BM25 over title, plain English, citation, scope) -> matching rules
  get_california_rule      one rule by id, with its quote, notes and the verifier's correction

Each rule carries its verification status: confirmed / corrected (checked against the official text; for corrected
rules the parameters ARE the corrected values), unverifiable (official text could not be read) or unchecked
(procedural). Only confirmed and corrected rules count as verified.
"""
from typing import Annotated, Optional

from pydantic import BaseModel, Field

from . import core


class RuleSummary(BaseModel):
    id: str
    title: Optional[str] = None
    plain_english: Optional[str] = None
    category: Optional[str] = None
    parameters: dict = Field(default_factory=dict)
    citation: Optional[str] = None
    source: Optional[str] = None
    status: str
    verified: bool
    checked_on: Optional[str] = None


class RuleDetail(RuleSummary):
    applies_to: Optional[str] = None
    quote: Optional[str] = None
    effective: Optional[str] = None
    notes: Optional[str] = None
    correction: Optional[str] = None
    pack: Optional[str] = None


def _summary(r: core.Rule) -> RuleSummary:
    return RuleSummary(id=r.id, title=r.title, plain_english=r.plain_english, category=r.category,
                       parameters=r.parameters, citation=r.citation_text, source=r.source, status=r.status,
                       verified=r.verified, checked_on=r.checked_on)


def categories() -> list[str]:
    return sorted({r.category for r in core.all_rules() if r.category})


def search_rules(query: str, limit: int = 10, verified_only: bool = False,
                 category: Optional[str] = None) -> list[RuleSummary]:
    if not (query or "").strip():
        raise ValueError("query must not be empty")
    if category is not None and category not in categories():
        raise ValueError(f"unknown category {category!r}; use one of: {', '.join(categories())}")
    limit = max(1, min(int(limit), 50))
    return [_summary(r) for r in core.search(query, limit, verified_only=verified_only, category=category)]


def get_rule(rule_id: str) -> RuleDetail:
    try:
        r = core.rule((rule_id or "").strip())
    except KeyError:
        near = [x.id for x in core.search((rule_id or "").replace("-", " ").replace(":", " "), 5)]
        raise ValueError(f"unknown rule id {rule_id!r}" + (f"; closest: {', '.join(near)}" if near else "")) from None
    return RuleDetail(**_summary(r).model_dump(), applies_to=r.applies_to, quote=r.quote, effective=r.effective,
                      notes=r.notes, correction=r.correction, pack=r.pack)


def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}

    @mcp.tool(name="search_california_rules", annotations=ro)
    def search_california_rules(
        query: Annotated[str, Field(description="Plain words, e.g. 'preliminary notice 20 days', 'trench permit', "
                                                "'home improvement down payment'")],
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum rules to return")] = 10,
        verified_only: Annotated[bool, Field(description="Only rules checked against the official text "
                                                         "(confirmed or corrected)")] = False,
        category: Annotated[Optional[str], Field(description="Optional filter: penalty, deadline, process_step, "
                                                             "threshold, required_document, money_amount, prohibition, "
                                                             "definition, license_classification, required_notice, "
                                                             "required_contract_clause")] = None,
    ) -> list[RuleSummary]:
        """Search the TradeComply California construction rulebook (about 2,100 rules from the CSLB Law Book, Labor
        Code, Civil Code, Title 8 / Title 24, DIR, CSLB and other official sources).

        Returns up to `limit` rules, best match first (verified rules win ties), each with id, title, plain_english,
        category, parameters (the numbers to use), citation, source URL, status (confirmed | corrected |
        unverifiable | unchecked), verified and checked_on. Tell the user when a rule is not verified. Use
        get_california_rule for the official quote and the verifier's notes."""
        return search_rules(query, limit, verified_only, category)

    @mcp.tool(name="get_california_rule", annotations=ro)
    def get_california_rule(
        rule_id: Annotated[str, Field(description="Rule id from search_california_rules or a result's citations, "
                                                  "e.g. 'neshap-notification-10-working-days'")],
    ) -> RuleDetail:
        """Read one rulebook rule in full: title, plain_english, parameters, citation, source, status, verified,
        checked_on, plus applies_to, the official quote, effective date, research notes, the verifier's correction
        (for corrected rules) and the research pack. Unknown ids return an error that lists the closest ids."""
        return get_rule(rule_id)
