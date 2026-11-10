# db-change-contract: failure modes

Load on demand. Each line is a defect class seen in practice.

- Check-then-insert race instead of a unique constraint.
- Index added on a hot write table with no write-cost estimate.
- Column dropped in the same release that stops writing it.
- RLS bypassed by a privileged connection used for ordinary requests.
