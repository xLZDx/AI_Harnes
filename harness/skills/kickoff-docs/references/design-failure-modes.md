# technical-design: failure modes

Load on demand. Each line is a defect class seen in practice.

- Designing a new mechanism parallel to an existing working one.
- Failure modes written only for the happy dependency set (no partial failure, no restart).
- A migration plan with no rollback or no verification query.
- 'TBD' hidden inside the interface definition instead of listed as an open decision.
