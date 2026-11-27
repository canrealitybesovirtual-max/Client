"""Search engine for the web app's search, field suggestions and Quick help. Pure and deterministic: no web imports,
no I/O beyond reading the JSON files in tradecomply/data, nothing per company.

  normalize/tokens  joins the forms contractors type for the same thing (C-39 / c39 / "C 39", B&P / bp, 8 CCR /
                    8ccr, re-roof / reroof, sq ft / sqft, $1,000 / 1000, W-9 / w9), then a small suffix stripper that
                    never touches words with digits or protected acronyms (HERS, CASp, NOC...).
  Index             BM25F (k1 1.2, b 0.75; field weights title 3.0, keys 2.5, cite 1.5, body 1.0, quote 0.25; average
                    length per document type) with query-time synonyms from the vocabulary (weight 0.6, generic forms
                    0.3, longest phrase first, up to 4 words; an expansion of several words matches only documents that
                    hold all of them), prefix completion of the last word while typing (8 terms, 0.7), typo tolerance
                    (SymSpell-style deletes checked with Damerau-Levenshtein, 1 edit under 8 letters and 2 from 8, top 3
                    by document frequency, 0.6, only for words not in the index and with no completion), a coverage
                    factor 0.35 + 0.65 x matched/asked, and a prior per document. Work caps per query: 12 words, typo
                    expansion for 3 of them, completion for the last only.
  documents         builders for rules, checks, pages, professional types, CSLB classes and integrations, from data
                    the caller passes in (app/search.py builds one index per process from them).
  matching          fold() and match_options() for field suggestions: case and accent folding ("jose pena" finds
                    "José Peña"), exact, then word prefix, then contains.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MAX_TOKENS = 12          # words of a query that are processed; the rest are ignored
MAX_TYPO_TOKENS = 3      # words that may get typo corrections
VERIFIED = {"confirmed", "corrected"}


# ---------------------------------------------------------------------------- data files
def _load(name: str):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def vocab() -> dict:
    """tradecomply/data/vocab.json: concept groups (forms -> expansions, tools, anchors), role words, stop and
    protect lists."""
    return _load("vocab.json")


@lru_cache(maxsize=1)
def glossary() -> list[dict]:
    """tradecomply/data/glossary.json: [{term, aliases, expansion?, rule_id}] (no rule text)."""
    return _load("glossary.json")["terms"]


@lru_cache(maxsize=1)
def cslb_d_subcategories() -> dict:
    """tradecomply/data/cslb_d_subcategories.json: the 64 C-61 "D" subcategories as CSLB lists them, with source and
    fetch date. `page_code` ("D-3") is the value everywhere; "still issued" = no note and not "(under relevant
    class)"."""
    return _load("cslb_d_subcategories.json")


def d_still_issued(item: dict) -> bool:
    return not (item.get("note") or "").strip() and "under relevant class" not in item["name"].lower()


def d_name(item: dict) -> str:
    """The name without CSLB's "(under relevant class)" remark, which the detail line carries instead."""
    return re.sub(r"\s*\((?i:under relevant class)\)\s*$", "", item["name"]).strip()


def d_note(item: dict) -> str:
    """CSLB's note in plain form: "(Now under D-64)" -> "Now under D-64"."""
    note = (item.get("note") or "").strip().strip("()").strip()
    if not note and "under relevant class" in item["name"].lower():
        note = "Under the relevant class"
    return note


# ---------------------------------------------------------------------------- tokenizer
STOP = frozenset(vocab()["stop"])
PROTECT = frozenset(vocab()["protect"])

_JOINS = [   # (pattern, replacement, substrings one of which must be present: a cheap test that skips the regex)
    (re.compile(r"\bb\s*&\s*p\b|\bbus(?:iness)?\.?\s*(?:&|and)\s*prof(?:essions)?\.?(?:\s*code)?\b"), " bp ",
     ("&", "prof")),
    (re.compile(r"\bciv(?:il)?\.?\s*code\b|\bciv\.\s"), " civ ", ("civ",)),
    (re.compile(r"\blab(?:or)?\.?\s*code\b"), " lc ", ("lab",)),
    (re.compile(r"\bpub(?:lic)?\.?\s*cont(?:ract)?\.?\s*code\b"), " pcc ", ("pub",)),
    (re.compile(r"\bgov(?:ernment)?\.?\s*code\b"), " gov ", ("gov",)),
    (re.compile(r"\bcal\s*/?\s*osha\b"), " calosha osha ", ("osha",)),
    (re.compile(r"\btitle\s*(8|24)\b"), r" title\1 \1ccr ", ("title",)),
    (re.compile(r"\b(\d{1,2})\s*ccr\b"), r" \1ccr ", ("ccr",)),
    (re.compile(r"\b(\d{1,2})\s*cfr\b"), r" \1cfr ", ("cfr",)),
    # license classes: C-39, c39, C 39, C-039, B-2, D-49 (A and B need the hyphen: "a 5 ft trench" stays apart)
    (re.compile(r"\b([cd])\s?-?\s?0*(\d{1,2})\b"), r" \1\2 ", None),
    (re.compile(r"\b([ab])\s?-\s?0*(\d{1,2})\b"), r" \1\2 ", ("-",)),
    # forms and bills: W-9, I-9, DE 34, DAS 140, PWC-100, WH-347, SF 1413, AB 1327, SB 61, EO 14026
    (re.compile(r"\b(w|i|de|das|pwc|wh|sf|ab|sb|eo|form)\s?-?\s?(\d{1,5})\b"), r" \1\2 ", None),
    (re.compile(r"\bre-(?=[a-z])"), "re", ("re-",)),                       # re-roof -> reroof, re-pipe -> repipe
    (re.compile(r"\b(?:sq\.?\s*f(?:ee)?t\.?|square\s*f(?:ee|oo)t|sf)\b"), " sqft ", ("sq", "sf")),
    (re.compile(r"\b(?:cu\.?\s*y(?:ar)?ds?\.?|cubic\s*yards?|cy)\b"), " cy ", ("cu", "cy")),
    (re.compile(r"(\d)\s*(?:'|ft\b|feet\b|foot\b)"), r"\1 ft ", ("'", "ft", "fe", "fo")),
    (re.compile(r"(\d),(?=\d{3}\b)"), r"\1", (",",)),                   # 1,000 -> 1000
    (re.compile(r"\$\s*(\d)"), r"\1", ("$",)),
    (re.compile(r"(\d)\s*%"), r"\1 percent ", ("%",)),
    (re.compile(r"\bworkers'?\s*comp(?:ensation)?\b|\bwork(?:ers)?\s*comp\b|\bw/?c\b"), " workerscomp ",
     ("comp", "wc", "w/c")),
]
_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)*")
_DIGIT = re.compile(r"\d")


def ascii_fold(text: str) -> str:
    """Accents off, lower case: "José Peña" -> "jose pena"."""
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in t if not unicodedata.combining(c)).encode("ascii", "ignore").decode().lower()


def normalize(text: str) -> str:
    t = ascii_fold((text or "").replace("\u2019", "'"))
    digits = _DIGIT.search(t) is not None
    for rx, rep, needs in _JOINS:
        if needs is None:
            if not digits:
                continue
        elif not any(n in t for n in needs):
            continue
        t = rx.sub(rep, t)
    return t


@lru_cache(maxsize=200_000)
def stem(w: str) -> str:
    if len(w) <= 3 or w in PROTECT or _DIGIT.search(w):
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    cut = False
    if w.endswith("ing") and len(w) > 6:
        w, cut = w[:-3], True
    elif w.endswith("ed") and len(w) > 5:
        w, cut = w[:-2], True
    if cut and len(w) >= 4 and w[-1] == w[-2] and w[-1] not in "lsz":
        w = w[:-1]                                            # digging -> dig, permitted -> permit
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]                                            # require/required -> requir
    return w


def token_pairs(text: str, keep_stop: bool = False) -> list[tuple[str, str]]:
    """(word as written after normalizing, its stem) for each word."""
    out = []
    for w in _WORD.findall(normalize(text)):
        w = w.strip(".")
        if not w or (not keep_stop and w in STOP):
            continue
        out.append((w, stem(w)))
    return out


def tokens(text: str, keep_stop: bool = False) -> list[str]:
    return [s for _, s in token_pairs(text, keep_stop)]


def phrase_key(text: str) -> str:
    """A phrase as the space-joined stemmed words (stop words kept), for whole-word phrase matching."""
    return " ".join(tokens(text, keep_stop=True))


def fold(text: str) -> str:
    """Case and accent folding for company values (casefold + NFKD, combining marks removed)."""
    t = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


_FOLD_WORD = re.compile(r"\w+", re.U)


def match_rank(value: str, q: str) -> int:
    """0 = no match; 3 exact; 2 the query is a prefix of the whole value; 1 the query is a prefix of one of its words
    (each query word must start some word of the value, in any order)."""
    v, x = fold(value).strip(), fold(q).strip()
    if not x:
        return 1
    if v == x:
        return 3
    if v.startswith(x):
        return 2
    words = _FOLD_WORD.findall(v)
    qw = _FOLD_WORD.findall(x)
    if qw and all(any(w.startswith(p) for w in words) for p in qw):
        return 1
    return 0


# ---------------------------------------------------------------------------- typo tolerance
def _deletes(w: str) -> set[str]:
    return {w[:i] + w[i + 1:] for i in range(len(w))}


def damerau(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


# ---------------------------------------------------------------------------- vocabulary
def _form(f) -> tuple[str, float | None]:
    return (f["form"], f.get("weight")) if isinstance(f, dict) else (f, None)


def synonyms(v: dict | None = None) -> dict[str, list[tuple[str, float]]]:
    """phrase -> [(expansion, weight)] from the concept groups and the role words."""
    v = v or vocab()
    w0, wg = float(v.get("synonym_weight", 0.6)), float(v.get("generic_weight", 0.3))
    out: dict[str, list[tuple[str, float]]] = {}
    for g in v["groups"]:
        for f in g["forms"]:
            text, w = _form(f)
            out.setdefault(text, []).extend((e, wg if w is not None else w0) for e in g["expand"])
    for f, exps in v.get("roles", {}).items():
        out.setdefault(f, []).extend((e, w0) for e in exps)
    return out


@lru_cache(maxsize=1)
def _group_keys() -> list[tuple[dict, list[str]]]:
    """Each group's whole-word keys for matching a question: its forms and expansions, but never one that starts
    with a number (a date like 10/20/2026 must not mean "20-day notice")."""
    out = []
    for g in vocab()["groups"]:
        keys = []
        for text in [_form(f)[0] for f in g["forms"]] + list(g["expand"]):
            k = phrase_key(text)
            if k and not k[0].isdigit():
                keys.append(f" {k} ")
        out.append((g, keys))
    return out


def groups_in(text: str) -> list[dict]:
    """Vocabulary groups whose forms or rulebook words appear in the text as whole words."""
    nq = f" {phrase_key(text)} "
    return [g for g, keys in _group_keys() if any(k in nq for k in keys)]


def tool_words(v: dict | None = None) -> dict[str, list[str]]:
    """Check name -> the vocabulary forms and expansions of its groups (the palette's and the index's keys)."""
    out: dict[str, list[str]] = {}
    for g in (v or vocab())["groups"]:
        words = [_form(f)[0] for f in g["forms"]] + list(g["expand"])
        for t in g["tools"]:
            out.setdefault(t, [])
            out[t] += [w for w in words if w not in out[t]]
    return out


# ---------------------------------------------------------------------------- documents and index
FIELD_W = {"title": 3.0, "keys": 2.5, "cite": 1.5, "body": 1.0, "quote": 0.25}
K1, B = 1.2, 0.75


@dataclass
class Doc:
    type: str                       # page | tool | rule | professional | class | integration
    id: str
    title: str
    fields: dict                    # field name -> text
    meta: dict = field(default_factory=dict)
    prior: float = 1.0


@dataclass
class Expanded:
    terms: list                     # [(term, weight, concepts)]
    phrases: list                   # [(terms tuple, weight, concepts)]: every word must be in the document
    concepts: int                   # question words counted for coverage
    notes: list                     # "synonym:<key>", "typo:<word>-><fix>"
    fixes: dict                     # typo word -> correction (for "did you mean")


class Index:
    def __init__(self, docs: list[Doc], synonym_map: dict[str, list[tuple[str, float]]] | None = None):
        self.docs = docs
        post: dict[str, list[tuple[int, float]]] = defaultdict(list)
        self.dl: list[float] = []
        per_type_len = defaultdict(list)
        seen_as: dict[str, Counter] = defaultdict(Counter)
        for i, d in enumerate(docs):
            tf: Counter = Counter()
            for f, text in d.fields.items():
                w = FIELD_W.get(f, 1.0)
                for word, t in token_pairs(text):
                    tf[t] += w
                    seen_as[t][word] += 1
            for t, v in tf.items():
                post[t].append((i, v))
            length = sum(tf.values()) or 1.0
            self.dl.append(length)
            per_type_len[d.type].append(length)
        self.post = dict(post)
        # the most common written form of each stem, to show a typo fix as a word ("asbesto" -> "asbestos")
        self.surface_of = {t: max(c.items(), key=lambda kv: (kv[1], -len(kv[0])))[0] for t, c in seen_as.items()}
        del seen_as
        self.avg = {t: sum(v) / len(v) for t, v in per_type_len.items()}
        self.title_norm = [normalize(d.title).strip() for d in docs]
        n = len(docs)
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.post.items()}
        self.vocab = sorted(self.post)
        self.df = {t: len(p) for t, p in self.post.items()}
        dels: dict[str, list[str]] = defaultdict(list)
        for t in self.vocab:
            if len(t) >= 4 and t.isalpha():
                for x in _deletes(t):
                    dels[x].append(t)
        self.del_index = dict(dels)
        self.syn: dict[str, list[tuple[tuple, float]]] = {}
        for phrase, exps in (synonym_map or {}).items():
            key = phrase_key(phrase)
            if key:
                self.syn.setdefault(key, []).extend((tuple(tokens(e)), w) for e, w in exps if tokens(e))
        self._docmaps: dict[str, dict[int, float]] = {}
        self.by_id = {(d.type, d.id): i for i, d in enumerate(docs)}

    def surface(self, t: str) -> str:
        return self.surface_of.get(t, t)

    def _docmap(self, t: str) -> dict[int, float]:
        m = self._docmaps.get(t)
        if m is None:
            m = dict(self.post.get(t, ()))
            self._docmaps[t] = m
        return m

    # -- query expansion
    def typo(self, t: str, limit: int = 3) -> list[str]:
        if t in self.post or len(t) < 4 or not t.isalpha():
            return []
        cands = set(self.del_index.get(t, ())) | {c for x in _deletes(t) for c in self.del_index.get(x, ())}
        cands |= {x for x in _deletes(t) if x in self.post}
        cands |= {stem(x) for x in _deletes(t) if stem(x) in self.post}
        maxd = 1 if len(t) < 8 else 2
        ok = [c for c in cands if damerau(t, c, maxd) <= maxd]
        return sorted(ok, key=lambda c: (-self.df[c], c))[:limit]

    def prefix(self, p: str, limit: int = 8) -> list[str]:
        if len(p) < 2:
            return []
        i = bisect.bisect_left(self.vocab, p)
        out = []
        while i < len(self.vocab) and self.vocab[i].startswith(p) and len(out) < 200:
            out.append(self.vocab[i])
            i += 1
        return sorted(out, key=lambda c: (-self.df[c], c))[:limit]

    def expand(self, q: str, typing: bool = False) -> Expanded:
        """The question's words (at most MAX_TOKENS non-stop words), their synonyms (longest phrase first), the
        completion of the last word while typing, and typo fixes. A concept is one non-stop word of the question; a
        synonym answers the concepts of the words it replaced (for the coverage factor)."""
        raw = tokens(q, keep_stop=True)
        concept_of: dict[int, int] = {}
        for j, t in enumerate(raw):
            if t not in STOP:
                if len(concept_of) == MAX_TOKENS:
                    raw = raw[:j]
                    break
                concept_of[j] = len(concept_of)
        terms: list = []
        phrases: list = []
        notes: list = []
        fixes: dict = {}
        covered: list[tuple[int, int]] = []
        for n in (4, 3, 2, 1):
            for i in range(len(raw) - n + 1):
                if any(a <= i and i + n <= b for a, b in covered):
                    continue
                key = " ".join(raw[i:i + n])
                if key in self.syn:
                    cs = frozenset(concept_of[j] for j in range(i, i + n) if j in concept_of)
                    if not cs:
                        continue
                    covered.append((i, i + n))
                    for exp, w in self.syn[key]:
                        if len(exp) == 1:
                            terms.append((exp[0], w, cs))
                        else:
                            phrases.append((exp, w, cs))
                    notes.append(f"synonym:{key}")
        last_open = typing and bool(q) and not q[-1].isspace()
        last_j = max(concept_of) if concept_of else -1
        typo_budget = MAX_TYPO_TOKENS
        for j, c in concept_of.items():
            t, cs = raw[j], frozenset([c])
            if t in self.post:
                terms.append((t, 1.0, cs))
            completions = self.prefix(t) if j == last_j and last_open else []
            for p in completions:
                if p != t:
                    terms.append((p, 0.7, cs))
            if t not in self.post and not completions and typo_budget > 0:
                found = self.typo(t)
                if found:
                    typo_budget -= 1
                    fixes[t] = found[0]
                    notes.append(f"typo:{t}->{found[0]}")
                for f in found:
                    terms.append((f, 0.6, cs))
        return Expanded(terms, phrases, len(concept_of), notes, fixes)

    def _bm25(self, i: int, t: str, tf: float) -> float:
        d = self.docs[i]
        return self.idf[t] * tf * (K1 + 1) / (tf + K1 * (1 - B + B * self.dl[i] / self.avg[d.type]))

    def score(self, ex: Expanded, types: set | None = None) -> tuple[dict[int, float], dict[int, set]]:
        acc: dict[int, float] = defaultdict(float)
        hit: dict[int, set] = defaultdict(set)
        best: dict[tuple[int, str], float] = {}

        def add(i, t, s, cs):
            k = (i, t)
            old = best.get(k, 0.0)
            if s > old:                            # a term counts once per document even if expanded twice
                acc[i] += s - old
                best[k] = s
            hit[i] |= cs

        for t, w, cs in ex.terms:
            if t not in self.idf:
                continue
            for i, tf in self.post[t]:
                if types and self.docs[i].type not in types:
                    continue
                add(i, t, self._bm25(i, t, tf) * w, cs)
        for words, w, cs in ex.phrases:
            if any(t not in self.idf for t in words):
                continue
            rarest = min(words, key=lambda t: self.df[t])
            maps = [self._docmap(t) for t in words if t != rarest]
            for i, tf in self.post[rarest]:
                if types and self.docs[i].type not in types:
                    continue
                if all(i in m for m in maps):
                    for t in words:
                        add(i, t, self._bm25(i, t, tf if t == rarest else self._docmap(t)[i]) * w, cs)
        return acc, hit

    def search_info(self, q: str, types: set | None = None, limit: int = 10, typing: bool = False,
                    filt=None) -> tuple[list[tuple[float, Doc, dict]], Expanded]:
        ex = self.expand(q, typing)
        if not ex.terms and not ex.phrases:
            return [], ex
        acc, hit = self.score(ex, types)
        out = []
        qn = normalize(q).strip()
        short = typing and len(qn) <= 5 and " " not in qn
        concepts = max(1, ex.concepts)
        for i, s in acc.items():
            d = self.docs[i]
            if filt and not filt(d):
                continue
            cov = len(hit[i]) / concepts
            sc = s * (0.35 + 0.65 * cov) * d.prior
            if typing and qn:
                tn = self.title_norm[i]
                if tn.startswith(qn):
                    sc *= 3.0 if short else 1.6          # "proj" -> Projects, "set" -> Settings
                elif short and any(w.startswith(qn) for w in tn.split()):
                    sc *= 1.8
            out.append((sc, d, {"coverage": round(cov, 3)}))
        out.sort(key=lambda x: (-x[0], x[1].type, x[1].id))
        return dedupe(out)[:limit], ex

    def search(self, q: str, types: set | None = None, limit: int = 10, typing: bool = False,
               filt=None) -> list[tuple[float, Doc, dict]]:
        return self.search_info(q, types, limit, typing, filt)[0]

    def coverage(self, q: str, text: str) -> float:
        """The share of the question's words (or their synonyms and typo fixes) that the text contains."""
        ex = self.expand(q)
        if not ex.concepts:
            return 0.0
        have = set(tokens(text))
        got: set = set()
        for t, _w, cs in ex.terms:
            if t in have:
                got |= cs
        for words, _w, cs in ex.phrases:
            if all(t in have for t in words):
                got |= cs
        return len(got) / ex.concepts


def dedupe(results):
    """Pack-prefixed rule ids ("web_labor-hr:heat-cool-down-rest-5-minutes") are separate rules that can share a slug
    with another rule; keep the best-ranked one only when slug and citation are the same."""
    seen, out = set(), []
    for s, d, m in results:
        if d.type == "rule":
            key = (d.id.split(":", 1)[-1], (d.meta.get("citation") or "").lower()[:40])
            if key in seen:
                continue
            seen.add(key)
        out.append((s, d, m))
    return out


def build(docs: list[Doc], v: dict | None = None) -> Index:
    return Index(docs, synonyms(v))


# ---------------------------------------------------------------------------- document builders (data passed in)
_FIRST_SENTENCE = re.compile(r"(?<=[.!?])\s")


def rule_docs(rules) -> list[Doc]:
    """Rules from core.all_rules(). Prior: verified 1.10, unchecked 1.00, unverifiable 0.92."""
    out = []
    for r in rules:
        slug = r.id.split(":", 1)[-1].replace("-", " ")
        prior = 1.1 if r.status in VERIFIED else 0.92 if r.status == "unverifiable" else 1.0
        out.append(Doc("rule", r.id, r.title or r.id,
                       {"title": r.title or "", "keys": slug, "cite": r.citation_text or "",
                        "body": " ".join(filter(None, [r.plain_english, r.applies_to])), "quote": r.quote or ""},
                       meta={"citation": r.citation_text, "status": r.status, "verified": r.status in VERIFIED,
                             "href": f"#/rule/{r.id}"}, prior=prior))
    return out


def tool_docs(specs: list[dict], catalog: dict[str, dict], v: dict | None = None) -> list[Doc]:
    """Checks: title from the catalog, keys = the vocabulary words of the check's groups + its name, body = summary +
    description. Prior 1.15; hidden checks 0.6."""
    words = tool_words(v)
    out = []
    for s in specs:
        c = catalog.get(s["name"], {})
        title = c.get("title") or s.get("title") or s["name"]
        props = (s.get("input_schema") or {}).get("properties", {})
        desc = s.get("description") or ""
        first = _FIRST_SENTENCE.split(desc, 1)[0]
        out.append(Doc("tool", s["name"], title,
                       {"title": title, "keys": " ".join(words.get(s["name"], [])) + " " + s["name"].replace("_", " "),
                        "body": " ".join(filter(None, [c.get("summary"), first, desc[:600]])),
                        "quote": " ".join(k.replace("_", " ") for k in props)},
                       meta={"href": f"#/tools/{s['name']}", "summary": c.get("summary") or first,
                             "hidden": bool(c.get("hidden")), "group": c.get("group")},
                       prior=0.6 if c.get("hidden") else 1.15))
    return out


def page_docs(pages: list[dict]) -> list[Doc]:
    """Pages and Settings sections: {id, title, href, keys, roles}. Prior 1.30."""
    return [Doc("page", p["id"], p["title"], {"title": p["title"], "keys": p.get("keys", "")},
                meta={"href": p["href"], "roles": tuple(p["roles"]), "detail": p.get("detail")}, prior=1.3)
            for p in pages]


def professional_docs(professionals: dict, handoffs: dict) -> list[Doc]:
    """Professional types (24 + the 8 extra types handoffs.json adds), from name, plain description, triggers and the
    when_required situations only: never not_required_when or optional_when (in a "can I skip" search those read as
    permission). Prior 1.10."""
    out = []
    for t in professionals["types"] + handoffs.get("extra_types", []):
        situations = " ".join(w.get("situation", "") for w in t.get("when_required", []))
        out.append(Doc("professional", t["id"], t["name"],
                       {"title": t["name"], "keys": " ".join(t.get("triggers", [])) + " " + t["id"].replace("_", " "),
                        "body": " ".join(filter(None, [t.get("plain_description"), situations]))},
                       meta={"href": f"#/professionals?type={t['id']}"}, prior=1.1))
    return out


BASE_CLASSES = (("A", "General Engineering", "general-engineering-contractor-def", "B&P 7056"),
                ("B", "General Building", "general-building-contractor-def", "B&P 7057"),
                ("B-2", "Residential Remodeling", "b2-residential-remodeling-scope", "B&P 7057.5"))


def class_docs(class_table: dict, rule_citation, d_items: list[dict]) -> list[Doc]:
    """CSLB classes A, B, B-2, the 43 C classes and the C-61 "D" subcategories still issued. Each links to its own
    scope rule (D items to the C-61 rule). Prior 1.05."""
    out = []
    for code, name, rid, _cite in BASE_CLASSES:
        out.append(Doc("class", code, f"{code} {name}", {"title": f"{code} {name}", "keys": code},
                       meta={"rule_id": rid, "name": name, "citation": rule_citation(rid)}, prior=1.05))
    for code, e in class_table.items():
        out.append(Doc("class", code, f"{code} {e.name}",
                       {"title": f"{code} {e.name}", "keys": " ".join(e.keywords) + " " + code,
                        "body": " ".join(e.scope)[:800]},
                       meta={"rule_id": e.rule_id, "name": e.name, "citation": rule_citation(e.rule_id)},
                       prior=1.05))
    c61 = class_table.get("C-61")
    for it in d_items:
        if not d_still_issued(it) or c61 is None:
            continue
        name = d_name(it)
        out.append(Doc("class", it["page_code"], f"{it['page_code']} {name}",
                       {"title": f"{it['page_code']} {name}", "keys": f"{it['code']} {it['page_code']} c61"},
                       meta={"rule_id": c61.rule_id, "name": name, "citation": rule_citation(c61.rule_id),
                             "d_subcategory": True}, prior=1.05))
    return out


def short_name(name: str) -> str:
    """The product name without its note: "Slack (channel alerts and Slack MCP Server)" -> "Slack"."""
    return re.split(r"\s*[(:]", name, maxsplit=1)[0].strip() or name


def integration_docs(catalog: dict) -> list[Doc]:
    """Integrations catalog products. Prior 0.90."""
    from urllib.parse import quote
    cats = {c["id"]: c["label"] for c in catalog.get("categories", [])}
    out = []
    for e in catalog.get("entries", []):
        name = short_name(e["name"])
        out.append(Doc("integration", e["id"], e["name"],
                       {"title": e["name"], "keys": " ".join([e.get("vendor") or "", cats.get(e.get("category"), ""),
                                                              " ".join(e.get("keywords") or [])]),
                        "body": (e.get("summary") or "")[:500]},
                       meta={"href": f"#/integrations?q={quote(name)}", "short": name,
                             "category": cats.get(e.get("category"))}, prior=0.9))
    return out


# ---------------------------------------------------------------------------- field suggestions (static lists)
def option(value, label=None, detail=None, aliases=(), **extra) -> dict:
    """A suggestion item. `_keys` (normalized value, label and aliases) is for matching and never sent."""
    keys = []
    for x in [value, label or "", *aliases]:
        k = normalize(str(x)).strip()
        if k and k not in keys:
            keys.append(k)
    item = {"value": value, "label": label or value}
    if detail:
        item["detail"] = detail
    if aliases:
        item["aliases"] = [a for a in dict.fromkeys(aliases) if a and a != value and a != label]
    item.update(extra)
    item["_keys"] = [(k, tuple(tokens(k, keep_stop=True))) for k in keys]
    return item


def match_options(options: list[dict], q: str, limit: int) -> list[dict]:
    """Exact value or alias, then prefix, then a word starting with the query, then every query word starting some
    word; ties keep the source's own order. Distinct values only."""
    nq = normalize(q).strip()
    if not nq:
        return options[:limit]
    qt = tokens(q, keep_stop=True)
    scored = []
    for n, o in enumerate(options):
        best = 0.0
        for k, kt in o["_keys"]:
            if k == nq:
                best = max(best, 4.0)
            elif k.startswith(nq):
                best = max(best, 3.0)
            elif f" {nq}" in f" {k}":
                best = max(best, 2.0)
            elif qt and all(any(w.startswith(x) for w in kt) for x in qt):
                best = max(best, 1.0)
        if best:
            scored.append((-best, n, o))
    scored.sort(key=lambda x: (x[0], x[1]))
    out, seen = [], set()
    for _, _, o in scored:
        key = str(o["value"]).lower()
        if key not in seen:
            seen.add(key)
            out.append(o)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------- grouping
def grouped(results: list[tuple[float, Doc, dict]], order: list[tuple[str, int]]) -> dict[str, list]:
    """One search, then a quota per document type in a fixed order."""
    out = {}
    for t, n in order:
        items = [(s, d, m) for s, d, m in results if d.type == t][:n]
        if items:
            out[t] = items
    return out
