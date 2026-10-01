# 08 — Editable SMTP transport and named email recipients

**Outcome:** Settings manages one SMTP transport and multiple named single-recipient destinations; jobs route to Email1/Email2 alongside existing channels. Test and event deliveries show independent outcomes. **Effort: medium–large.** Depends directly on 07 and particularly 03/06 delivery isolation, encrypted secrets and named-config behavior.

Read the [shared execution contract](README.md). Treat backend `b5cee59` and UI `6f3f589` as historical evidence; use current integrated source on the existing `feat/improvements` branch, then inspect the adapters/configuration added by 06–07. Entry points are `app/notifications.py` or successor adapters, the dispatcher, repository event/delivery methods, API channel/settings schemas and Settings/job UI. Follow current contracts and handoffs instead of building separate email alert records.

## Scope and boundaries

The **SMTP transport is editable in Settings**: hostname/port, supported TLS mode, username/password, sender and enabled state. Each named email channel has exactly one recipient and refers to that transport. Ship plain text plus escaped HTML with consistent content. No inbound mailbox, mail server, multiple transports, multi-recipient delivery ambiguity, bounce processor or inbox-receipt promise.

## Implementation order

1. Define transport and recipient contracts. Transport secrets use 06 encryption and explicit keep/replace/clear. Recipient/name input rejects CR/LF and unsupported address forms; sender and display fields cannot inject headers. Keep recipients out of broad operational logs. Validate port/TLS modes and require certificate/hostname validation; never fall back silently to plaintext when STARTTLS fails. Separate syntactic saved state from verified connection/test outcome.
2. Reuse 07's destination/egress security principles for user-editable SMTP hosts: block local/metadata/unintended private targets and enforce verified connect-time addresses with original hostname TLS, allowing deliberate private SMTP only through an exact operator exception. The SMTP implementation must not become a new unguarded server-side request surface. Apply bounds to connection, TLS/authentication and send phases, as well as message size.
3. Store one transport version and distinguish credential rotation from server/sender identity changes. Same-destination credential repair can recover still-fresh action-required deliveries explicitly. Server/sender identity changes invalidate unsent work rather than silently redirecting it. Reevaluate recipient/transport enabled state and version at delivery claim; a cached SMTP connection cannot bypass configuration changes. Close/recreate connections when versions change.
4. Add an SMTP adapter with one recipient per delivery. Classify authentication/recipient rejection, temporary responses, timeouts and uncertain acceptance through the existing dispatcher. SMTP acceptance after DATA is “accepted by mail server”; a connection loss around final acceptance is uncertain and must not trigger blind duplicate delivery. Use a stable Message-ID derived from delivery/event identity for diagnostics, without promising server deduplication. Preserve the shared freshness, paused-manual capability and episode cancellation rules.
5. Build Settings transport editing and named recipient CRUD, masked reload, explicit clear/disable and affected-job feedback. Extend job selection with enabled usable email configs. Add saved-config asynchronous test sends through the shared idempotent test system; validate the saved transport and recipient used by the operation. A test email does not mutate appointment episode history. Present safe authentication/recipient/timeout errors and the accepted-versus-inbox distinction.
6. Render synthetic preview and actual multipart content from the shared event representation. Use a plain-text fallback and HTML escaping; preserve explicit time zone and safe booking links. Document provider sender verification, authentication requirements and external-key recovery, with configuration examples containing no live secrets. Verify current official provider guidance for a later chosen provider.

## Direct acceptance checks

- A fake SMTP server/transport receives one message per event for each of two named recipients, alongside a successful Telegram delivery.
- One rejected recipient and one temporary transport failure do not resend successful channels or block availability checks.
- Validate TLS failure, unsupported downgrade, auth rejection, header injection and private/changed DNS enforcement.
- Keep/replace/clear transport secrets and rotate/disable during queued work; verify explicit cancellation/recovery and no cached-connection bypass.
- Simulate lost test response and loss after DATA; operation remains idempotent and uncertain acceptance is visible.
- Inspect text/HTML previews and persisted errors for escaping, time zones and absent credentials/raw transcript.

**Done/handoff:** Demonstrate editable transport → two recipient configs → job selection → independent fake delivery status. Record transport version rules, timeout classification and accepted-status wording for later policy work. Major risks are ambiguous SMTP acceptance, configuration races and provider deliverability. A controlled real-provider send remains separate from offline verification and requires an authorized recipient.
