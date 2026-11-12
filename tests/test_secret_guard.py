"""Tests for harness/hooks/secret_guard.py (PreToolUse + PostToolUse). The hook runs as a subprocess with a temp
HARNESS_HOOK_STATE. Expected outcomes come from docs/S7_HOOK_FIXES_PLAN.md S7-T5 and the task cases.
All secret-looking values are built at run time from pieces, so this file holds no token-shaped literal."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "harness" / "hooks" / "secret_guard.py"
PY = "C:/Python314/python.exe"

# Format matches, but none of the fixture-hint words (fake/example/dummy/test/...) so the scanner treats them as real.
AWS = "AK" + "IA" + "7Q2W9E4R" + "1T6Y3U8I"
GHP = "gh" + "p_" + ("aB3dE5gH7j" * 4)
GENERIC = "Xk29Pq7L" + "mN4rTs8U" + "vW3yZa6B" + "1c5D"
VALUES = [AWS, GHP, GENERIC]

FAILS = []
TMPS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL") + ": " + name + ("" if cond else "  " + detail))
    if not cond:
        FAILS.append(name)


def new_state():
    d = Path(tempfile.mkdtemp(prefix="sg_state_"))
    TMPS.append(d)
    return d


def records_file(state):
    return state / "evidence" / "records.json"


def load_records(state):
    f = records_file(state)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def save_records(state, rows):
    f = records_file(state)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rows), encoding="utf-8")


def transcript(state, human):
    p = state / "transcript.jsonl"
    row = {"type": "user", "timestamp": "2026-11-12T10:00:00Z", "message": {"role": "user", "content": human}}
    p.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return str(p)


def run(payload, state, env=None, client_arg=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    e["HARNESS_HOOK_STATE"] = str(state)
    e["PYTHONIOENCODING"] = "utf-8"
    e.update(env or {})
    argv = [PY, "-s", "-X", "utf8", str(HOOK)] + ([client_arg] if client_arg else [])
    p = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", env=e, timeout=60)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def pre(state, command=None, tool="Bash", file_path=None, human=None, env=None):
    ti = {"file_path": file_path} if file_path else {"command": command}
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti, "session_id": "s1", "cwd": str(state),
               "transcript_path": transcript(state, human) if human else str(state / "missing.jsonl")}
    return run(payload, state, env)


def post(state, command, response, tool="Bash", client_arg=None, env=None):
    payload = {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"command": command}, "session_id": "s1",
               "cwd": str(state), "transcript_path": str(state / "missing.jsonl"), "tool_response": response}
    return run(payload, state, env, client_arg)


def decision(out):
    try:
        return json.loads(out)["hookSpecificOutput"].get("permissionDecision", "")
    except (ValueError, KeyError, TypeError):
        return ""


def reason(out):
    try:
        return json.loads(out)["hookSpecificOutput"].get("permissionDecisionReason", "")
    except (ValueError, KeyError, TypeError):
        return ""


def expect(name, want, state, command=None, **kw):
    rc, out, err = pre(state, command, **kw)
    got = decision(out)
    check(name, rc == 0 and got == want, "rc=%s want=%s got=%r out=%r err=%r" % (rc, want or "allow", got, out[:200], err[:200]))
    return out


S = new_state()

# ---------------------------------------------------------------- printing: deny
for cmd, tool in [("cat .env", "Bash"), ("type .env", "Bash"), ("Get-Content .env", "PowerShell"),
                  ("cat config/prod.tfvars", "Bash"), ("Get-Content infra\\prod.tfvars", "PowerShell"),
                  ("cat ~/.ssh/id_rsa", "Bash"), ("type C:\\Users\\u\\.ssh\\id_rsa", "Bash"),
                  ("cat service-account.json", "Bash"), ("Get-Content keys\\my-service-account.json", "PowerShell")]:
    expect("deny print: " + cmd, "deny", S, cmd, tool=tool)

# printing: masked / name-only -> allow
for cmd in ["grep -c KEY .env", "grep -l TOKEN .env", "cut -d= -f1 .env", "cat .env | wc -l", "cat prod.tfvars | cut -d= -f1",
            "grep -c private_key service-account.json"]:
    expect("allow name-only: " + cmd, "", S, cmd)

expect("deny Read tool on .env", "deny", S, tool="Read", file_path="D:/proj/.env")
expect("deny Read tool on id_rsa", "deny", S, tool="Read", file_path="C:/Users/u/.ssh/id_rsa")
expect("allow Read tool on README.md", "", S, tool="Read", file_path="D:/proj/README.md")

# environment dumps
for cmd, tool in [("printenv", "Bash"), ("env", "Bash"), ("set", "Bash"), ("Get-ChildItem env:", "PowerShell")]:
    expect("deny env dump: " + cmd, "deny", S, cmd, tool=tool)
for cmd in ["printenv | cut -d= -f1", "env | cut -d= -f1"]:
    expect("allow filtered env: " + cmd, "", S, cmd)

expect("deny printenv API_TOKEN", "deny", S, "printenv API_TOKEN")
expect("deny echo $CLOUDFLARE_API_TOKEN", "deny", S, "echo $CLOUDFLARE_API_TOKEN")
expect("allow [ -n ] test of token var", "", S, '[ -n "$CLOUDFLARE_API_TOKEN" ] && echo set')
expect("allow echo ${#VAR} length", "", S, "echo ${#CLOUDFLARE_API_TOKEN}")

# access token printing
expect("deny gcloud print-access-token alone", "deny", S, "gcloud auth print-access-token")
expect("allow token captured in variable", "", S, "TOKEN=$(gcloud auth print-access-token)")
expect("allow token piped to curl", "", S, "gcloud auth print-access-token | curl -s -H @- https://api.example.org/x")

# inline interpreters
expect("deny python -c printing os.environ secret", "deny", S,
       "python -c \"import os; print(os.environ['API_TOKEN'])\"")
expect("allow python -c printing len()", "", S,
       "python -c \"import os; print(len(os.environ['API_TOKEN']))\"")

# ---------------------------------------------------------------- money
for cmd in ["gcloud billing accounts list", "gcloud billing projects link p --billing-account X",
            "gcloud compute instances create vm1 --zone z", "aws ec2 run-instances --image-id i --count 1"]:
    out = expect("deny money: " + cmd, "deny", S, cmd)
    check("money message names money: " + cmd, "money" in reason(out).lower(), reason(out))
expect("allow git commit with word payment", "", S, 'git commit -m "payment module"')

# ---------------------------------------------------------------- credential change gate
DEL = "gcloud iam service-accounts keys delete ABC123 --iam-account x --project p"
CREATE = "gcloud iam service-accounts keys create k.json --iam-account x --project p"
GCP_READ = "gcloud projects describe p --project p"
AWS_CREATE = "aws iam create-access-key --user-name u"
CF = '-H "Authorization: Bearer $CF_API_TOKEN" https://api.cloudflare.com/client/v4/user/tokens'
CF_CREATE = "curl -X POST " + CF
CF_DELETE = "curl -X DELETE " + CF + "/abc"
CF_ROLL = "curl -X PUT " + CF + "/abc/value"
CF_READ = "curl -s -H \"Authorization: Bearer $CF_API_TOKEN\" https://api.cloudflare.com/client/v4/user/tokens/verify"
UNAUTH = "Exit code: 1\nERROR: (gcloud) 401 Unauthorized: request had invalid authentication credentials"
OK_RESP = "Exit code: 0\nfine"

# a) no evidence
for cmd in [DEL, CREATE, AWS_CREATE, CF_CREATE, CF_DELETE, CF_ROLL]:
    st = new_state()
    out = expect("a) no evidence denies: " + cmd[:50], "deny", st, cmd)
    check("a) reason names the missing proof: " + cmd[:40], "no proof" in reason(out) and "no fresh auth check" in reason(out), reason(out))

# b) evidence matrix
st = new_state()
post(st, GCP_READ, UNAUTH)
rows = load_records(st)
check("b) PostToolUse records credential-invalid", any(r.get("class") == "credential-invalid" and r.get("service") == "gcp"
                                                       and r.get("project") == "p" for r in rows), str(rows))
expect("b) credential-invalid same cred/service/project allows create", "", st, CREATE)
expect("b) service A evidence does not unlock service B", "deny", st, AWS_CREATE)
expect("b) same service, other project denied", "deny", st, CREATE.replace("--project p", "--project q"))

for label, resp in [("403", "Exit code: 1\nERROR: 403 Permission denied on resource"),
                    ("timeout", "Exit code: 1\nERROR: request timed out"),
                    ("config error", "Exit code: 1\nERROR: project p not found")]:
    st = new_state()
    post(st, GCP_READ, resp)
    rows = load_records(st)
    check("b) %s is recorded but not as credential-invalid" % label, rows and all(r.get("class") != "credential-invalid" for r in rows), str(rows))
    expect("b) %s evidence does not unlock create" % label, "deny", st, CREATE)

st = new_state()
post(st, GCP_READ, UNAUTH)
rows = load_records(st)
for r in rows:
    r["at"] = time.time() - 61 * 60
save_records(st, rows)
expect("b) evidence older than 60 minutes denies", "deny", st, CREATE)
rows = load_records(st)
for r in rows:
    r["at"] = time.time() - 59 * 60
save_records(st, rows)
expect("b) evidence 59 minutes old still allows", "", st, CREATE)

st = new_state()
post(st, GCP_READ, UNAUTH)
post(st, GCP_READ, OK_RESP)
check("latest ok recorded", load_records(st)[-1].get("class") == "ok", str(load_records(st)))
expect("a later ok clears an earlier credential-invalid", "deny", st, CREATE)

# c) delete needs verified replacement
st = new_state()
post(st, GCP_READ, UNAUTH)  # default:gcp invalid
DEL_D = "gcloud iam service-accounts keys disable --iam-account x --project p"  # cred default:gcp
out = expect("c) delete with invalid evidence but no rotation record is denied", "deny", st, DEL_D)
check("c) reason mentions verified replacement", "verified replacement" in reason(out), reason(out))
rows = load_records(st)
rows.append({"kind": "rotation", "service": "gcp", "project": "p", "step": "created", "rotation_id": "gcp:1",
             "old_credential_id": "file:a.json", "credential_id": "file:a.json", "at": time.time() - 30})
save_records(st, rows)
expect("c) created but not verified still denied", "deny", st, DEL_D)
rows = load_records(st)
rows.append({"kind": "rotation", "service": "gcp", "project": "p", "step": "verified", "rotation_id": "gcp:1",
             "old_credential_id": "file:a.json", "credential_id": "file:a2.json", "at": time.time() - 10})
save_records(st, rows)
out = expect("c) created then verified still denies a default-credential delete (identity unresolved)", "deny", st, DEL_D)
check("c) reason mentions credential-identity-unresolved", "credential-identity-unresolved" in reason(out), reason(out))
DEL_NAMED = DEL_D + " --key-file=a.json"  # authenticates with the rotation's old credential file; names NO target key
post(st, GCP_READ + " --key-file=a.json", UNAUTH)
out = expect("c) authenticating with the old credential file does not identify the delete target (denied)", "deny", st, DEL_NAMED)
check("c) reason mentions credential-identity-unresolved (auth file)", "credential-identity-unresolved" in reason(out), reason(out))
DEL_ID_A = "gcloud iam service-accounts keys delete ABC123 --iam-account x --project p --key-file=a.json"
out = expect("c) target key id not recorded in the rotation record: denied despite auth by the old file", "deny", st, DEL_ID_A)
check("c) reason mentions credential-identity-unresolved (no old_key_id)", "credential-identity-unresolved" in reason(out), reason(out))
rows = load_records(st)
for r in rows:
    if r.get("kind") == "rotation" and r.get("step") == "verified":
        r["old_key_id"] = "keyid:ABC123"
save_records(st, rows)
expect("c) record carries old_key_id == target: delete allowed", "", st, DEL_ID_A)
expect("c) same record, different target key id: denied", "deny", st, DEL_ID_A.replace("ABC123", "ZZZ999"))
expect("c) same record, delete naming no key id (file auth only): denied", "deny", st, DEL_NAMED)
st_other = new_state()
post(st_other, GCP_READ + " --key-file=b.json", UNAUTH)
save_records(st_other, load_records(st_other) + [r for r in load_records(st) if r.get("kind") == "rotation"])
expect("c) delete of the recorded key but from another project's record set is not authorized by auth file b.json alone", "deny", st_other, DEL_D + " --key-file=b.json")

# c2) full flow via PostToolUse on cloudflare with confirmed exposure. The leak is attributed to env:CF_API_TOKEN only when the live
# variable in the hook's environment has the same fingerprint as the leaked value, so the hook subprocess gets CF_API_TOKEN=<leaked value>.
# Verification = a successful read-only call with an EXPLICIT credential different from the one that was created (CF_READ_NEW).
LEAK_ENV = {"CF_API_TOKEN": AWS}
CF_READ_NEW = "curl -s -H \"Authorization: Bearer $CF_NEW_API_TOKEN\" https://api.cloudflare.com/client/v4/user/tokens/verify"
st = new_state()
post(st, CF_READ, "Exit code: 0\nleak " + AWS, env=LEAK_ENV)
check("c2) exposure attributed to env:CF_API_TOKEN when live value matches", any(
    r.get("kind") == "exposure" and r.get("credential_id") == "env:CF_API_TOKEN" and r.get("state") == "confirmed" for r in load_records(st)), str(load_records(st)))
expect("c2) confirmed exposure: delete denied before verification", "deny", st, CF_DELETE, env=LEAK_ENV)
expect("c2) confirmed exposure: create allowed", "", st, CF_CREATE, env=LEAK_ENV)
post(st, CF_CREATE, OK_RESP, env=LEAK_ENV)
check("c2) create success writes rotation created", any(r.get("step") == "created" for r in load_records(st)), str(load_records(st)))
expect("c2) created without verification still denies delete", "deny", st, CF_DELETE, env=LEAK_ENV)
time.sleep(0.05)
post(st, CF_READ, OK_RESP, env=LEAK_ENV)
check("c2) read-only success with the SAME credential does not write verified", not any(r.get("step") == "verified" for r in load_records(st)), str(load_records(st)))
expect("c2) same-credential success still denies delete", "deny", st, CF_DELETE, env=LEAK_ENV)
post(st, CF_READ_NEW, OK_RESP, env=LEAK_ENV)
check("c2) read-only success with a DIFFERENT explicit credential writes verified", any(r.get("step") == "verified" for r in load_records(st)), str(load_records(st)))
out = expect("c2) after verification a curl DELETE (target not resolvable from a URL) is still denied", "deny", st, CF_DELETE, env=LEAK_ENV)
check("c2) reason mentions credential-identity-unresolved", "credential-identity-unresolved" in reason(out), reason(out))

# c3) without the matching live variable the leak is recorded as value:<fp>, not as the credential, so it unlocks nothing
st = new_state()
post(st, CF_READ, "Exit code: 0\nleak " + AWS)
check("c3) leak without matching live env is recorded as value:<fp>", any(
    r.get("kind") == "exposure" and str(r.get("credential_id", "")).startswith("value:") for r in load_records(st)), str(load_records(st)))
expect("c3) value:<fp> exposure does not unlock a change of env:CF_API_TOKEN", "deny", st, CF_CREATE)

# d) operator message
for human in ["please rotate the Cloudflare token", "Rotate the key, and revoke the old one"]:
    st = new_state()
    expect("d) operator message names rotation allows: " + human[:20], "", st, CREATE, human=human)
st = new_state()
expect("d) unrelated operator message does not allow", "deny", st, CREATE, human="please fix the build")

# e) exposure
st = new_state()
out_rc = post(st, CF_READ, "Exit code: 0\nresponse " + AWS, env=LEAK_ENV)
rows = load_records(st)
check("e) known-format secret recorded as confirmed", any(r.get("kind") == "exposure" and r.get("state") == "confirmed" for r in rows), str(rows))
expect("e) confirmed + standing order allows create", "", st, CF_CREATE, env=LEAK_ENV)
expect("e) confirmed: delete still needs verification", "deny", st, CF_DELETE, env=LEAK_ENV)
expect("e) standing order 0 -> confirmed no longer allows", "deny", st, CF_CREATE, env={**LEAK_ENV, "HARNESS_ROTATION_STANDING_ORDER": "0"})

st = new_state()
post(st, CF_READ, "Exit code: 0\nconfig api_token=" + GENERIC, env={"CF_API_TOKEN": GENERIC})
rows = load_records(st)
check("e) generic labelled value recorded as detected", any(r.get("kind") == "exposure" and r.get("state") == "detected" for r in rows), str(rows))
rows.append({"kind": "auth", "credential_id": "env:CF_API_TOKEN", "service": "cloudflare", "project": "",
             "operation": "call", "class": "permission-insufficient", "at": time.time()})
save_records(st, rows)
out = expect("e) detected + technical rights: change denied", "deny", st, CF_CREATE, env={"CF_API_TOKEN": GENERIC})
check("e) reason says detected never unlocks", "detected" in reason(out), reason(out))

# f) terraform
for cmd in ["terraform destroy -auto-approve", "terraform state rm module.x"]:
    st = new_state()
    expect("f) denied without operator: " + cmd, "deny", st, cmd)
    expect("f) allowed when operator names it: " + cmd, "", st, cmd, human="run terraform destroy and state rm now")

# ---------------------------------------------------------------- PostToolUse detection
st = new_state()
rc, out, err = post(st, "cat build.log", "log line\nkey " + GHP + "\n")
ctx = ""
try:
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
except (ValueError, KeyError):
    pass
check("post: claude gets additionalContext with containment checklist", rc == 0 and "Contain it" in ctx and "SECRET EXPOSED" in ctx, out[:200])
check("post: output does not repeat the value", GHP not in out and GHP not in err)

st = new_state()
rc, out, err = post(st, "cat build.log", "log line\nkey " + GHP + "\n", client_arg="--harness-client=codex")
check("post: codex writes to stderr, stdout empty", rc == 0 and out == "" and "SECRET EXPOSED" in err and GHP not in err, "out=%r err=%r" % (out[:100], err[:100]))

st = new_state()
post(st, CF_READ, "Exit code: 0\nleak " + AWS + " and api_token=" + GENERIC)
blob = ""
for f in st.rglob("*"):
    if f.is_file() and f.name != "transcript.jsonl":
        blob += f.read_text(encoding="utf-8", errors="replace")
check("state: no secret value in any state/log file", not any(v in blob for v in VALUES), "value leaked")
check("state: secret_guard.jsonl exists with type and fp", (st / "evidence" / "secret_guard.jsonl").exists()
      and '"fp"' in (st / "evidence" / "secret_guard.jsonl").read_text(encoding="utf-8"))
exp = [r for r in load_records(st) if r.get("kind") == "exposure"]
check("state: exposure records hold only type/fp/credential ref", exp and all({"type", "fp", "credential_id"} <= set(r) and
      not any(v in json.dumps(r) for v in VALUES) for r in exp), str(exp))

st = new_state()
rc, out, err = post(st, "cat build.log", "see example key " + AWS)
check("post: fixture-looking strings ignored", rc == 0 and out == "" and err == "" and not load_records(st), "out=%r rows=%r" % (out[:100], load_records(st)))
rc, out, err = post(st, "cat build.log", "nothing secret here")
check("post: clean output is silent", rc == 0 and out == "" and err == "")

# ---------------------------------------------------------------- switches and failure modes
st = new_state()
expect("kill switch HARNESS_SECRET_GUARD=off disables", "", st, "cat .env", env={"HARNESS_SECRET_GUARD": "off"})
rc, out, err = post(st, "cat build.log", "key " + GHP, env={"HARNESS_SECRET_GUARD": "off"})
check("kill switch disables PostToolUse", rc == 0 and out == "" and not load_records(st))

st = new_state()
records_file(st).parent.mkdir(parents=True, exist_ok=True)
records_file(st).write_text("{ not json !!", encoding="utf-8")
expect("corrupt records.json: ordinary command fails open", "", st, "git status")
expect("corrupt records.json: print pattern still denies", "deny", st, "cat .env")
expect("corrupt records.json: change without evidence still denies", "deny", st, CREATE)

for d in TMPS:
    shutil.rmtree(d, ignore_errors=True)
if FAILS:
    print("FAILED: %d failing" % len(FAILS))
    sys.exit(1)
print("OK: 0 failing")
