#!/usr/bin/env python3
"""
The kit's own skill-name set, read from `core/agents.yaml`.

`core/agents.yaml` is the single source of truth for skill metadata, so the
workflow-boundary detectors recognize skill names by reading it — never from a
list embedded in this feature. Add a skill to agents.yaml and it becomes
detectable here with no code change.

Where it is used, and where it is deliberately not:

  * Claude Code — two jobs, one soft and one LOAD-BEARING.
      (a) Activation recall: the log proves activation on its own (a "Base
          directory for this skill" entry follows a real invocation), so here
          the registry is only an EXTRA way to say yes — it recovers an
          invocation whose activation entry is missing because the transcript
          was truncated or the run was interrupted, and it never vetoes.
          Gating activation on the registry would silently drop every skill
          that is not an Agentic Studio skill; in one real session measured
          during this work that would have hidden the two costliest workflows.
      (b) MENTIONS: an L1 WORKFLOW's name typed as a standalone token in a
          user's own message opens a run with no slash and no activation proof.
          The registry is the whole definition of what may be mentioned into
          existence, so nothing outside agents.yaml can ever open a run this
          way — which is exactly why non-kit skills stay activation-only. See
          mention_pattern() and workflows().
  * Devin — the stream may advertise its command list (`available_commands_update`);
    when it does not, this registry decides, replacing what was otherwise a
    guess from the shape of the text.
  * VS Code family — its prompt-file marker carries no kit-specific naming to
    check (any prompt file is a unit of work, and a new skill is picked up with
    no change either way), so nothing here is consulted. Mentions are a Claude
    Code affordance only: on the other hosts the user picks the agent or command
    explicitly, so intent is already captured by the existing markers.
  * Devin — the stream may advertise its command list (`available_commands_update`);
    when it does not, this registry decides, replacing what was otherwise a
    guess from the shape of the text.
  * VS Code family — its prompt-file marker carries no kit-specific naming to
    check (any prompt file is a unit of work, and a new skill is picked up with
    no change either way), so nothing here is consulted.

Resolution order for the file (first hit wins):

  1. $ADK_EXPORT_AGENTS_YAML — explicit override, and the seam tests use.
  2. $ADK_ROOT/core/agents.yaml — the developer/runtime env var the kit sets.
  3. a `core/agents.yaml` in any ancestor of this file — true when the skill
     runs from the git checkout.

RUNTIME CAVEAT (verified, not assumed): agents.yaml is NOT copied into any
deploy target's plugin payload, so a machine that installed the plugin without
the checkout and without $ADK_ROOT resolves nothing here. That case degrades to
the per-host evidence described above — it never falls back to an embedded name
list, because a stale embedded list is the failure mode this module exists to
prevent.

stdlib only (the collector layer takes no dependencies). Only the skill NAMES
are needed, so this reads them with a narrow scan of the `skills:` block rather
than pulling in a YAML parser; a test asserts the scan agrees with PyYAML on the
real file, so the shortcut is checked against the real thing rather than
trusted.
"""

import os
import re

PATH_ENV = "ADK_EXPORT_AGENTS_YAML"

# `skills:` at column 0 opens the block; `  - name: adk-something` names one.
# Anything else at column 0 ends it (the file continues with other top-level
# keys), so a `name:` under a later section is never mistaken for a skill.
_SKILLS_KEY = re.compile(r"^skills:\s*(#.*)?$")
_TOP_LEVEL = re.compile(r"^[A-Za-z_]")
_NAME = re.compile(r"^\s*-\s*name:\s*([^\s#]+)")
_MENU_GROUP = re.compile(r"^\s*menu_group:\s*([^\s#]+)")

# agents.yaml's own taxonomy. L1 workflows are the units of delivery work a
# person starts; roles are the L2 agents a workflow delegates TO.
WORKFLOW_MENU_GROUP = "workflows"

_cache = {"key": None, "names": frozenset(), "skills": {}}


def locate():
    """Path to agents.yaml, or None when it cannot be found (see the module
    note on the deployed-runtime case)."""
    override = os.environ.get(PATH_ENV)
    if override:
        return override if os.path.isfile(override) else None
    root = os.environ.get("ADK_ROOT")
    if root:
        candidate = os.path.join(root, "core", "agents.yaml")
        if os.path.isfile(candidate):
            return candidate
    here = os.path.dirname(os.path.abspath(__file__))
    while True:
        candidate = os.path.join(here, "core", "agents.yaml")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def parse_skills(text):
    """{skill name -> menu_group} for every skill in an agents.yaml document.
    menu_group is None when the entry declares none."""
    skills, current, in_skills = {}, None, False
    for line in text.splitlines():
        if _SKILLS_KEY.match(line):
            in_skills = True
            continue
        if not in_skills:
            continue
        if _TOP_LEVEL.match(line):
            break
        m = _NAME.match(line)
        if m:
            current = m.group(1).strip()
            skills.setdefault(current, None)
            continue
        m = _MENU_GROUP.match(line)
        if m and current:
            skills[current] = m.group(1).strip()
    return skills


def parse_names(text):
    """Just the skill names in an agents.yaml document."""
    return frozenset(parse_skills(text))


def _load():
    """{name -> menu_group}, read once per (path, mtime, size) so a parse is
    deterministic and repeated collector calls do not re-read the file."""
    path = locate()
    if not path:
        _cache.update(key=None, names=frozenset(), skills={})
        return _cache["skills"]
    try:
        stat = os.stat(path)
        key = (path, stat.st_mtime, stat.st_size)
        if _cache["key"] == key:
            return _cache["skills"]
        with open(path, "r", encoding="utf-8") as fh:
            skills = parse_skills(fh.read())
    except OSError:
        return {}
    _cache.update(key=key, names=frozenset(skills), skills=skills)
    return skills


def names():
    """Every skill name the kit declares, or an empty set when agents.yaml is
    not reachable."""
    _load()
    return _cache["names"]


def workflows():
    """The L1 WORKFLOW names — the units of delivery work a person starts, i.e.
    agents.yaml entries whose menu_group is "workflows".

    Only these may be MENTIONED into a run. An L2 role (adk-product-analyst,
    adk-backend-engineer, …) is delegated to from inside a workflow, so its
    spend already belongs to that workflow's run; naming one in conversation is
    talk about the work, not the work. Activation evidence — a command block or
    a Skill call — still opens a run for ANY skill, because a user really can
    invoke a role directly."""
    return frozenset(name for name, group in _load().items()
                     if group == WORKFLOW_MENU_GROUP)


def knows(name):
    """Whether `name` is a skill this kit declares. Accepts either the bare name
    or a namespaced invocation (`adk:adk-guide`, `/adk-guide`)."""
    if not name:
        return False
    candidate = str(name).strip().lstrip("/")
    if ":" in candidate:
        candidate = candidate.rsplit(":", 1)[1]
    return candidate.strip() in names()


def reset_cache():
    """Drop the memoized read — for tests that repoint the file."""
    _cache.update(key=None, names=frozenset(), skills={})
    _pattern_cache.clear()


# ── Mention matching ─────────────────────────────────────────────────────
# A mention must be the skill's name as its own TOKEN. Python's \b is no use
# here: it treats "-" as a boundary, so \badk-write-pbi\b happily matches inside
# "adk-write-pbi-ish". The guards below exclude letters, digits, "_" and "-" on
# either side instead, which gives:
#
#   "/adk:adk-implement-pbi 2900566"        -> match   (":" and " " are outside)
#   "core/skills/adk-write-pbi/SKILL.md"   -> match   (accepted deliberately —
#                                                      deterministic beats clever)
#   "adk-write-pbi-ish", "xadk-write-pbi"  -> no match
_GUARD = "[A-Za-z0-9_-]"

_pattern_cache = {}          # frozenset of names -> compiled alternation


def _pattern_for(known):
    """A compiled pattern matching any of `known` as a standalone token, or None
    for an empty set. Longest name first, so a name that contains a shorter one
    cannot be shadowed by it."""
    if not known:
        return None
    cached = _pattern_cache.get(known)
    if cached is None:
        alternation = "|".join(re.escape(n) for n in
                               sorted(known, key=lambda n: (-len(n), n)))
        cached = _pattern_cache[known] = re.compile(
            "(?<!%s)(%s)(?!%s)" % (_GUARD, alternation, _GUARD))
    return cached


def _tokens(text, pattern):
    """Names matched in `text`, in order of FIRST appearance and de-duplicated:
    naming one skill twice in a single message is one intent, not two."""
    if not text or pattern is None:
        return []
    seen, out = set(), []
    for m in pattern.finditer(text):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def mention_pattern():
    """Standalone-token matcher for L1 WORKFLOW names — what may open a run."""
    return _pattern_for(workflows())


def mentions(text):
    """Every L1 workflow named as a standalone token in `text`."""
    return _tokens(text, mention_pattern())


def skill_pattern():
    """Standalone-token matcher for EVERY declared skill, roles included.

    Distinct from mention_pattern() on purpose: only an L1 workflow may be
    MENTIONED into a run, but any skill — roles especially — can name the ROLE a
    delegate was given."""
    return _pattern_for(names())


def skill_mentions(text):
    """Every declared skill named as a standalone token in `text`."""
    return _tokens(text, skill_pattern())
