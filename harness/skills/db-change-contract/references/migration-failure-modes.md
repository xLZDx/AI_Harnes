# migration: failure modes

Load on demand. Each line is a defect class seen in practice.

- Backfill restarts from zero after a crash.
- Verification counts rows but not content.
- Rollback assumes data the migration already overwrote.
- Migration holds a table lock during peak traffic.
