# integration-contract: failure modes

Load on demand. Each line is a defect class seen in practice.

- Pagination cursor lost on retry, producing gaps or duplicates.
- Rate-limit response treated as a hard failure and retried instantly.
- Webhook handled before signature verification.
- Provider field renamed silently - unknown fields dropped with no alarm.
