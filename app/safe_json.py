"""Text and numbers that can always be written back out as UTF-8 JSON.

Python's json module reads two things from perfectly valid JSON that can never be written out again: "\\ud800" (half
of a UTF-16 surrogate pair, which has no UTF-8 encoding) and 1e400, NaN or Infinity (JSON has no infinite or
not-a-number values). Stored once, such a value made every later response, download and export that included it fail
(the activity log, /api/me, the projects list, the company export, the Splunk feed). So:

- problem(value): what main.StrictJSONBodies checks in every JSON request body, answering 422 before anything runs.
- clean(value): the second line, for data already stored: lone surrogates become U+FFFD and non-finite numbers
  None. Used by the default JSON response, the downloads and exports, and the activity-log arguments.
"""
import json
import math

PROBLEM = ("The request has text or a number that can't be saved: a broken character (an unpaired \\ud800-\\udfff "
           "escape) or a number that is infinite or not a number (NaN, Infinity, or too large, like 1e400). Remove "
           "it and try again.")


def fix_str(s: str) -> str:
    """The string, with any lone surrogate replaced by U+FFFD (a proper surrogate pair becomes its character)."""
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _bad(v) -> bool:
    if isinstance(v, str):
        try:
            v.encode("utf-8")
            return False
        except UnicodeEncodeError:
            return True
    return isinstance(v, float) and not math.isfinite(v)


def problem(value) -> bool:
    """True if `value` (parsed JSON) holds a lone surrogate (in a key or a value) or a non-finite number. Iterative,
    so a deeply nested body can't exhaust the stack."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
        elif _bad(v):
            return True
    return False


def clean(value, _depth: int = 0):
    """`value` with lone surrogates replaced and non-finite numbers set to None, so json.dumps(..., allow_nan=False)
    and .encode("utf-8") always work. Returns `value` itself when there is nothing to fix."""
    if not problem(value):
        return value
    return _clean(value, 0)


def _clean(v, depth: int):
    if isinstance(v, str):
        return fix_str(v)
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if depth > 200:                  # nothing the app stores is nested this deep
        return None
    if isinstance(v, dict):
        return {(_clean(k, depth + 1) if isinstance(k, str) else k): _clean(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x, depth + 1) for x in v]
    return v


def dumps(value, **kw) -> str:
    """json.dumps of clean(value): valid JSON that always encodes as UTF-8."""
    kw.setdefault("ensure_ascii", False)
    return json.dumps(clean(value), allow_nan=False, **kw)
