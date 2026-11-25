"""Shared building blocks for every module: the verified rulebook, citations and the result base model.

The rulebook (tradecomply/data/rulebook.json) is compiled from research/*.verified.json. Each rule was extracted from
an official source (CSLB 2026 Law Book, dir.ca.gov, cslb.ca.gov, ...) and then checked by an independent verifier:
  confirmed    - the official text states the rule as written
  corrected    - the verifier fixed a value, citation or scope against the official text (use the corrected value)
  unverifiable - the primary text could not be read (e.g. leginfo blocks automated access); treat as unverified
  unchecked    - procedural/definition rules outside the verification scope
Modules must take every number they compute with from here (rule(...).param(...)), so a value always has a citation.
"""
import json
import math
import os
import re
from collections import Counter
from functools import lru_cache

from pydantic import BaseModel, Field

RULEBOOK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "rulebook.json")
VERIFIED = {"confirmed", "corrected"}
DISCLAIMER = ("Compliance information, not legal advice. Values come from official California sources checked on the "
              "date shown; laws change. A licensed professional (attorney, engineer, architect, safety professional) "
              "must review anything you sign, stamp or file.")


class Citation(BaseModel):
    rule_id: str
    citation: str | None = None
    source: str | None = None
    status: str = "unchecked"
    checked_on: str | None = None

    @property
    def verified(self) -> bool:
        return self.status in VERIFIED


class Result(BaseModel):
    """Base for every tool result. `verified` is True only when every rule the result relied on is verified."""
    verified: bool = False
    citations: list[Citation] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def cite(self, *rule_ids: str) -> "Result":
        have = {c.rule_id for c in self.citations}
        for rid in rule_ids:
            if rid not in have:
                self.citations.append(rule(rid).citation)
                have.add(rid)
        self.verified = bool(self.citations) and all(c.verified for c in self.citations)
        return self


class Rule(BaseModel):
    id: str
    title: str | None = None
    plain_english: str | None = None
    category: str | None = None
    parameters: dict = Field(default_factory=dict)
    applies_to: str | None = None
    citation_text: str | None = Field(None, alias="citation")
    source: str | None = None
    quote: str | None = None
    effective: str | None = None
    notes: str | None = None
    status: str = "unchecked"
    correction: str | None = None
    checked_on: str | None = None
    pack: str | None = None

    model_config = {"populate_by_name": True}

    @property
    def verified(self) -> bool:
        return self.status in VERIFIED

    @property
    def citation(self) -> Citation:
        return Citation(rule_id=self.id, citation=self.citation_text, source=self.source, status=self.status,
                        checked_on=self.checked_on)

    def param(self, key: str, default=KeyError):
        """Dotted keys reach nested values: rule('x').param('prior_rates_usd.2025-01-01')."""
        cur = self.parameters
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif default is KeyError:
                raise KeyError(f"rule {self.id} has no parameter {key!r}; has {sorted(self.parameters)}")
            else:
                return default
        return cur


@lru_cache(maxsize=1)
def _book() -> dict[str, Rule]:
    with open(RULEBOOK_PATH, encoding="utf-8") as f:
        return {r["id"]: Rule.model_validate(r) for r in json.load(f)["rules"]}


def rule(rule_id: str) -> Rule:
    try:
        return _book()[rule_id]
    except KeyError:
        raise KeyError(f"unknown rule id {rule_id!r}") from None


def all_rules() -> list[Rule]:
    return list(_book().values())


_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_STOP = set("a an and are as at be by for from has have if in is it its of on or that the this to was were will with "
            "you your must may can any all not no".split())


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


@lru_cache(maxsize=1)
def _index():
    docs = {}
    for r in _book().values():
        text = " ".join(filter(None, [r.title, r.title, r.plain_english, r.citation_text, r.applies_to, r.category,
                                      r.id.replace("-", " ")]))
        docs[r.id] = Counter(_tokens(text))
    df = Counter(t for c in docs.values() for t in c)
    avg = sum(sum(c.values()) for c in docs.values()) / max(1, len(docs))
    return docs, df, avg


def search(query: str, limit: int = 10, verified_only: bool = False, category: str | None = None) -> list[Rule]:
    """BM25 keyword search over the rulebook. Verified rules rank first on ties."""
    docs, df, avg = _index()
    n, terms = len(docs), _tokens(query)
    book = _book()
    scored = []
    for rid, tf in docs.items():
        r = book[rid]
        if (verified_only and not r.verified) or (category and r.category != category):
            continue
        dl = sum(tf.values())
        s = 0.0
        for t in terms:
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * dl / avg))
        if s > 0:
            scored.append((s + (0.5 if r.verified else 0), rid))
    scored.sort(reverse=True)
    return [book[rid] for _, rid in scored[:limit]]
