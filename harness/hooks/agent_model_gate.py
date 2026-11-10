#!/usr/bin/env python3
"""PreToolUse gate for the Agent tool (Control Plane v3).

Policy (control-plane/model_policy.json): automatic agents run on the `sonnet` alias or an explicit
Sonnet id >= 5.5. Opus is never automatic: it needs per-run operator consent and no consent receipt
mechanism exists, so it fails closed. Haiku/Fable and anything else are denied. Agents marked
`deprecated: true` are not automatically runnable. Non-Agent tools are never touched.

Claude Code only. In Codex the subagent model is fixed in config.toml ([agents] default_subagent_model) and
spawn_agent has no Claude model alias or agent frontmatter to check, so this hook is a deliberate no-op for
Codex payloads (detected with _agentio.client); it stays safe to register there.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import _agentio as agentio
except Exception:  # pragma: no cover - helper missing: behave as Claude
    agentio = None

HOME = Path(os.environ.get("CLAUDE_CONTROL_PLANE_HOME") or (Path.home() / ".claude"))
SONNET_ID = re.compile(r"^claude-sonnet-(\d+)-(\d+)(?:-\d{8})?$")
EFFORTS = {"low", "medium", "high", "xhigh"}


def model_ok(model: str) -> bool:
    m = (model or "").strip().lower()
    if m == "sonnet":
        return True
    hit = SONNET_ID.match(m)
    return bool(hit) and (int(hit.group(1)), int(hit.group(2))) >= (5, 5)


def deny(reason: str) -> None:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}},
        separators=(",", ":")))
    raise SystemExit(0)


def candidate_paths(name: str, cwd: Path):
    seen = set()
    roots = []
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        roots.append(Path(env_root))
    roots += [cwd, *cwd.parents]
    for root in roots:
        p = root / ".claude" / "agents" / f"{name}.md"
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            yield p
    yield HOME / "agents" / f"{name}.md"


def frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return {}
    m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        kv = re.match(r"^([A-Za-z_][\w-]*):\s*([^\r\n]*)$", line)
        if kv:
            out[kv.group(1)] = re.sub(r"\s+#.*$", "", kv.group(2)).strip().strip("'\"")
    return out


def load_deprecation_rules() -> list:
    try:
        data = json.loads((HOME / "control-plane" / "deprecated_agents.json").read_text(encoding="utf-8-sig"))
        return [r for r in data.get("rules", []) if isinstance(r, dict)]
    except OSError:
        return []  # no rules file configured
    except ValueError:
        raise RuntimeError("deprecated_agents.json is malformed")  # surfaces as fail-closed for Agent calls


def effective_subagent_model() -> str:
    """Model that platform-level inheritance gives an agent that declares no model."""
    env = os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL")
    if env:
        return env
    try:
        s = json.loads((HOME / "settings.json").read_text(encoding="utf-8-sig"))
        return str((s.get("env") or {}).get("CLAUDE_CODE_SUBAGENT_MODEL") or "")
    except (OSError, ValueError):
        return ""


def decide(data: dict) -> None:
    if agentio is not None and agentio.client(data) == "codex":
        # Codex: the subagent model comes from config.toml ([agents] default_subagent_model), not from a
        # per-call model alias or agent frontmatter, so the Claude Sonnet-alias rules do not apply. No-op.
        return
    if data.get("tool_name") != "Agent":
        return
    ti = data.get("tool_input") or {}
    name = str(ti.get("subagent_type") or "").strip()
    explicit = str(ti.get("model") or "").strip().lower()
    label = name or "<unknown>"

    if explicit and not model_ok(explicit):
        deny(f"Control Plane v3: explicit model '{explicit}' is blocked for agent {label}. Automatic agents "
             "use the 'sonnet' alias (Sonnet 5.5+). Opus runs only after explicit operator consent for that "
             "run, only at effort=high, and never automatically; Haiku/Fable are not permitted.")

    cwd = Path(str(data.get("cwd") or os.getcwd()))
    resolved = next((p for p in candidate_paths(name, cwd) if name and p.is_file()), None)
    fm = frontmatter(resolved) if resolved else {}

    if fm.get("deprecated", "").lower() == "true":
        succ = fm.get("superseded_by") or "the canonical replacement"
        deny(f"Control Plane v3: agent '{name}' ({resolved}) is deprecated and not automatically runnable. "
             f"Use {succ}, or ask the operator for an explicit historical comparison.")

    where = (str(resolved or "") + "|" + str(cwd)).lower().replace("/", "\\")
    for rule in load_deprecation_rules():
        succ = (rule.get("agents") or {}).get(name)
        if succ and any(str(m).lower() in where for m in rule.get("path_markers", [])):
            deny(f"Control Plane v3: legacy agent '{name}' ({rule.get('project', 'project')}) is deprecated "
                 f"history and not automatically runnable. Use canonical '{succ}', or ask the operator for an "
                 "explicit historical comparison.")

    declared = fm.get("model", "")
    if declared and not model_ok(declared):
        deny(f"Control Plane v3: {name} resolves to model '{declared}' in {resolved}. Automatic agents must "
             "use 'sonnet' (Sonnet 5.5+). Opus needs explicit per-run operator consent and effort=high.")
    effort = fm.get("effort", "")
    if effort and effort not in EFFORTS:
        deny(f"Control Plane v3: {name} declares invalid effort '{effort}' in {resolved}.")

    if not declared and not explicit and not model_ok(effective_subagent_model()):
        deny(f"Control Plane v3: {label} declares no model and CLAUDE_CODE_SUBAGENT_MODEL is not a permitted "
             "Sonnet, so it would inherit the parent model (possibly Opus). Failing closed.")

    if name and resolved is None:
        sys.stderr.write(f"agent_model_gate: '{name}' has no definition under the project or ~/.claude/agents; "
                         "allowed as a built-in/plugin agent (it inherits the enforced Sonnet subagent model).\n")


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16")
        elif b"\x00" in raw:
            text = raw.decode("utf-16-le")
        else:
            text = raw.decode("utf-8-sig")
        data = json.loads(text or "{}")
    except (UnicodeError, json.JSONDecodeError):
        sys.stderr.write("agent_model_gate: malformed hook JSON; failing closed\n")
        return 2
    if not isinstance(data, dict):
        sys.stderr.write("agent_model_gate: hook payload is not an object; failing closed\n")
        return 2
    try:
        decide(data)
    except SystemExit:
        raise
    except Exception as exc:  # unknown internal error on an Agent call must not silently allow
        if data.get("tool_name") == "Agent":
            sys.stderr.write(f"agent_model_gate: internal error {exc!r}; failing closed\n")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
