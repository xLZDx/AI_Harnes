# security-evidence-contract: failure modes

Load on demand. Each line is a defect class seen in practice.

- Authorization checked on the list endpoint but not on the item/download endpoint.
- Tenant id taken from the request body instead of the session.
- Input validated after the dangerous operation.
- Scanner output pasted as findings with no reachability analysis.
