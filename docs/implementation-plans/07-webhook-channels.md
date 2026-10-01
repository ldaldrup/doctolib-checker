# 07 — ntfy and fixed-contract outbound webhooks

**Outcome:** Settings can create multiple ntfy and generic HTTPS destinations; jobs select them alongside Telegram, with independent test and event deliveries. **Effort: medium–large.** Depends directly on 06 and its encrypted secret, event, delivery, version and test-operation contracts. Both adapters ship in this chunk.

Read the [shared execution contract](README.md). Revalidate backend `b5cee59`, UI `6f3f589`, then inspect the implementation left by 06. Entry points include `app/notifications.py`, the new dispatcher/adapter module, settings/channel API schemas/routes, repository delivery methods and UI Settings/job multiselect. Reuse existing mutation and status contracts; do not add a second queue or a generic HTTP automation engine.

## Scope and boundaries

Support ntfy's documented publish shape and a generic **POST with fixed versioned JSON**. Require HTTPS, bounded payload/response sizes and one bounded attempt per dispatch turn. Support no auth or typed bearer/basic auth with encrypted values, only where supported by the adapter; no free-form header editor. No user scripts, arbitrary methods/bodies/headers, redirects, inbound webhooks or receiver-delivery guarantees. Check current official ntfy documentation during implementation, and make mock fixtures reflect the chosen documented contract.

## Implementation order

1. Publish the fixed JSON contract: schema version, stable event ID, trigger/revision, job/target display fields, earliest slot, check time, time zone and booking link. Decide which personal fields are necessary; use the same sanitized event data as Telegram. Generic 2xx means receiver acceptance. ntfy has a distinct renderer/response classifier, not a misleading generic-body preset. Preserve event ID across retries and use a documented idempotency mechanism only when supported.
2. Treat endpoint URLs, including topic paths/query components, as sensitive. Store encrypted endpoint/auth data using 06's mechanism; API reads expose a safe destination label and bounded host summary, never the full URL. Build explicit keep/replace/clear operations and prospective destination-version behavior. Render previews from synthetic data and do not expose decrypted destinations through browser history or diagnostic errors.
3. Implement endpoint validation and **connect-time enforcement**. Reject credentials in URLs, unsupported ports/schemes, malformed hosts, loopback/link-local/metadata and unintended private ranges for both IPv4/IPv6. Resolve and connect only to validated addresses while retaining TLS verification and SNI for the original hostname, or use an equivalently verified egress boundary. A DNS preflight followed by an ordinary re-resolving request is insufficient. Never disable certificate verification to achieve IP binding. Disable redirects and never forward credentials to a new host.
4. Deliberate self-hosted destinations use an exact operator-controlled host/port/address allowlist, applied to real and test delivery. Do not expose a broad “allow private addresses” toggle in Settings. Validate resolved addresses against this policy on each attempt; record safe policy-rejection categories. If the available HTTP client cannot enforce connection binding correctly, use a proven transport or explicit verified egress implementation before shipping; do not silently weaken the gate.
5. Implement both adapters through 06's dispatcher. Classify credential/policy failures as action required, transient/timeouts and rate limits within existing bounds, and unknown acceptance as uncertain. Parse only bounded safe response details; never persist raw bodies or request URLs. Success of one channel cannot suppress another.
6. Extend Settings type selection, destination/auth input, supported options, synthetic preview and saved-config asynchronous test. Extend job selection with adapter labels and the shared disabled/deleted behaviors. Report “accepted by endpoint,” with clear next action for failed/uncertain work. Use existing idempotent test-operation polling.

## Direct acceptance checks

- A mixed Telegram + two ntfy + generic event produces independently tracked deliveries with stable event IDs and correct distinct payloads.
- Local fake transport verifies POST JSON, response/body/time limits, status classification and retries without external sends.
- Block IPv4/IPv6 private, loopback, link-local, mixed DNS answers, DNS rebinding and redirects; verify original-host TLS behavior and exact allowlist exceptions.
- Exercise saved test and real send through the identical endpoint policy, including endpoint changes after queueing.
- Confirm full notification endpoints, auth tokens and raw response bodies are absent from API config reads/status/logs. Private booking URLs stay out of general operational diagnostics; authenticated job/result views and explicitly selected outgoing message payloads may contain their documented booking links.

**Done/handoff:** Demonstrate both Settings adapters and mixed job routing; record JSON version, supported auth, ntfy documentation references, transport enforcement and operator allowlist configuration. Main risks are SSRF/DNS rebinding, endpoint-secret disclosure and confusing remote acceptance with device receipt. Keep provider smoke tests explicitly separate from local verification.
