# reliability-contract: failure modes

Load on demand. Each line is a defect class seen in practice.

- Retry storm after a dependency outage (no jitter, no budget).
- Message acknowledged before the side effect is durable.
- Latest-attempt selection wrong after a retry.
- Shutdown drops in-flight work with no record.
