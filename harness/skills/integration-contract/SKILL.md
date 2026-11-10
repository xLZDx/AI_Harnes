---
name: integration-contract
description: Review external API, connector and message-boundary behavior. Use for third-party APIs, webhooks, connectors and queues.
---

# integration-contract

## Apply when
External APIs, connectors, webhooks and message boundaries.

## Method
1. Define auth (scopes, rotation), versioning and compatibility, timeouts, retry/idempotency, pagination and rate-limit handling.
2. Handle malformed, partial, duplicate and out-of-order payloads; define behavior during provider outage.
3. Preserve provenance across retries and replays.
4. Contract tests with recorded fixtures plus one live-sandbox smoke where permitted; secrets only via env/secret store.

## Required output
Contract table per endpoint/message, failure behavior and the tests that pin it.

## Do not
Trust the provider's documented schema; assume exactly-once delivery; log credentials or tokens.

Typical failure modes: `references/failure-modes.md` (read only when the work hits one of them).
