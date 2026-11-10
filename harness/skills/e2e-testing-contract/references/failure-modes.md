# e2e-testing-contract: failure modes

Load on demand. Each line is a defect class seen in practice.

- Tests share an account/tenant and corrupt each other.
- Selectors bound to styling instead of roles/test ids.
- Network or clock nondeterminism treated as 'flaky browser'.
- Green run on a stale build or the wrong environment.
