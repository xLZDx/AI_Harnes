#!/usr/bin/env python3
"""
When a governed workflow's run is OVER.

A run needs an end, not just a start. Without one, spend accrues to whatever
opened last until the next invocation — so a finished authoring run quietly
absorbs hours of unrelated engineering that happened after it.

A workflow that ships a Studio Live cue sheet declares its own end state there.
It may do so three ways, and a run closes when ANY of them fires:

  * `completes_run`         globs for the artifact(s) whose appearance means
                            this run reached its terminal state.
  * `completes_run_command` regexes matched against a shell command line.
  * `completes_run_tool`    exact tool names whose invocation IS the end state.

The last two exist because a path glob can only see files written by a
Write-family TOOL. A skill whose terminal artifact is produced by a subprocess
(`adk-export-session` writes its HTML from `python3 export_session.py`) or whose
end state is an external side effect rather than a file (opening a pull request,
creating work items) had no way to declare an ending at all, so every one of its
runs stayed open and absorbed whatever followed. Measured across 207 sessions,
every one of 21 `adk-export-session` runs closed only at the next boundary.

The narrower rule below — that a Bash command line is not a sound way to infer
WHICH FILE was created — still stands and is unchanged. These two kinds do not
infer a file: they match an event the skill itself nominated as its ending. That
is the same trust model as a path glob, applied to a different signal.

Three properties make all three trustworthy:

  * it is the SKILL's own statement about itself, not an inference. Stage order
    is not a substitute — several sheets end on a stage whose files are written
    continuously from early in the run, so "last stage" is simply wrong.
  * it is deterministic. A path, a command line or a tool name appearing in a
    tool call either matches the declaration or it does not. No model in the path.
  * it is SCOPED to the declaring workflow. A signal only closes a run of the
    skill that nominated it, so another skill's terminal command appearing
    mid-run means nothing — see `complete()` in usage_attribution.

Two deliberate non-features:

  * NO idle or no-touch rule. A run left open because the user stopped for the
    night and resumed in the morning is still that workflow's spend. A timeout
    would misattribute exactly that case, which is the common one.
  * NO completion artifact invented where the skill has none. A sheet with no
    honest terminal artifact simply omits `completes_run` and its runs close the
    old way, at the next boundary.

ASYMMETRY, STATED: only governed workflows have cue sheets, so only they can
close early. A non-kit skill (rj-document, a personal skill) has no sheet and its
run closes at the next boundary — governed workflows are measured exactly,
ad-hoc skills are estimated. That is a real difference in precision and worth
knowing when reading the numbers.

Resolution order for the cue-sheet directory, mirroring skill_registry:

  1. $ADK_EXPORT_CUE_SHEETS  — explicit override, and what the tests use.
  2. $ADK_ROOT/studio-live/cue-sheets
  3. a `studio-live/cue-sheets` in any ancestor of this file (the git checkout).

RUNTIME CAVEAT (same as agents.yaml): cue sheets are not copied into any deploy
target's plugin payload, so a machine running the plugin without the checkout and
without $ADK_ROOT resolves nothing here and every run closes at the next
boundary. Degradation, never invention.

stdlib only.
"""

import glob as globlib
import json
import os
import re

PATH_ENV = "ADK_EXPORT_CUE_SHEETS"

# Tool inputs that name a file the agent WROTE. A shell redirect could write one
# too, but a Bash command line is not a deterministic statement about which file
# was created, so it is not read here — a missed close is better than a wrong one.
WRITE_TOOLS = {
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path", "file_path"),
}

# Tool inputs that carry a shell command line. Matched against
# `completes_run_command` regexes — never parsed for filenames, which is the
# inference the WRITE_TOOLS note above rules out.
COMMAND_TOOLS = {
    "Bash": ("command",),
}

# The three signal kinds a cue sheet may declare, and the cue-sheet key each
# reads. Order is the order they are evaluated; any one firing closes the run.
SIGNAL_KINDS = (
    ("path", "completes_run"),
    ("command", "completes_run_command"),
    ("tool", "completes_run_tool"),
)

_cache = {"key": None, "rules": {}}


def locate():
    """The cue-sheet directory, or None when it cannot be found."""
    override = os.environ.get(PATH_ENV)
    if override:
        return override if os.path.isdir(override) else None
    root = os.environ.get("ADK_ROOT")
    if root:
        candidate = os.path.join(root, "studio-live", "cue-sheets")
        if os.path.isdir(candidate):
            return candidate
    here = os.path.dirname(os.path.abspath(__file__))
    while True:
        candidate = os.path.join(here, "studio-live", "cue-sheets")
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def _to_regex(pattern):
    """A workspace-relative glob, anchored at the END of a path.

    Cue sheets name artifacts relative to their workspace (`created-pbis.json`,
    `pbis/pbi-*.json`), and a tool call reports an absolute path. Rather than
    guess where the workspace root is, the glob is matched against the TAIL of
    the path at a directory boundary — so `created-pbis.json` matches
    `/w/.adk/pbis/run-1/created-pbis.json` and `report.html` does not match
    `/w/final-report.html`.

    `**` crosses directories, `*` and `?` do not."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile(r"(?:^|.*/)" + "".join(out) + r"$")


def _compile_kind(kind, values):
    """Compile one declaration list into matchers for its kind."""
    out = []
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if kind == "path":
            out.append(_to_regex(value))
        elif kind == "command":
            # A free regex, not a glob: a command line is not a path, and the
            # useful patterns are substrings ("export_session\\.py"). Anchoring
            # would force every sheet to spell out the whole invocation, which
            # changes with every flag. An unparseable pattern is dropped rather
            # than raised — a missed close beats a crashed export.
            try:
                out.append(re.compile(value))
            except re.error:
                continue
        elif kind == "tool":
            # Exact, case-sensitive: tool names are logged verbatim and a
            # partial match here would let `Bash` close on `BashOutput`.
            out.append(value)
    return out


def _load():
    """{skill -> {kind -> [matcher, ...]}} from every cue sheet that declares
    any completion signal. Read once per (dir, newest mtime) so a parse is
    deterministic and repeated collector calls do not re-read the tree.

    Keyed on the sheet's DIRECTORY, not its `workflow` field. Runs are keyed on
    the skill, and a skill with several flows gives each sheet a suffixed
    workflow id (`adk-technical-analyst-enterprise-services`) that no run is
    ever named after — keying on it silently yields a rule nothing can match."""
    directory = locate()
    if not directory:
        _cache.update(key=None, rules={})
        return _cache["rules"]
    files = sorted(globlib.glob(os.path.join(directory, "*", "*.json")))
    try:
        key = (directory, tuple((f, os.stat(f).st_mtime) for f in files))
    except OSError:
        return {}
    if _cache["key"] == key:
        return _cache["rules"]
    rules = {}
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                sheet = json.load(fh)
        except (OSError, ValueError):
            continue
        workflow = sheet.get("workflow")
        if not workflow:
            continue
        for kind, sheet_key in SIGNAL_KINDS:
            declared = sheet.get(sheet_key) or []
            if not isinstance(declared, list):
                continue
            compiled = _compile_kind(kind, declared)
            if compiled:
                rules.setdefault(os.path.basename(os.path.dirname(path)), {}) \
                     .setdefault(kind, []).extend(compiled)
    _cache.update(key=key, rules=rules)
    return rules


def completes(workflow, kind="path"):
    """The compiled completion matchers of one kind for `workflow`, or []."""
    return _load().get(workflow, {}).get(kind, [])


def workflows_with_completion():
    """Every workflow id that declares a completion signal of any kind."""
    return frozenset(_load())


def active_kinds():
    """The signal kinds some loaded cue sheet actually declares.

    A scan that collected every kind regardless would build a ("tool", name)
    tuple for EVERY tool call in a transcript — hundreds of thousands across a
    large store — and then test each against every watched workflow. Measured,
    that turned an 18s aggregate into 231s. Collecting only the kinds something
    could possibly match keeps the common case (paths only) at its old cost and
    makes an undeclared kind free."""
    kinds = set()
    for per_kind in _load().values():
        kinds.update(per_kind)
    return frozenset(kinds)


def workflows_by_kind(kind):
    """The workflows declaring a signal of `kind` — so a scanner tests a signal
    only against sheets that could match it, rather than against all of them."""
    return tuple(sorted(w for w, per_kind in _load().items()
                        if per_kind.get(kind)))


def _fields(table, tool_name, tool_input):
    if not isinstance(tool_input, dict):
        return ()
    out = []
    for field in table.get(tool_name) or ():
        value = tool_input.get(field)
        if isinstance(value, str) and value:
            out.append(value)
    return tuple(out)


def written_paths(tool_name, tool_input):
    """The file path(s) this tool call wrote, or ()."""
    return _fields(WRITE_TOOLS, tool_name, tool_input)


def command_lines(tool_name, tool_input):
    """The shell command line(s) this tool call ran, or ()."""
    return _fields(COMMAND_TOOLS, tool_name, tool_input)


def signals(tool_name, tool_input, kinds=None):
    """Every completion signal one tool call emits, as (kind, value) pairs.

    Stateless and workflow-agnostic on purpose: this says what happened, and
    `closes_signal` decides whether it ended anything. One call can emit several
    — a Bash call is both a "tool" signal and a "command" signal.

    `kinds` narrows collection to the kinds a caller can actually use (see
    `active_kinds`); omitted, every kind is collected."""
    if not tool_name:
        return ()
    out = []
    if kinds is None or "path" in kinds:
        out += [("path", p) for p in written_paths(tool_name, tool_input)]
    if kinds is None or "command" in kinds:
        out += [("command", c) for c in command_lines(tool_name, tool_input)]
    if kinds is None or "tool" in kinds:
        out.append(("tool", tool_name))
    return tuple(out)


def closes_signal(workflow, kind, value):
    """Whether this signal means `workflow`'s run has reached its end state."""
    if not workflow or not value:
        return False
    matchers = completes(workflow, kind)
    if not matchers:
        return False
    if kind == "tool":
        return value in matchers
    if kind == "path":
        normalized = str(value).replace("\\", "/")
        return any(m.match(normalized) for m in matchers)
    return any(m.search(value) for m in matchers)


def closes(workflow, path):
    """Whether writing `path` means `workflow`'s run has reached its end state."""
    return closes_signal(workflow, "path", path)


def reset_cache():
    """Drop the memoized read — for tests that repoint the directory."""
    _cache.update(key=None, rules={})
