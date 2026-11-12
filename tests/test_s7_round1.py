"""Tests for the GPT-PM round-1 fixes (F1 exposure attribution, F2 rotation transaction, F3 plan boundary, F4 ledger ownership, F5 CORE.md).
Expected outcomes come from the round-1 finding rules, not from the code's output. Everything runs against temp folders (state, metrics,
config and home are overridden for every subprocess); secret-looking values are built from pieces at run time.
Run: C:\\Python314\\python.exe -I tests/test_s7_round1.py"""
import json
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hv2  # noqa: E402
import test_s7_fixbatch as fb  # noqa: E402  (shared sandbox/run helpers; sets temp env overrides before any hook import)

sp = fb.sp
import session_close_gate as scg  # noqa: E402

AWS = "AK" + "IA" + "7Q2W9E4R" + "1T6Y3U8I"
AWS2 = "AK" + "IA" + "3M8N5B2V" + "6C9X4Z1L"
GENERIC = fb.GENERIC
OK = "Exit code: 0\nfine"


# ---------------------------------------------------------------- helpers
def cf(tok):
    return '-H "Authorization: Bearer $%s" https://api.cloudflare.com/client/v4/user/tokens' % tok


def cf_create(tok="CF_A_TOKEN"):
    return "curl -X POST " + cf(tok)


def cf_delete(tok="CF_A_TOKEN"):
    return "curl -X DELETE " + cf(tok) + "/abc"


def cf_read(tok="CF_A_TOKEN"):
    return 'curl -s -H "Authorization: Bearer $%s" https://api.cloudflare.com/client/v4/user/tokens/verify' % tok


def post(sb, cmd, resp, env=None):
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s1",
               "cwd": str(sb["root"]), "transcript_path": str(sb["root"] / "missing.jsonl"), "tool_response": resp}
    rc, out, err = fb.run_script(sb, "secret_guard.py", payload, extra_env=env)
    assert rc == 0, err
    return out


def pre(sb, cmd, env=None, human=None):
    tp = str(sb["root"] / "missing.jsonl")
    if human:
        tp = fb.write_transcript(sb, human)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s1",
               "cwd": str(sb["root"]), "transcript_path": tp}
    rc, out, err = fb.run_script(sb, "secret_guard.py", payload, extra_env=env)
    assert rc == 0, err
    try:
        return json.loads(out)["hookSpecificOutput"].get("permissionDecision", "")
    except (ValueError, KeyError, TypeError):
        return ""


def pre_reason(sb, cmd, env=None):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s1",
               "cwd": str(sb["root"]), "transcript_path": str(sb["root"] / "missing.jsonl")}
    rc, out, err = fb.run_script(sb, "secret_guard.py", payload, extra_env=env)
    assert rc == 0, err
    try:
        o = json.loads(out)["hookSpecificOutput"]
        return o.get("permissionDecision", ""), o.get("permissionDecisionReason", "")
    except (ValueError, KeyError, TypeError):
        return "", ""


def recs(sb):
    return fb.sg_records(sb)


def seed(sb, rows):
    f = sb["state"] / "evidence" / "records.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    f.write_text(json.dumps(cur + [{"at": time.time(), **r} for r in rows]), encoding="utf-8")


def invalid(cred, service, project=""):
    return {"kind": "auth", "credential_id": cred, "service": service, "project": project, "operation": "call", "class": "credential-invalid"}


def rot(step, rid, old, new, service="cloudflare", project="", age=0.0):
    r = {"kind": "rotation", "step": step, "rotation_id": rid, "old_credential_id": old, "credential_id": new, "service": service,
         "project": project, "at": time.time() - age}
    return r


# ================================================================ F1: exposure only unlocks the credential it is attributed to
def test_f1_detected_exposure_never_unlocks_a_change_but_confirmed_does():
    env = {"CF_A_TOKEN": GENERIC}
    sb = sandbox_ = fb.sandbox("r1f1_det")
    post(sb, cf_read(), OK + "\nconfig api_token=" + GENERIC, env=env)
    exp = [r for r in recs(sb) if r.get("kind") == "exposure"]
    assert len(exp) == 1 and exp[0]["state"] == "detected" and exp[0]["credential_id"] == "env:CF_A_TOKEN", exp
    assert pre(sb, cf_create(), env=env) == "deny"
    # positive control: the same flow with a known-format (confirmed) value unlocks create
    sb2 = fb.sandbox("r1f1_conf")
    env2 = {"CF_A_TOKEN": AWS}
    post(sb2, cf_read(), OK + "\nleak " + AWS, env=env2)
    assert [r["state"] for r in recs(sb2) if r.get("kind") == "exposure"] == ["confirmed"]
    assert pre(sb2, cf_create(), env=env2) == ""


def test_f1_confirmed_leak_of_a_value_that_is_not_the_env_credential_unlocks_nothing():
    sb = fb.sandbox("r1f1_other")
    env = {"CF_A_TOKEN": AWS2}  # the live credential is a different value than the leaked one
    post(sb, cf_read(), OK + "\nleak " + AWS, env=env)
    exp = [r for r in recs(sb) if r.get("kind") == "exposure"]
    assert len(exp) == 1 and exp[0]["state"] == "confirmed" and exp[0]["credential_id"].startswith("value:"), exp
    assert pre(sb, cf_create(), env=env) == "deny"
    # env variable not set at all: also unattributed
    sb2 = fb.sandbox("r1f1_unset")
    post(sb2, cf_read(), OK + "\nleak " + AWS)
    assert recs(sb2) and [r["credential_id"] for r in recs(sb2) if r["kind"] == "exposure"][0].startswith("value:")
    assert pre(sb2, cf_create()) == "deny"


def test_f1_exposure_linked_to_credential_a_does_not_unlock_credential_b():
    sb = fb.sandbox("r1f1_ab")
    env = {"CF_A_TOKEN": AWS, "CF_B_TOKEN": AWS2}
    post(sb, cf_read("CF_A_TOKEN"), OK + "\nleak " + AWS, env=env)
    assert [r["credential_id"] for r in recs(sb) if r["kind"] == "exposure"] == ["env:CF_A_TOKEN"]
    assert pre(sb, cf_create("CF_B_TOKEN"), env=env) == "deny"
    assert pre(sb, cf_create("CF_A_TOKEN"), env=env) == ""  # positive: the linked credential itself


def test_f1_standing_order_switch_disables_the_exposure_unlock():
    sb = fb.sandbox("r1f1_so")
    env = {"CF_A_TOKEN": AWS}
    post(sb, cf_read(), OK + "\nleak " + AWS, env=env)
    assert pre(sb, cf_create(), env=env) == ""
    assert pre(sb, cf_create(), env={**env, "HARNESS_ROTATION_STANDING_ORDER": "0"}) == "deny"
    assert pre(sb, cf_create(), env={**env, "HARNESS_ROTATION_STANDING_ORDER": "1"}) == ""


def test_f1_false_positives_are_not_confirmed():
    for label, text in [("too short", "token: AKIA"), ("fixture word", "fixture key " + AWS), ("example", "example key " + AWS),
                        ("generic with fixture", "api_token=" + GENERIC + " fixture")]:
        sb = fb.sandbox("r1f1_fp")
        env = {"CF_A_TOKEN": AWS}
        post(sb, cf_read(), OK + "\n" + text, env=env)
        assert [r for r in recs(sb) if r.get("kind") == "exposure"] == [], (label, recs(sb))
        assert pre(sb, cf_create(), env=env) == "deny", label
    # negative of the negative: the same AWS value without a fixture hint IS confirmed
    sb = fb.sandbox("r1f1_fp_ctl")
    post(sb, cf_read(), OK + "\nkey " + AWS, env={"CF_A_TOKEN": AWS})
    assert [r["state"] for r in recs(sb) if r.get("kind") == "exposure"] == ["confirmed"]


# ================================================================ F2: rotation_for_delete (pure function)
def test_f2_delete_matches_old_or_verified_credential_of_the_transaction():
    rows = [rot("verified", "R1", "env:A", "env:A2")]
    assert sp.rotation_for_delete(rows, "env:A", "cloudflare", "")["rotation_id"] == "R1"
    assert sp.rotation_for_delete(rows, "env:A2", "cloudflare", "")["rotation_id"] == "R1"
    assert sp.rotation_for_delete(rows, "env:OTHER", "cloudflare", "") is None  # a named credential outside the pair
    assert sp.rotation_for_delete(rows, "", "cloudflare", "") is None


def test_f2_unresolved_id_is_never_accepted_even_with_exactly_one_open_rotation():
    one = [rot("verified", "R1", "env:A", "env:A2", "gcp", "p")]
    for cred in ("keyid:ABC123", "default:gcp", ""):
        assert sp.rotation_for_delete(one, cred, "gcp", "p") is None, cred
    two = one + [rot("verified", "R2", "env:B", "env:B2", "gcp", "p")]
    for cred in ("keyid:ABC123", "default:gcp"):
        assert sp.rotation_for_delete(two, cred, "gcp", "p") is None, cred
    # named credentials still resolve with one or two open rotations
    assert sp.rotation_for_delete(one, "env:A", "gcp", "p")["rotation_id"] == "R1"
    assert sp.rotation_for_delete(two, "env:B", "gcp", "p")["rotation_id"] == "R2"
    assert sp.rotation_for_delete(two, "env:A", "gcp", "p")["rotation_id"] == "R1"
    # once one is revoked the other is still not reachable through an unresolved id
    three = two + [{"kind": "rotation", "step": "old_revoked", "rotation_id": "R1", "service": "gcp", "project": "p", "at": time.time()}]
    assert sp.rotation_for_delete(three, "keyid:ABC123", "gcp", "p") is None
    assert sp.rotation_for_delete(three, "env:B", "gcp", "p")["rotation_id"] == "R2"
    assert sp.rotation_for_delete(three, "env:A", "gcp", "p") is None


def test_f2_other_project_other_service_created_only_and_stale_rotations_do_not_authorize():
    ver = rot("verified", "R1", "env:A", "env:A2", "gcp", "p")
    assert sp.rotation_for_delete([ver], "env:A", "gcp", "p") is not None  # control
    assert sp.rotation_for_delete([ver], "keyid:ABC123", "gcp", "q") is None
    assert sp.rotation_for_delete([ver], "env:A", "gcp", "q") is None
    assert sp.rotation_for_delete([ver], "keyid:ABC123", "aws", "p") is None
    assert sp.rotation_for_delete([ver], "env:A", "aws", "p") is None
    assert sp.rotation_for_delete([rot("created", "R1", "env:A", "env:A", "gcp", "p")], "env:A", "gcp", "p") is None
    assert sp.rotation_for_delete([rot("verified", "R1", "env:A", "env:A2", "gcp", "p", age=25 * 3600)], "env:A", "gcp", "p") is None
    assert sp.rotation_for_delete([rot("verified", "R1", "env:A", "env:A2", "gcp", "p", age=23 * 3600)], "env:A", "gcp", "p") is not None


def test_f2_after_old_revoked_the_same_rotation_cannot_authorize_again():
    ver = rot("verified", "R1", "env:A", "env:A2", "gcp", "p")
    assert sp.rotation_for_delete([ver], "env:A", "gcp", "p") is not None
    revoked = {"kind": "rotation", "step": "old_revoked", "rotation_id": "R1", "service": "gcp", "project": "p", "at": time.time()}
    for cred in ("env:A", "env:A2", "keyid:ABC123"):
        assert sp.rotation_for_delete([ver, revoked], cred, "gcp", "p") is None, cred
    # a revocation of ANOTHER rotation does not close this one
    other = {**revoked, "rotation_id": "R9"}
    assert sp.rotation_for_delete([ver, other], "env:A", "gcp", "p") is not None


def test_f2_parallel_rotations_of_one_service_do_not_unlock_each_other():
    ra, rb = rot("verified", "RA", "env:A", "env:A2"), rot("verified", "RB", "env:B", "env:B2")
    assert sp.rotation_for_delete([ra, rb], "env:A", "cloudflare", "")["rotation_id"] == "RA"
    assert sp.rotation_for_delete([ra, rb], "env:B", "cloudflare", "")["rotation_id"] == "RB"
    assert sp.rotation_for_delete([ra, rb], "env:A2", "cloudflare", "")["rotation_id"] == "RA"
    assert sp.rotation_for_delete([ra, rb], "env:B2", "cloudflare", "")["rotation_id"] == "RB"
    # only A is verified: B is not unlocked, and vice versa
    assert sp.rotation_for_delete([ra], "env:B", "cloudflare", "") is None
    assert sp.rotation_for_delete([ra], "env:B2", "cloudflare", "") is None
    assert sp.rotation_for_delete([rb], "env:A", "cloudflare", "") is None
    # B has only been created, A verified: deleting B is not unlocked by A's verification
    assert sp.rotation_for_delete([ra, rot("created", "RB", "env:B", "env:B", "cloudflare", "")], "env:B", "cloudflare", "") is None


# ================================================================ F2: rotation transaction through the hook
def test_f2_post_hook_writes_created_verified_and_old_revoked_with_ids():
    sb = fb.sandbox("r1f2_flow")
    post(sb, cf_create("CF_A_TOKEN"), OK)
    created = [r for r in recs(sb) if r.get("kind") == "rotation"]
    assert len(created) == 1 and created[0]["step"] == "created", created
    c = created[0]
    assert c["rotation_id"] and c["old_credential_id"] == "env:CF_A_TOKEN" and c["credential_id"] == "env:CF_A_TOKEN" and c["project"] == "", c
    # verification attempts that must NOT count: same credential, no explicit credential (default:), another service
    post(sb, cf_read("CF_A_TOKEN"), OK)
    post(sb, "wrangler whoami", OK)
    post(sb, "gcloud projects describe p --project p --key-file=x.json", OK)
    assert [r["step"] for r in recs(sb) if r.get("kind") == "rotation"] == ["created"]
    # a successful read-only call with a different explicit credential verifies
    post(sb, cf_read("CF_A2_TOKEN"), OK)
    rows = [r for r in recs(sb) if r.get("kind") == "rotation"]
    assert [r["step"] for r in rows] == ["created", "verified"], rows
    v = rows[1]
    assert v["rotation_id"] == c["rotation_id"] and v["old_credential_id"] == "env:CF_A_TOKEN" and v["credential_id"] == "env:CF_A2_TOKEN", v
    # a FAILED call with the new credential does not verify another rotation
    post(sb, cf_create("CF_B_TOKEN"), OK)
    post(sb, cf_read("CF_B2_TOKEN"), "Exit code: 1\nERROR: 401 Unauthorized")
    assert [r["step"] for r in recs(sb) if r.get("kind") == "rotation"] == ["created", "verified", "created"]
    # successful delete of the old credential closes THIS rotation only
    post(sb, cf_delete("CF_A_TOKEN"), OK)
    rev = [r for r in recs(sb) if r.get("step") == "old_revoked"]
    assert len(rev) == 1 and rev[0]["rotation_id"] == c["rotation_id"], rev
    # a failed delete records nothing
    sb2 = fb.sandbox("r1f2_faildel")
    seed(sb2, [rot("verified", "R1", "env:CF_A_TOKEN", "env:CF_A2_TOKEN")])
    post(sb2, cf_delete("CF_A_TOKEN"), "Exit code: 1\nERROR: boom")
    assert [r for r in recs(sb2) if r.get("step") == "old_revoked"] == []


def test_f2_verification_in_another_project_does_not_verify():
    sb = fb.sandbox("r1f2_proj")
    post(sb, "gcloud iam service-accounts keys create n.json --iam-account x --project p --key-file=a.json", OK)
    created = [r for r in recs(sb) if r.get("kind") == "rotation"]
    assert len(created) == 1 and created[0]["project"] == "p" and created[0]["old_credential_id"] == "file:a.json", created
    post(sb, "gcloud projects describe q --project q --key-file=a2.json", OK)
    assert [r["step"] for r in recs(sb) if r.get("kind") == "rotation"] == ["created"]
    post(sb, "gcloud projects describe p --project p --key-file=a2.json", OK)  # control: same project verifies
    assert [r["step"] for r in recs(sb) if r.get("kind") == "rotation"] == ["created", "verified"]


def test_f2_delete_gate_through_the_hook_unresolved_id_and_scope():
    del_key = "gcloud iam service-accounts keys delete ABC123 --iam-account x --project p"
    ev = lambda svc="gcp", proj="p": invalid("keyid:ABC123", svc, proj)  # noqa: E731
    # one open rotation of A, delete of a bare key id (maybe a different key B): denied, identity unresolved
    sb = fb.sandbox("r1f2_one")
    seed(sb, [rot("verified", "R1", "env:A", "env:A2", "gcp", "p"), ev()])
    assert pre(sb, del_key) == "deny"
    # two open rotations of the same service and project: the unresolved key id is denied
    sb = fb.sandbox("r1f2_two")
    seed(sb, [rot("verified", "R1", "env:A", "env:A2", "gcp", "p"), rot("verified", "R2", "env:B", "env:B2", "gcp", "p"), ev()])
    assert pre(sb, del_key) == "deny"
    # verified rotation of another project
    sb = fb.sandbox("r1f2_proj_other")
    seed(sb, [rot("verified", "R1", "env:A", "env:A2", "gcp", "q"), ev()])
    assert pre(sb, del_key) == "deny"
    # verified rotation of another service
    sb = fb.sandbox("r1f2_svc_other")
    seed(sb, [rot("verified", "R1", "env:A", "env:A2", "aws", "p"), ev()])
    assert pre(sb, del_key) == "deny"
    # revoked rotation cannot authorize a second delete
    sb = fb.sandbox("r1f2_revoked")
    seed(sb, [rot("verified", "R1", "env:A", "env:A2", "gcp", "p"),
              {"kind": "rotation", "step": "old_revoked", "rotation_id": "R1", "service": "gcp", "project": "p"}, ev()])
    assert pre(sb, del_key) == "deny"
    # only created, never verified
    sb = fb.sandbox("r1f2_created")
    seed(sb, [rot("created", "R1", "env:A", "env:A", "gcp", "p"), ev()])
    assert pre(sb, del_key) == "deny"


def test_f2_one_open_rotation_of_a_keyid_delete_of_other_key_is_denied_with_identity_reason():
    del_key = "gcloud iam service-accounts keys delete ABC123 --iam-account x --project p"
    sb = fb.sandbox("r1f2_keyid_reason")
    seed(sb, [rot("verified", "R1", "env:A", "env:A2", "gcp", "p"), invalid("keyid:ABC123", "gcp", "p")])
    dec, reason = pre_reason(sb, del_key)
    assert dec == "deny" and "credential-identity-unresolved" in reason, (dec, reason)


def test_f2_one_open_rotation_delete_by_the_old_credentials_env_name_is_allowed():
    sb = fb.sandbox("r1f2_envname_ok")
    seed(sb, [rot("verified", "R1", "env:CF_A_TOKEN", "env:CF_A2_TOKEN"), invalid("env:CF_A_TOKEN", "cloudflare")])
    assert pre_reason(sb, cf_delete("CF_A_TOKEN"))[0] == ""
    # control: a different env name than the pair is still denied
    seed(sb, [invalid("env:CF_C_TOKEN", "cloudflare")])
    assert pre_reason(sb, cf_delete("CF_C_TOKEN"))[0] == "deny"


def test_f2_two_parallel_rotations_end_to_end_delete_by_env_name():
    sb = fb.sandbox("r1f2_par")
    post(sb, cf_create("CF_A_TOKEN"), OK)
    post(sb, cf_read("CF_A2_TOKEN"), OK)
    post(sb, cf_create("CF_B_TOKEN"), OK)
    # delete of B is not unlocked while only A is verified (B created, not verified)
    seed(sb, [invalid("env:CF_A_TOKEN", "cloudflare"), invalid("env:CF_B_TOKEN", "cloudflare")])
    assert pre(sb, cf_delete("CF_B_TOKEN")) == "deny"
    assert pre(sb, cf_delete("CF_A_TOKEN")) == ""
    post(sb, cf_read("CF_B2_TOKEN"), OK)
    steps = [(r["step"], r["old_credential_id"], r.get("credential_id")) for r in recs(sb) if r.get("kind") == "rotation"]
    assert ("verified", "env:CF_A_TOKEN", "env:CF_A2_TOKEN") in steps and ("verified", "env:CF_B_TOKEN", "env:CF_B2_TOKEN") in steps, steps
    ids = {r["old_credential_id"]: r["rotation_id"] for r in recs(sb) if r.get("step") == "verified"}
    assert len(set(ids.values())) == 2, ids
    # both verified: each named delete is allowed (the verifications did not cross over)
    seed(sb, [invalid("env:CF_A_TOKEN", "cloudflare"), invalid("env:CF_B_TOKEN", "cloudflare")])
    assert pre(sb, cf_delete("CF_A_TOKEN")) == "" and pre(sb, cf_delete("CF_B_TOKEN")) == ""
    # delete A succeeds: it revokes A's transaction, B stays open
    post(sb, cf_delete("CF_A_TOKEN"), OK)
    rev = [r["rotation_id"] for r in recs(sb) if r.get("step") == "old_revoked"]
    assert rev == [ids["env:CF_A_TOKEN"]], (rev, ids)
    seed(sb, [invalid("env:CF_A_TOKEN", "cloudflare"), invalid("env:CF_B_TOKEN", "cloudflare")])
    assert pre(sb, cf_delete("CF_A_TOKEN")) == "deny", "A's revoked transaction must not authorize a second delete"
    assert pre(sb, cf_delete("CF_B_TOKEN")) == ""
    post(sb, cf_delete("CF_B_TOKEN"), OK)
    assert sorted(r["rotation_id"] for r in recs(sb) if r.get("step") == "old_revoked") == sorted(ids.values())
    seed(sb, [invalid("env:CF_B_TOKEN", "cloudflare")])
    assert pre(sb, cf_delete("CF_B_TOKEN")) == "deny"


# ================================================================ F3: plan_first_gate first-change boundary
PLAN = "1. Read the current handler code\n2. Change the handler to return the new value\n3. Run the unit tests to verify the change"


def pf_transcript(sb, items, human="Implement the feature"):
    """items: ('text', str) or ('tool', name, input, result) in order; tool calls get a success result."""
    rows = [{"type": "user", "timestamp": "2026-11-12T10:00:00Z", "message": {"role": "user", "content": human}}]
    for it in items:
        if it[0] == "text":
            rows.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": it[1]}]}})
        else:
            tid = "toolu_" + uuid.uuid4().hex[:10]
            rows.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": it[1], "input": it[2]}]}})
            rows.append({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": it[3], "is_error": False}]}})
    p = sb["root"] / (uuid.uuid4().hex[:8] + ".jsonl")
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8", newline="\n")
    return str(p)


def edit_decision(sb, tp):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Edit",
               "tool_input": {"file_path": "D:/proj/src/app.py", "old_string": "a", "new_string": "b"},
               "session_id": "sess-P", "transcript_path": tp, "cwd": str(sb["root"])}
    rc, out, err = fb.run_script(sb, "plan_first_gate.py", payload)
    assert rc == 0, err
    return fb.is_deny(out), out


def test_f3_plan_after_read_only_shell_calls_is_accepted_for_the_first_edit():
    sb = fb.sandbox("r1f3_ok")
    items = [("tool", "Bash", {"command": "git status"}, "clean"), ("tool", "Bash", {"command": "rg handler src"}, "src/app.py:1"),
             ("text", PLAN)]
    assert edit_decision(sb, pf_transcript(sb, items))[0] is False
    # the Edit itself already present in the transcript (it is the first mutation): the plan before it still counts
    sb = fb.sandbox("r1f3_ok2")
    items2 = items + [("tool", "Edit", {"file_path": "D:/proj/src/app.py", "old_string": "a", "new_string": "b"}, "ok")]
    assert edit_decision(sb, pf_transcript(sb, items2))[0] is False


def test_f3_plan_written_after_a_real_mutation_is_not_accepted_as_text_list():
    sb = fb.sandbox("r1f3_late")
    items = [("tool", "Bash", {"command": "mkdir x"}, ""), ("text", PLAN)]
    denied, out = edit_decision(sb, pf_transcript(sb, items))
    assert denied is True, out
    # the same plan before the mutation passes (control: the boundary is what changes the verdict)
    sb = fb.sandbox("r1f3_early")
    items = [("text", PLAN), ("tool", "Bash", {"command": "mkdir x"}, "")]
    assert edit_decision(sb, pf_transcript(sb, items))[0] is False


def test_f3_read_only_calls_and_no_plan_still_deny_and_invalid_plan_is_rejected():
    sb = fb.sandbox("r1f3_none")
    items = [("tool", "Bash", {"command": "git status"}, "clean"), ("tool", "Bash", {"command": "rg x"}, "")]
    assert edit_decision(sb, pf_transcript(sb, items))[0] is True
    sb = fb.sandbox("r1f3_bad")
    items = [("tool", "Bash", {"command": "git status"}, "clean"), ("text", "1. Do the thing now please")]  # one step only
    assert edit_decision(sb, pf_transcript(sb, items))[0] is True


# ================================================================ F4: ledger ownership independent of state
def mk_ledger(root, states, extra=None):
    tasks = []
    for st in states:
        t = fb.mk_task("T-" + st, st)
        tasks.append(t)
    for t in extra or []:
        tasks.append(t)
    led = {"version": 1, "project": "p", "approval": None, "events": [], "sprints": [
        {"id": "S1", "title": "s", "branch": "b", "state": "open", "opened": "2026-11-12T00:00:00+02:00", "tasks": tasks}]}
    (root / ".harness").mkdir(parents=True, exist_ok=True)
    (root / ".harness" / "ledger.json").write_text(json.dumps(led), encoding="utf-8", newline="\n")


def with_log(sb):
    scg.LOG = sb["root"] / "scg.jsonl"
    return scg.LOG


def unknown_events(log):
    if not log.exists():
        return []
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
    return sorted((r["task"], r["state"]) for r in rows if r.get("verdict") == "ownership-unknown")


NON_TODO = ["implemented", "verified", "not_run", "failed", "in_progress"]


def test_f4_with_own_ids_only_own_tasks_are_returned_whatever_their_state():
    sb = fb.sandbox("r1f4_mine")
    log = with_log(sb)
    mk_ledger(sb["root"], ["todo"] + NON_TODO + ["done"], extra=[{**fb.mk_task("T-blocked", "implemented"), "blocked": {"class": "OPERATOR"}}])
    got = scg.ledger_open_tasks(sb["root"], {"T-implemented"})
    assert got == [("T-implemented", "implemented")], got
    assert unknown_events(log) == [], "ownership is known: no ownership-unknown event"
    for st in NON_TODO + ["todo"]:  # each state on its own: owned -> kept, everything else (todo included) skipped
        assert scg.ledger_open_tasks(sb["root"], {"T-" + st}) == [("T-" + st, st)], st
    # owned but done / blocked: never returned
    assert scg.ledger_open_tasks(sb["root"], {"T-done"}) == []
    assert scg.ledger_open_tasks(sb["root"], {"T-blocked"}) == []


def test_f4_without_own_ids_all_open_tasks_are_kept_and_logged_as_ownership_unknown():
    sb = fb.sandbox("r1f4_nomine")
    log = with_log(sb)
    mk_ledger(sb["root"], ["todo"] + NON_TODO + ["done"], extra=[{**fb.mk_task("T-blocked", "failed"), "blocked": {"class": "EXTERNAL"}}])
    got = scg.ledger_open_tasks(sb["root"], set())
    assert sorted(got) == sorted([("T-todo", "todo")] + [("T-" + s, s) for s in NON_TODO]), got
    assert unknown_events(log) == sorted([("T-todo", "todo")] + [("T-" + s, s) for s in NON_TODO]), unknown_events(log)  # none for done or blocked


def test_f4_mine_in_progress_and_foreign_todo_returns_only_mine():
    sb = fb.sandbox("r1f4_ip_todo")
    log = with_log(sb)
    mk_ledger(sb["root"], [], extra=[fb.mk_task("T1", "in_progress"), fb.mk_task("T2", "todo")])
    assert scg.ledger_open_tasks(sb["root"], {"T1"}) == [("T1", "in_progress")]
    assert unknown_events(log) == []


def test_f4_mine_done_and_foreign_todo_returns_nothing():
    sb = fb.sandbox("r1f4_done_todo")
    with_log(sb)
    mk_ledger(sb["root"], [], extra=[fb.mk_task("T1", "done"), fb.mk_task("T2", "todo")])
    assert scg.ledger_open_tasks(sb["root"], {"T1"}) == []
    assert scg.ledger_open_tasks(sb["root"], {"T2"}) == [("T2", "todo")]  # control: owning the todo task does hold the session


def test_f4_done_never_listed_todo_listed_only_when_ownership_unknown_or_own():
    sb = fb.sandbox("r1f4_todo")
    log = with_log(sb)
    mk_ledger(sb["root"], ["todo", "done"])
    assert scg.ledger_open_tasks(sb["root"], set()) == [("T-todo", "todo")]
    assert unknown_events(log) == [("T-todo", "todo")]
    assert scg.ledger_open_tasks(sb["root"], {"T-todo"}) == [("T-todo", "todo")]
    assert scg.ledger_open_tasks(sb["root"], {"T-unrelated"}) == []
    # unreadable ledger is None, not an empty list
    (sb["root"] / ".harness" / "ledger.json").write_text("{ nope", encoding="utf-8")
    assert scg.ledger_open_tasks(sb["root"], set()) is None


# ================================================================ F5: CORE.md says Codex stderr visibility is not confirmed
def test_f5_core_md_says_codex_stderr_is_not_confirmed_visible():
    text = (hv2.SRC / "harness" / "rules" / "CORE.md").read_text(encoding="utf-8")
    flat = " ".join(text.split()).lower()
    assert "codex" in flat and "stderr" in flat
    i = flat.index("stderr")
    window = flat[max(0, i - 200):i + 200]
    assert "not confirmed" in window and "visible" in window, window
    # negative: it must not claim the operator sees the Codex stderr line
    assert "stderr, which is shown" not in flat and "stderr and is visible" not in flat


if __name__ == "__main__":
    sys.exit(hv2.run_all(globals()))
