# 09 — Safe message content and notification controls

**Outcome:** Users choose structured message fields/presets and supported silent options, preview channel rendering, and control existing appointment events per job without changing default deduplication. **Effort: medium.** Depends directly on 08 and on 02/06 revision, event and delivery identities.

Read the [shared execution contract](README.md). Backend `b5cee59` and UI `6f3f589` are historical evidence; inspect current implementations/handoffs on the existing `feat/improvements` branch. Inspect `app/notifications.py::format_slot_alert/should_notify`, `repositories.py` episode/event eligibility, adapters from 06–08, API schemas and Settings/job UI. Existing policy alerts on a new earliest-slot episode/reappearance and ignores count-only growth; preserve that behavior as the default. Do not infer policy from historical planning documents if current tests/source disagree.

## Scope and boundaries

Use **structured allowlisted fields and presets**, not an arbitrary template editor or executable placeholders. Offer job name, practitioner/practice, earliest appointment, check time, time zone and booking link where the adapter supports them. Provide per-config content defaults and optional explicit job override, plus appointment-notification enablement and supported silent delivery options. Quiet hours ship in 10; digests, reminders, count-growth events, persistent error alerts and arbitrary formatting languages remain outside this release.

## Implementation order

1. Specify the supported field/preset vocabulary, defaults and precedence. Store policy/content versions separately from search and general edit versions. A content, channel label or notification-enabled edit never increments search revision or triggers an upstream availability request. Job inheritance must be explicit and visible; unsupported adapter options are rejected rather than ignored. Existing jobs inherit a preset reproducing current content and event behavior.
2. Keep event generation tied to the existing episode semantics and 06's observed-event boundary even when routing is off. Job event controls decide whether newly observed eligible appointment events route to selected channels; turning notification off cancels unsent appointment deliveries with a safe reason. Reenabling applies prospectively: an unchanged same-slot confirmation cannot revive a cancelled row or create a delivery for an already observed episode. Do not reset `target_alert_state` or sent history on policy changes, and do not introduce a count-growth trigger as an accidental side effect of a more general event UI.
3. Define queued-work behavior precisely. Persist the effective content/silent options with delivery creation so retry rendering is stable. Cosmetic changes affect future events; existing queued work uses its saved rendering contract. Enabling/disabling affects eligibility at claim. Destination changes continue to follow 06 cancellation/version rules. Same-destination credential repair must not regenerate a new event merely to adopt different message text.
4. Implement one pure structured event-to-content renderer with channel-specific output. Escape HTML/untrusted values, preserve time-zone-aware appointment/check timestamps, bound lengths and handle missing optional values deliberately. Telegram/ntfy/email may support distinct presentation, while the generic webhook's fixed versioned JSON remains stable. Optional presentation data must use a documented compatible extension or an explicit new contract version, never silently replace its event schema.
5. Add Settings preset/field controls and synthetic per-channel previews; show job inheritance/override and supported silent behavior. Preview and real delivery invoke the same renderer. Test sends use saved effective configuration via the shared idempotent asynchronous operation; no preview performs a network send. Label field availability and fallback/truncation so users can predict actual messages.
6. Keep legacy CLI configuration behavior explicitly separate unless an existing common renderer can be reused without changing CLI delivery policy. Document deliberate CLI/API-worker differences. This chunk does not migrate the legacy CLI into the database or promise Settings controls affect that independent execution path.

## Direct acceptance checks

- Existing new-earliest/reappearance/count-only tests retain current semantics under default presets; observe while off then enable/confirm same slot stays silent, while genuine disappear/reappear can notify.
- Compare synthetic preview and dispatcher output for each adapter using identical event/options, including missing fields and overlength content.
- Inject HTML/header-shaped names and invalid option keys; output escapes them and schemas reject unsupported options.
- Edit content/routing/event controls: no search revision increment or check intent; a queued retry keeps saved content while disable cancels eligibility.
- Show inherited versus explicit job options after reload and conflict resolution; saved test uses the displayed effective contract.

**Done/handoff:** Demonstrate safe controls and previews through Settings/job UI and stub delivery. Record policy/content schemas, version precedence, queued rendering and no-replay semantics for 10. Main risks are policy edits duplicating episodes, confusing inheritance, and preview/send mismatch. Validation should target these behaviors rather than add broad unrelated formatting tests.
