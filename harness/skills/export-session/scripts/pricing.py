#!/usr/bin/env python3
"""Model pricing table + cost math for session-cost estimates.

IDE-agnostic and collector-agnostic: both the Claude Code collector and the
frozen HTML generator import PRICING / _family / _cost from here, so the frozen,
IDE-agnostic generator never has to reach into a collector for pricing. Adding a
new collector does not change these rates.
"""

# Approximate USD per 1M tokens (public list price). Maintained by hand; unknown models
# -> cost hidden. cw = cache write (1.25x input), cr = cache read (0.1x input).
# Last verified: 2026-07-20
#
# MAINTENANCE — Sonnet introductory rate expires 2026-08-31:
#   The single "sonnet" entry below carries the Sonnet-5 *introductory* rate
#   ($2 in / $10 out), which is in effect ONLY through 2026-08-31. After that
#   date the standard Sonnet rate is $3 / $15 / $3.75 cw / $0.30 cr, and this
#   one entry conflates every Sonnet generation (Sonnet-4 is already $3/$15
#   standard, not the intro rate). When the intro period ends — or if Sonnet-4
#   vs Sonnet-5 costs must diverge before then — split this into distinct
#   "sonnet-5" / "sonnet-4" keys and teach _family() to route to them (checking
#   the more specific keys before the generic "sonnet"). Left as one key for now
#   so no current cost value changes; this dated note is the maintenance flag.
PRICING = {
    "fable":  {"in": 10.0, "out": 50.0, "cw": 12.50, "cr": 1.00},
    "opus":   {"in": 5.0,  "out": 25.0, "cw": 6.25,  "cr": 0.50},
    "sonnet": {"in": 2.0,  "out": 10.0, "cw": 2.50,  "cr": 0.20},
    "haiku":  {"in": 1.0,  "out": 5.0,  "cw": 1.25,  "cr": 0.10},
}


def _family(model):
    if not model:
        return None
    m = model.lower()
    # If PRICING ever splits Sonnet by generation, add the more specific keys
    # (e.g. "sonnet-5", "sonnet-4") to the front of this tuple so they win over
    # the generic "sonnet" substring match.
    for fam in ("fable", "opus", "sonnet", "haiku"):
        if fam in m:
            return fam
    return None


def _cost(model, tokens):
    fam = _family(model)
    if not fam:
        return None
    r = PRICING[fam]
    return (tokens["input"] * r["in"]
            + tokens["cache_creation"] * r["cw"]
            + tokens["cache_read"] * r["cr"]
            + tokens["output"] * r["out"]) / 1_000_000.0
