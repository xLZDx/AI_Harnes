# observability: failure modes

Load on demand. Each line is a defect class seen in practice.

- Errors logged without an id that joins them to the request.
- Metric counts successes only, so a stall looks healthy.
- Alert exists but nobody owns it.
- High-cardinality labels that take down the metrics store.
