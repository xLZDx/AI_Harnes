# architecture-contract: failure modes

Load on demand. Each line is a defect class seen in practice.

- Two writers for one fact with no reconciliation.
- A synchronous call chain that couples failure domains the design says are independent.
- 'Scalable' claimed without a number or an assumption.
- Abstraction introduced for a single caller.
