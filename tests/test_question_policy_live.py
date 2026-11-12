"""Tests for the live PermissionRequest handler of harness/hooks/question_policy.py (S7-T1).
The hook runs as a subprocess with a temp state folder (CLAUDE_QUESTION_POLICY_STATE), a config.json and a fake
transcript. Expected values come from the acceptance criteria, not from the code."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "harness" / "hooks" / "question_policy.py"
REGISTRY = ROOT / "harness" / "hooks" / "registry.json"
PY = "C:/Python314/python.exe"
FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + ": " + name + ("" if cond else "  " + detail))
    if not cond:
        FAILS.append(name)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def q(question="Pick the chart style for the report?", header="Chart", rec="Bar chart (Recommended)",
      rec_desc="Simple bars", other="Line chart", other_desc="Lines"):
    return {"question": question, "header": header, "multiSelect": False,
            "options": [{"label": rec, "description": rec_desc}, {"label": other, "description": other_desc}]}


class Sandbox:
    def __init__(self, cfg=None):
        self.dir = Path(tempfile.mkdtemp(prefix="qp_live_"))
        self.state = self.dir / "state"
        self.state.mkdir()
        self.transcript = self.dir / "t.jsonl"
        old = {"type": "assistant", "timestamp": "2020-01-01T00:00:00+00:00",
               "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}
        self.transcript.write_text(json.dumps(old) + "\n", encoding="utf-8")
        c = {"pending_minutes": 0.05, "poll_seconds": 0.2}
        c.update(cfg or {})
        (self.state / "config.json").write_text(json.dumps(c), encoding="utf-8")

    def append(self, row):
        with self.transcript.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def ledger(self):
        f = self.state / "auto_answers.jsonl"
        if not f.exists():
            return []
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]

    def actions(self):
        return [r.get("action") for r in self.ledger()]

    def run(self, questions, tool_name="AskUserQuestion", tuid="toolu_1", transcript=True, event="PermissionRequest"):
        env = dict(os.environ)
        env.pop("CLAUDE_QUESTION_POLICY", None)
        env["CLAUDE_QUESTION_POLICY_STATE"] = str(self.state)
        payload = {"hook_event_name": event, "session_id": "sess-" + uuid.uuid4().hex[:6], "tool_name": tool_name,
                   "tool_use_id": tuid, "tool_input": {"questions": questions}}
        if transcript:
            payload["transcript_path"] = str(self.transcript)
        p = subprocess.run([PY, "-s", "-X", "utf8", str(HOOK), "--harness-client=claude"], input=json.dumps(payload),
                           capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        return p.returncode, p.stdout.strip()

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def later(delay, fn):
    t = threading.Timer(delay, fn)
    t.start()
    return t


# 1. defaults and registry timeout
sb = Sandbox()
sb.state.joinpath("config.json").unlink()  # no config: pure defaults
rc, out = sb.run([q(question="Delete the old logs?")])
acts = sb.actions()
seen = [r for r in sb.ledger() if r.get("action") == "pending-seen"]
check("default pending_mode is live (pending-seen mode=live)", seen and seen[0].get("mode") == "live", str(sb.ledger()))
check("default: risky question kept for operator immediately", rc == 0 and out == ""
      and "pending-kept-for-operator-destructive" in acts, "rc=%s out=%r acts=%s" % (rc, out, acts))
sb.close()

# defaults for minutes: read DEFAULTS from the module in a clean interpreter
env = dict(os.environ)
env["CLAUDE_QUESTION_POLICY_STATE"] = tempfile.gettempdir()
code = ("import sys,json;sys.path.insert(0,%r);import question_policy as m;print(json.dumps(m.DEFAULTS))"
        % str(HOOK.parent))
p = subprocess.run([PY, "-s", "-c", code], capture_output=True, text=True, env=env, timeout=60)
try:
    d = json.loads(p.stdout.strip().splitlines()[-1])
except Exception:
    d = {}
check("DEFAULTS idle_minutes == 10", d.get("idle_minutes") == 10, p.stdout + p.stderr)
check("DEFAULTS pending_minutes == 10", d.get("pending_minutes") == 10, str(d))
check("DEFAULTS pending_mode == live", d.get("pending_mode") == "live", str(d))

reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
ents = [h for h in reg["hooks"] if h.get("event") == "PermissionRequest" and h.get("script") == "question_policy.py"]
check("registry has exactly one PermissionRequest entry for question_policy.py", len(ents) == 1, str(ents))
if ents:
    need = 10 * 60 + 60
    check("registry PermissionRequest timeout >= pending_minutes*60+60 (%d)" % need, ents[0]["timeout"] >= need,
          "timeout=%s" % ents[0]["timeout"])

# 2. only AskUserQuestion is answered
sb = Sandbox()
rc, out = sb.run([q()], tool_name="Bash")
check("other tool name: no output", rc == 0 and out == "", "rc=%s out=%r" % (rc, out))
check("other tool name: no ledger rows", sb.ledger() == [], str(sb.ledger()))
rc, out = sb.run([q()], tool_name="askuserquestion")
check("tool name differing in case: no output", rc == 0 and out == "", out)
sb.close()

# 3. risk-class questions are not answered (English and Russian), in header / question / recommended option
RISKY = [
    ("EN header delete", q(header="Delete logs")),
    ("EN question credentials", q(question="Should we store the credentials in the repo?")),
    ("EN question token", q(question="Use the new token for the bot?")),
    ("EN recommended label rotate", q(rec="Rotate keys now (Recommended)")),
    ("EN recommended desc money", q(rec_desc="Moves real money to the exchange")),
    ("EN question production", q(question="Ship this to production?")),
    ("EN question access", q(question="Give the bot access to the folder?")),
    ("EN header money", q(header="Money")),
    ("RU header удалить", q(header="Удалить файлы")),
    ("RU question пароль", q(question="Сменить пароль администратора?")),
    ("RU question токен", q(question="Выпустить новый токен?")),
    ("RU recommended label ротация", q(rec="Ротация ключей (Recommended)")),
    ("RU recommended desc деньги", q(rec_desc="Списывает деньги со счёта")),
    ("RU question доступ", q(question="Дать доступ к серверу?")),
    ("RU question продакшн", q(question="Выкатить на продакшн?")),
]
sb = Sandbox()
for name, qq in RISKY:
    before = len(sb.ledger())
    t0 = time.time()
    rc, out = sb.run([qq])
    new = [r.get("action") for r in sb.ledger()[before:]]
    check("risky not answered: " + name, rc == 0 and out == ""
          and "pending-kept-for-operator-destructive" in new and "auto-accepted" not in new,
          "rc=%s out=%r new=%s" % (rc, out, new))
sb.close()

# risky text only in a NON-recommended option does not block (contract: recommended option is what runs)
sb = Sandbox()
rc, out = sb.run([q(other="Delete everything", other_desc="Wipe it")])
check("risk word only in non-recommended option: still answered", '"allow"' in out, "out=%r" % out)
sb.close()

# neutral question is answered with the recommended label after pending_minutes
sb = Sandbox()
t0 = time.time()
rc, out = sb.run([q(), q(question="Which colour theme?", header="Theme", rec="Dark (Recommended)", other="Light")])
elapsed = time.time() - t0
try:
    d = json.loads(out)
except ValueError:
    d = {}
hso = d.get("hookSpecificOutput", {})
dec = hso.get("decision", {})
ans = (dec.get("updatedInput") or {}).get("answers")
check("neutral: PermissionRequest allow decision", hso.get("hookEventName") == "PermissionRequest"
      and dec.get("behavior") == "allow", out[:200])
check("neutral: answers are the recommended labels of EVERY question",
      ans == {"Pick the chart style for the report?": "Bar chart (Recommended)",
              "Which colour theme?": "Dark (Recommended)"}, str(ans))
check("neutral: waited about pending_minutes (3 s), not instantly", 2.5 <= elapsed < 30, "elapsed=%.1f" % elapsed)
check("neutral: original questions preserved in updatedInput", len((dec.get("updatedInput") or {}).get("questions", [])) == 2)

# 5. ledger row
rows = [r for r in sb.ledger() if r.get("action") == "auto-accepted"]
check("ledger: one auto-accepted row", len(rows) == 1, str(sb.ledger()))
if rows:
    check("ledger: via == pending-live", rows[0].get("via") == "pending-live", str(rows[0]))
    check("ledger: row carries the answers", rows[0].get("answers") == ans, str(rows[0]))
sb.close()

# no recommended option -> never answered
sb = Sandbox()
bad = {"question": "Pick?", "header": "H", "multiSelect": False,
       "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]}
rc, out = sb.run([bad])
check("no recommended option: no output", rc == 0 and out == "", out)
sb.close()

# missing transcript: never answered blindly
sb = Sandbox()
rc, out = sb.run([q()], transcript=False)
check("no transcript: no output", rc == 0 and out == "", out)
check("no transcript: ledger pending-skip-no-transcript", "pending-skip-no-transcript" in sb.actions(), str(sb.actions()))
sb.close()

# 4. operator answers meanwhile
sb = Sandbox({"pending_minutes": 0.1})  # 6 s window
timer = later(1.0, lambda: sb.append({"type": "user", "timestamp": now_iso(),
                                      "message": {"role": "user", "content": "I choose myself"}}))
rc, out = sb.run([q()])
timer.join()
check("human message during wait: nothing answered", rc == 0 and out == "", "rc=%s out=%r" % (rc, out))
check("human message during wait: ledger pending-answered-by-operator",
      "pending-answered-by-operator" in sb.actions() and "auto-accepted" not in sb.actions(), str(sb.actions()))
sb.close()

sb = Sandbox({"pending_minutes": 0.1})
timer = later(1.0, lambda: sb.append({"type": "user", "timestamp": now_iso(), "message": {
    "role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "answered"}]}}))
rc, out = sb.run([q()], tuid="toolu_1")
timer.join()
check("tool_result of the question: nothing answered", rc == 0 and out == "", "rc=%s out=%r" % (rc, out))
check("tool_result of the question: ledger pending-answered-by-operator",
      "pending-answered-by-operator" in sb.actions() and "auto-accepted" not in sb.actions(), str(sb.actions()))
sb.close()

# an unrelated tool_result (other tool_use_id) must NOT stop the auto-answer
sb = Sandbox()
timer = later(0.8, lambda: sb.append({"type": "user", "timestamp": now_iso(), "message": {
    "role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_OTHER", "content": "x"}]}}))
rc, out = sb.run([q()], tuid="toolu_1")
timer.join()
check("unrelated tool_result: question still auto-answered", '"allow"' in out, "out=%r acts=%s" % (out, sb.actions()))
sb.close()

# operator message BEFORE the question started does not cancel
sb = Sandbox()
sb.append({"type": "user", "timestamp": "2021-01-01T00:00:00+00:00", "message": {"role": "user", "content": "earlier"}})
rc, out = sb.run([q()])
check("old human message does not cancel the auto-answer", '"allow"' in out, "out=%r" % out)
sb.close()

# 6. config typo disables the handler
for mode in ("lvie", "off", ""):
    sb = Sandbox({"pending_mode": mode})
    t0 = time.time()
    rc, out = sb.run([q()])
    check("pending_mode %r: no output" % mode, rc == 0 and out == "", out)
    check("pending_mode %r: no ledger rows, returns at once" % mode, sb.ledger() == [] and time.time() - t0 < 2.5,
          str(sb.ledger()))
    sb.close()

# kill switch
sb = Sandbox()
env_off = dict(os.environ, CLAUDE_QUESTION_POLICY="off", CLAUDE_QUESTION_POLICY_STATE=str(sb.state))
p = subprocess.run([PY, "-s", "-X", "utf8", str(HOOK)], input=json.dumps({
    "hook_event_name": "PermissionRequest", "session_id": "s", "tool_name": "AskUserQuestion", "tool_use_id": "t",
    "transcript_path": str(sb.transcript), "tool_input": {"questions": [q()]}}),
    capture_output=True, text=True, encoding="utf-8", env=env_off, timeout=30)
check("kill switch: no output", p.returncode == 0 and p.stdout.strip() == "", p.stdout)
sb.close()

if FAILS:
    print("FAILED: %d failing" % len(FAILS))
    sys.exit(1)
print("OK: 0 failing")
