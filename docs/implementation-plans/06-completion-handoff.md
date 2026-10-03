# Part 06 completion handoff

Completed locally on 3 October 2026. Grounded in the [part 05 handoff](05-completion-handoff.md), [part 06 plan](06-named-telegram-channels.md) and [shared execution contract](README.md). All implementation and reviews reuse **`feat/improvements`**, based on consolidated local `master` at `d08cbf6`. No additional branch, remote push, production migration or live provider send. Completed reviewed work was committed on the shared branch and fast-forwarded into local `master`, with the checkout returned to `feat/improvements`.

## Delivered behavior

Settings manages multiple named Telegram configurations. Stable channel IDs identify destinations; renames preserve job selections. Jobs select multiple channels and show their independent delivery states. Credentials use explicit Save and independent Keep/Replace/Clear actions. Existing polling Settings retain serialized autosave and reconciliation. Channel conflicts preserve drafts, block saves after failed refetch and require explicit review before applying the draft to the current version.

Channel creation uses an immutable idempotency key and payload. Creation and its durable outcome commit in one SQLite write transaction; there is no metadata/network step requiring a renewable reservation. Replay is resolved before evaluating credential writes, including after key loss. Expiry and new-write validation occur under that same transaction. Unknown UI creation retains its original request for explicit replay, with no browser persistent storage. Updates/deletion require positive strict `expected_version`; deletion also requires explicit confirmation, detaches current job selections and advances affected jobs' edit versions. Historical alerts retain their channel ID/name snapshot.

An observed available episode commits a logical event and its original routing snapshot atomically with the result, even with notifications off or no selected channels. Reconfirmation never attaches new recipients or automatically revives cancelled deliveries. A genuine disappearance/reappearance or changed earliest slot creates a new event. Each destination has independent attempts, retry schedules and terminal outcomes. One successful destination is untouched when another fails.

Disable, clear, delete, detach and recipient rotation cancel unsent work. An attempt already permitted to send keeps its owned completion boundary and records actual acceptance/uncertainty while retaining cancellation history. Credential repair for the same destination can explicitly recover fresh eligible failed work. Explicit CLI recovery also permits originally routed, same-destination cancelled work only with current episode, revision, freshness, selection and run capability. Sent work, removed legacy recipient identity and changed destinations cannot be redirected/replayed. Uncertain acceptance still requires duplicate-risk acknowledgement. A paused manual event grants only its original selected delivery capabilities; it never resumes polling.

## Schema, credentials and restore boundary

Schema **8** adds:

- `notification_channels`: stable ID/type/name, enabled/deleted flags, edit/destination/credential versions, encrypted bot token/chat ID, keyed destination identity and timestamps.
- `job_channels`: job selections.
- `availability_events`: unique episode identity, result/revision evidence, observed/routed timestamps and original routing snapshot.
- `channel_mutations`: atomic channel-create/import idempotency outcomes, retained seven days.
- `notification_onboarding`: durable once-only import marker.
- `channel_tests`: one-shot test identity, destination/credential versions, owner/lease, attempt evidence and safe outcome.
- `alerts` gains `event_id`, `channel_config_id`, `destination_version`, `credential_version`, `channel_name`, with uniqueness for `(event_id, channel_config_id, destination_version)`. Existing claim, attempt, epoch, freshness and paused capability fields remain.

Fernet from `cryptography>=46,<47` provides authenticated encryption. `NOTIFICATION_SECRET_KEY` is an external URL-safe base64 32-byte key shared by API and dispatcher, never stored in SQLite. Invalid syntax fails startup; missing keys block secret writes/sends, and unreadable ciphertext produces safe unusable/action-required state without rewriting data. Both token and chat ID are masked in public reads. Destination identity uses a keyed digest of bot numeric ID plus chat ID: repair of the same bot secret preserves destination version; a different bot or chat advances it and cancels old work. No provider lookup is needed during ordinary saves.

Back up the key separately in protected operator storage **before** migration. Database backup alone cannot restore decrypted credentials. Recovery must use the original key, or explicitly replace saved credentials after configuring a new key. API health and offline backup rehearsal do not prove decryption or delivery. Key replacement does not repair unreadable ciphertext automatically. Repository/application/log/browser storage contain no real credentials from this implementation.

## Explicit onboarding and migration

`POST /api/v1/channels/import-legacy` imports environment Telegram once as **Telegram1**, mapping only existing opted-in non-deleted jobs. It never imports at startup or overwrites a saved destination; repeated requests preserve credentials and mappings. Environment values remain only an optional onboarding source. Saved configurations control backend sends. The legacy JSON CLI retains its separate behavior.

Migration is quiesced: stop API, availability worker and dispatcher before transition. Schema 7 sent/cancelled rows preserve terminal history. Pending/failed/claimed unsent legacy rows retain IDs, attempt counts, claim/attempt/epoch evidence and uncertainty, but become cancelled with `migration_legacy_requires_import`. Their old recipient cannot safely be redirected to a newly imported destination. Already-observed target episodes, including those without alert rows, are seeded as consumed. Import and fresh confirmation cannot replay them. This conservative cancellation is the explicit freshness/recipient boundary for migrated work.

Install compatible API/worker/dispatcher/UI code together, configure the shared external key, and reload old browser tabs. Verify saved state and perform explicit onboarding as needed. Never mix old environment-based senders with the new dispatcher. Rollback restores the pre-upgrade database, matching key and matching application after stopping writers; no in-place schema downgrade. A restored backup cannot reconstruct provider acceptance since its snapshot.

## API and adapter entry points

- `/api/v1/channels`: list/create; `/{id}`: read/versioned update/confirmed delete.
- `/api/v1/channels/import-legacy`: explicit idempotent import.
- `/api/v1/channels/{id}/preview`: escaped synthetic formatter output; no send.
- `/api/v1/channels/{id}/tests`: idempotent saved-config test request with expected version, returns operation ID.
- `/api/v1/channel-tests/{id}`: safe bounded-operation status.
- Job create/update uses `notification_channel_ids`; public reads include masked channel metadata and actual key-dependent usability. The retained `telegram_enabled` field is the job's notification opt-in, not a credential source.

Saved tests use a **60-second claim**, a persisted single-attempt permission boundary and the existing **15-second Telegram transport budget**. Queued tests expire after seven days; terminal test idempotency retention is seven days from creation. An expired started claim becomes **unknown**; it never automatically retries. Expired unstarted ownership can be reclaimed without a network attempt. Completion fences the live owner/deadline; preparation failures remain visible without consuming a send. UI status polling stops after at most **30 lookups at 2-second spacing** and supports explicit status checks (each starts another bounded cycle) or same-key uncertain-request replay. Saved channels expose a safe latest-test summary so unknown outcomes remain visible after reload.

For part 07, reuse `ChannelOperations`, channel edit/destination/credential versions, `notification_channel_ids`, original event routing and per-destination `alerts`. The concrete Telegram adapter boundary is `channel_settings` plus `send_telegram_alert`; `run_test_once` and `DeliveryService` guard permission before transport. Add the new adapter's validation/transport behavior without weakening those guards or introducing historical rerouting. Full Activity UI remains part 11; existing alert API browsing preserves history now.

## Verification and reviews

Independent full offline suite: **239 passed**, with the existing Starlette/AnyIO deprecation warning. Retained focused checks cover nine raw API cases, fifteen routing/delivery/migration cases and three root recovery/privacy cases. Original mutation, polling, manual capability, transport, ownership and backup regressions remain green after updating legacy fixture contracts and obsolete automatic-reactivation expectations.

Actual `python -m app.admin backup` and `verify` subprocesses passed **7→8** and **8→8** on disposable databases, with integrity, foreign keys and offline ASGI health. Saved ciphertext, channel selections and sent delivery state survive restore; correct-key decryption works, while missing/wrong keys retain ciphertext and expose unusable state. Missing test ownership structure is rejected before backup/initialization.

Adversarial storage/API/UI reviews and an independent acceptance review found and fixed: replay expiry races; expired test owner completion; ignored transport guard wait budget; unsafe test error codes; unknown import fields; deleted selections/version drift; inaccurate key-dependent usability; failed channel conflict refetch using cached state; cancellation reason loss after in-flight acceptance; and test outcomes disappearing from Settings after reload. Direct regressions retain the fixes. UI review also fixed delayed job actions, disabled-fieldset draft capture and missing channel options during refresh. No reproducible source P1/P2 remains after independent review. **30 native in-app-browser contracts passed**, first by the UI agent and then independently by root on fresh fixtures. Actual iframe journeys exercise Settings CRUD, lost create/test response replay with the original key and payload, Keep/Replace/Clear, conflict refetch failure, escaped preview, unknown test acceptance remaining visible after reload, explicit legacy import, two-channel job selection and independent sent status after a fixture availability event. No real provider sends occurred. All owned tabs, test servers and disposable fixture directories were closed/stopped/removed.

Final self-review checked every plan 06 requirement against current source and retained tests, native execution, actual backup commands, migration/key recovery documentation, agent fixes, local links and branch policy. Whitespace checks pass. Parts 07–14 remain plans; this completion does not publish remotely or deploy production.
