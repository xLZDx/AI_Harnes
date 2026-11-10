---
name: security-evidence-contract
description: Review reachable security risk with evidence and realistic actor paths. Use on touched trust boundaries or explicit security review.
---

# security-evidence-contract

## Apply when
A touched trust boundary (auth, tenancy, input parsing, files, network egress, secrets, CI) or an explicit security review.

## Method
1. For each finding name the actor, entry point, trust boundary crossed, sensitive sink and impact.
2. Check authn/authz, tenant/object isolation (IDOR), injection (SQL/command/template), SSRF and DNS rebinding, secrets in code/logs/artifacts, file/archive/XML import, deserialization, supply chain (pins, lockfiles, CI permissions).
3. Trace or reproduce a reachable path; rate severity by reachability x impact.

## Required output
Finding: attacker precondition, exact path (file:line), impact, fix, regression test.

## Do not
Report unreachable patterns as high severity; paste secrets into reports; run exploits against live systems without authorization.

Typical failure modes: `references/failure-modes.md` (read only when the work hits one of them).
