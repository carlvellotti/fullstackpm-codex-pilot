# Optional pilot accounts

Free lessons and local checkpoints do not need an account. Connect only when the learner requests tracking or paid content. Use Codex's browser authentication for the plugin's account connection; never request, read or forward OAuth credentials yourself. A deployed `not_configured` response means the feature is unavailable.

## Protected module or work skill

Call `fspm_pilot_install` with the normal local root/kind/item and `account: true`. It saves the operation and returns exact `account_tool` and `account_arguments`. Call that tool on the authenticated account connection, then repeat the local install with its `install_ticket`. Keep the ticket transient in tool arguments; never print or save it. For a retry, use the returned refresh arguments and original operation. A service outage does not require new login. Denied membership must not be bypassed through public tools or another user's account.

## Progress tracking

1. Explain the scope when enabling tracking: uploads include lesson/release/workspace identifiers, attempt states and event times. Names, product questions, checkpoint prose, exercise files and local paths stay local. The optional account profile can store a name only if the learner separately asks for that.
2. Read `fspm_pilot_account_profile(action: get)` for the connected account's opaque `account_id`. After the learner requests tracking, call local `fspm_pilot_sync_progress(action: prepare, account_id: ..., confirmed: true)`. Existing guest history stays local unless the learner explicitly requests importing it; that separate request uses `import_guest: true` and `confirmed: true`. Older pilot records import their current state as newly observed snapshots, not reconstructed historical events.
3. When the local tool returns `sync_authorization_required`, call its exact `account_tool` with `account_arguments`. Pass the resulting `sync_ticket` to local `fspm_pilot_sync_progress(action: send)`. The helper sends the saved batch directly and checks the service response. Never invent, edit or save tickets, digests or event payloads.
4. Keep teaching and saving local checkpoints if the service is unavailable. The saved batch survives lost responses and retries. For an expired ticket, call prepare again to obtain authorization for the same batch. Do not call it synchronized until `send` returns `synchronized`; inspect counts for further batches. `partially_synchronized` identifies rejected events that remain pending.
5. Once tracking is enabled, send pending account events at natural lesson pauses/completion. A learner's request to import their existing history covers its bounded batches; no need to ask again for each batch of that same import. Never automatically import previously local or another account's events merely because the account is connected.
6. To change the locally linked account, use `disconnect` with confirmation before preparing for the new account. This preserves local and cloud records and stops local uploads; it does not sign Codex out of OAuth. Then use Codex's account controls to switch accounts, read the new profile identity, and enable tracking only at the learner's request. Uploading earlier account history to the new account requires explicit import consent.

Cloud progress is a summary, not a replacement for practice files. In a fresh task, use local saved attempts and checkpoints to resume; if only cloud records are available on a new device, fetch the permitted module and ask where to resume rather than inventing the missing exercise state.
