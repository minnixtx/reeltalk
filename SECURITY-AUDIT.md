# Security audit (2026-10-20)

Read-only audit of the federation, auth, moderation, and deploy surfaces.
Every finding F1-F18 is now fixed: F1-F12 landed in increments S1-S6 and F13-F18
in S7, so this file is the record of what was found rather than a to-do list.
Each finding still lists its suggested fix, which is not always the fix that
landed - PROGRESS.md's `## NOW` block is authoritative on what was actually
done, and on the three places where measurement overrode the suggestion here.
Line numbers are as of the audit date and were not maintained as the code moved.

## High

### F1. Remote HTML is stored unsanitized and rendered with `|safe`

Local content is sanitized at write time (`render_markdown` → `sanitize_html`,
`core/utils.py`). The federation ingest path does not do the same:

- `activitypub/statuses.py:442` - a remote Note's `content` is stored raw.
- `activitypub/statuses.py:217` - a remote Film document's `summary` is
  stored raw as `Film.description`.
- `activitypub/statuses.py:718` (and the create path in `_mirror_list`) - a
  remote list extension's `summary` is stored raw as `FilmList.description`.

Every read path renders these with `|safe`: `_feed_row.html:137`,
`status/detail.html:68`, `status/_reply.html:36`, `film/detail.html:54,80`,
`genre.html:33`, `lists/detail.html:85`, `moderation/index.html:67`,
`social/profile.html:63`.

So any federated peer can post `<img src=x onerror=...>` (or a script-laden
fragment bleach would have stripped) and have it execute on this instance -
on the feed, the film page, the list page, and in the moderator's queue.
The moderation-queue render is the worst case: XSS against a staff session.

The codebase already knows remote HTML must be sanitized:
`mirrors.py:281` runs a remote Person `summary` through `sanitize_html`.
The Note/Film/list content paths just never got the same line.

Fix: run `sanitize_html()` on the three ingest sites (create and update
branches alike). Same allowlist, one shared gate, matching `mirrors.py:281`.

### F2. `Update`/`Delete` do not check that the sender owns the object

`handle_update` (`statuses.py:846`) and `handle_delete` (`statuses.py:885`)
look the mirror up by `remote_url` and apply the change without checking the
verified `sender` is the row's author (`existing.user`). Any remote user with
a valid keypair can therefore overwrite or tombstone another remote user's
mirrored content by replaying its object id. The module's attribution rule
("the signature is the authority") protects who *created* a row; it says
nothing about who may *mutate an existing one*.

Fix: in the update branch of `_mirror_status` and in `handle_delete`, ignore
the activity when `existing.user_id != sender.pk` (log the drop, same
posture as the other graceful ignores).

## Medium

### F3. A fetched Person document's `id` is never checked against the URL it was fetched from

`fetch_person_document` (`mirrors.py:71`) only requires `doc["id"]` to be
truthy; `mirror_user_from_person` keys the mirror on `doc["id"]`. A remote
server can answer a fetch of `https://evil.example/x` with a document whose
`id` is some other actor's URL, minting a mirror that impersonates that
identity's actor URL on this box (and, once present, blocks the real actor's
mirror via the unique `actor_url` - a denial of that identity).

Fix: in `fetch_person_document` (or the callers), require
`doc["id"] == requested_url` (after the fragment strip), else raise
`RemoteFetchError`.

### F4. SSRF: outbound federation fetches accept any URL, including internal addresses

`fetch_person_document` (`mirrors.py:82`), `webfinger_actor_url`
(`mirrors.py:138`), `fetch_image_bytes` (`mirrors.py:313`),
`_fetch_film_document` (`statuses.py`), and `deliver_activity`
(`delivery.py:127`) all check only that the scheme is http/https. There is no
private/link-local IP guard anywhere (grep confirms `ipaddress` is used only
by `proxy_trust.py`), and `requests` follows redirects by default.

Reachable by an **unauthenticated** attacker: POST to `/inbox/` with a keyid
of `http://169.254.169.254/...#main-key` and the server GETs it before any
signature check (`resolve_sender` fetches first). Reachable by any member:
`/find/` webfinger to an internal host. Impact: port-scanning of the
operator's LAN/cloud metadata endpoint via timing/error oracle; GET/POST of
instance-signed activity to internal ports.

Fix (deployer-agnostic, safe default): a shared `ssrf_guard` used by every
outbound call - resolve the host, refuse private/link-local/loopback ranges
by default, with an env opt-in for operators who legitimately federate with
a LAN peer (mirrors how `TRUSTED_PROXIES` works). Re-check after redirects,
or set `allow_redirects=False` for the Person/film fetches.

### F5. CSP is configured but never sent

`django-csp==4.0` is a dependency and `settings.py:301-304` defines
`CSP_*`, but neither `"csp"` in `INSTALLED_APPS` nor
`csp.middleware.CSPMiddleware` in `MIDDLEWARE` is present, so no
`Content-Security-Policy` header is emitted. Verified live on the test
instance (2026-10-20): responses carry `X-Frame-Options`, `nosniff`,
`Referrer-Policy`, and `Cross-Origin-Opener-Policy`, but no CSP. The
configured policy is dead config - and its absence compounds F1.

Fix: add `"csp"` to `INSTALLED_APPS` (4.0 inserts its middleware from
there), keep the current policy, and add a test asserting the header appears.

### F6. `SECRET_KEY` falls back to a known constant

`settings.py:20` defaults to `"insecure-dev-only-key-change-me"`. An
operator who deploys without `SECRET_KEY` in `.env` (the prebuilt path, or a
hand-written env that skips setup.sh) gets a silently public key: session
and CSRF forgery, i.e. admin takeover. `setup.sh` generates a real key, but
nothing enforces that it ran.

Fix: when `DEBUG` is false and `SECRET_KEY` is unset or equal to the
fallback, raise `ImproperlyConfigured` at import with a pointer to
setup.sh.

## Low / hardening

- **F7. Inbox is unthrottled and its dedup table is unbounded.** Every
  unknown-keyid POST triggers a synchronous outbound fetch (amplification /
  thread exhaustion), and `DeliveredActivity` grows one row per activity id
  forever. Consider a per-source floor (the `attempts.py` pattern) and a
  retention prune.
- **F8. The signature does not have to cover the body.** In
  `_verify_rfc9421` the covered components come from the sender's own
  `Signature-Input`; a sender may omit `content-digest`, leaving the body
  authenticated only by transport. Require `content-digest` for POSTs with a
  body (Mastodon requires `Digest` on the legacy path too).
- **F9. No replay window** on `created`/`date` - documented as accepted
  (activities are idempotent via dedup); fine, but F7 makes replays cheap.
- **F10. Invite codes ride a query string** (`?invite=` on the profile), so
  they can leak via Referer/history. Mint-and-redirect in one fragment would
  avoid it.
- **F11. CSV export formula injection**: user-authored titles/notes starting
  with `=` execute in spreadsheet apps on open. Prefix `'` in the export.
- **F12. Minor**: private keys stored unencrypted in the DB (inherent to the
  design, but worth stating: DB compromise = key compromise); nodeinfo/about
  disclose the version; `refresh_mirror_profile`'s throttle is LocMemCache,
  so it is per-process.

### F12 resolutions (S6)

- **(a) Private keys at rest.** Documented in DEPLOYING.md under "What the role
  split does not protect: keys at rest". The role split limits what the app can
  reach; it does not encrypt anything at rest, so a database or backup
  compromise is a signing-key compromise. The protection is the operator's
  (guard the data volume and backups), not the code's.
- **(b) Version disclosure.** Decision: keep publishing it, in nodeinfo and on
  `/about`. The reference implementation does the same: a live Mastodon peer
  measured 2026-10-22 reports `"software": {"name": "mastodon", "version":
  "4.7.3"}` in its nodeinfo, plainly, and ReelTalk's `/about` already shows the
  version to any visitor. Suppressing it would be security-by-obscurity that
  costs an attacker nothing (the source is public under AGPL) while removing the
  software/version hints peers use for compatibility. No code change.
- **(c) Per-process throttle.** Fixed. `refresh_mirror_profile`'s throttle moved
  from LocMemCache to a one-row-per-actor Postgres table
  (`MirrorProfileAttempt`), so the window is shared across workers and survives
  a deploy rather than resetting per-process.

## Second pass (S6): the surfaces the first pass missed

The first pass left `social/admin.py`, `lists/models.py` + `services.py`,
`mentions/parser.py`, the notifications views, `collections.py`, `objects.py`,
`broadcast.py`, and `verify.py` unaudited. They are covered here. Findings
continue the numbering (F13+). Line numbers are as of 2026-10-22.

### F13. High: the user admin lets any staff user with change rights self-promote to superuser

`social/admin.py:161` (`UserAdmin`) subclasses plain `admin.ModelAdmin`, not
`django.contrib.auth.admin.UserAdmin`, and re-implements none of the role guards
stock Django carries: there is no `has_change_permission(request, obj)` /
`has_delete_permission(request, obj)` per-object check, no `get_queryset`
scoping, and no strip of the role fields for a non-superuser requester (grep
confirms none of those methods exist on the class). Meanwhile the "Roles"
fieldset (`admin.py:226-236`) exposes `is_staff`, `is_superuser`, and
`is_moderator` as editable fields.

Concrete path: the owner grants a support/helper staff account change
permission on `social.User` - exactly the flow this admin is built to support
(reset a member's password, correct an address per R124). That account opens
its own row, ticks `is_superuser`, saves: full superuser. The same permission
plus the password fields lets it overwrite the real admin's credential through
`save_model`'s `set_password` path and sign in as them. This is a total bypass
of the moderation model, which goes to lengths (`can_act_on`,
`AdminImmunityError`) to keep staff off admin-side accounts through the
moderation views - the admin surface never consults any of it.

Fix: override `has_change_permission`/`has_delete_permission(request, obj)` to
refuse a non-superuser requester on any row where `obj.is_superuser or
obj.is_staff or obj.is_moderator`; and in `get_form`/`save_model`, drop and
ignore the role fields unless `request.user.is_superuser`, mirroring stock
`UserAdmin.get_form`.

### F14. Medium: a status `Delete` never reaches the remote users the post mentioned

`broadcast.py` - `broadcast_status_delete` delivers via `_deliver_to_followers`,
while `broadcast_status_create`/`_update` deliver via `_status_targets`
(followers *plus* the remotes the status mentions). A mention is an address, so
the Create went to a mentioned remote who does not follow the author; the Delete
does not. That remote keeps a full copy of a post the rest of the network has
been told is gone. The file's own lists-section comment states this gap verbatim
and says `broadcast_list_delete` was fixed to use `_status_targets` - the status
path was left behind. Fix: have `broadcast_status_delete` deliver to
`_status_targets(status)` exactly as `broadcast_list_delete` does.

### F15. Medium: the full password hash is rendered to view-only staff

`social/admin.py:622-645` (`password_hash`, a read-only display method placed
in the change fieldset). The docstring justifies it as "an admin who can reach
this page can already set a new password," but since Django 2.1 the `view_user`
permission alone grants read-only access to the change page, where read-only
methods render. A staff account with view-only rights can open every user's
page and harvest the full hash (the superuser's included) to crack offline. The
token columns already apply the right discipline (8 of a 256-bit code); the hash
should get the same treatment. Fix: drop `password_hash` from
`get_fieldsets` unless `request.user.has_perm("social.change_user")`, or
truncate it to a short prefix.

### F16. Medium: the mention parser backtracks super-linearly on crafted markdown

`mentions/parser.py:59-63` - `_FENCED_CODE_RE` (`^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1`,
DOTALL|MULTILINE) and `_CODE_SPAN_RE` (`` `+.+?`+ ``). For a body of many lines
each opening a fence that never closes, the lazy `.*?` rescans toward end-of-text
at every candidate line-start, and the `` `{3,} `` quantifier admits multiple
match lengths per backtick run. `Status.raw_content` is an unbounded `TextField`
(`core/models.py:429`), so `n` is unbounded and a handful of concurrent crafted
posts pin every worker on CPU. It is polynomial (roughly O(n^2)-O(n^3)), not
exponential, and the input is local typed markdown only (the remote path uses
`mentions_from_tags`, which never runs these regexes), so it is authenticated
self-DoS rather than remote. Fix: make the quantifiers atomic (`(?>...)`) or
replace the fence pass with a line-scan state machine, and/or cap markdown
length before parsing.

### F17. Medium: the verification-resend throttles are check-then-mint with no lock

`social/verify.py` - `request_resend` reads `ip_budget_spent` and
`address_in_cooldown` and then calls `send_verification_email` (which mints),
with no lock or transaction spanning the read and the write. N concurrent POSTs
for one victim address all pass the cooldown check before any `mint()` commits,
so all N mint and enqueue: the 5-minute per-address window and the per-IP budget
are both defeated by concurrency. Superseding a token does not cancel its
already-enqueued send. Fix: serialize check+mint per key (a
`pg_advisory_xact_lock(hashtext(email))`, or `select_for_update()` on the user
row, inside a transaction covering the cooldown check and the mint).

### F18. Minor (second pass)

- **(a) Soft-delete tombstone keeps publishing the rating.** `Status.delete()`
  (`core/models.py:523`) clears `content`/`raw_content` but not `rating`, and
  `delete_activity` embeds the full `note_document`, so a deleted review's
  star score keeps being republished network-wide inside the tombstone. Fix:
  clear `rating` in `delete()`, or embed a stripped object (id at most).
- **(b) Admin delete bypasses the moderation immunity.** No
  `has_delete_permission`/`delete_model` override on `UserAdmin`, so a staff
  account with `delete_user` can hard-delete the instance representative
  (recreated with a fresh keypair, orphaning previously-signed Flags) or the
  site admin, and a local-user delete via admin sends no `Delete(Person)`
  (the ban view does). Fix: refuse `is_instance_actor`/`is_superuser` deletes
  for non-superusers; ideally route local-user deletes to the federating ban
  flow.
- **(c) The admin send-verification POST guard is weaker than its display
  guard.** `send_verification_now` (`admin.py:373`) checks only `obj.email`,
  while the button's visibility uses `_may_send_verification` (`admin.py:558`,
  local + unverified + not the instance rep). A hand-crafted POST can mint a
  verification token against a remote mirror or an already-verified account,
  and each send supersedes the live token the member holds. The admin path also
  skips the resend throttles. Fix: call `_may_send_verification` inside the
  handler and refuse.
- **(d) `InviteAdmin.search_fields` includes `code`.** `admin.py:779` gives any
  staff user with view-on-invite an `icontains` oracle over the credential the
  changelist deliberately truncates; a live code reconstructs one character at a
  time by whether a search returns a row. Fix: drop `code` from `search_fields`.
- **(e) Resend throttle edge cases.** `ip_budget_spent` returns `False` (no
  limit) when `client_ip()` is empty rather than bucketing those sends together;
  and both budgets filter `send_error=""`, so a caller cycling addresses at a
  domain that rejects at RCPT time accrues only failed sends and stays
  unthrottled. Low, bounded, but worth tightening.
- **(f) NodeInfo field name is singular.** `activitypub/views.py:148` publishes
  `"openRegistration"`; NodeInfo 2.0 (and Mastodon) use `"openRegistrations"`
  (plural). A spec-conformant consumer reads the wrong/absent value. Fix: rename
  the key.
- **(g) The lists service mutators carry no owner/deleted check.**
  `lists/services.py` `add_films`/`remove_film`/`move`/`rename`/`set_description`/
  `soft_delete_list` mutate a passed `FilmList` unconditionally. Not reachable
  today (every write view resolves through `_owned_list`, which scopes
  `user=request.user, deleted=False`), but the module docstring's "nothing else
  writes these tables" is the only thing enforcing it. Defense-in-depth: assert
  ownership inside the mutators.
- **(h) The add-film route bypasses the per-user blocked-film exclusion.**
  `lists/views.py` `list_add_film` accepts any `film_id` with no blocked check,
  so a member can add a film their own block list hides from search, then
  `broadcast_list_update` federates it. Self-directed (the actor is the
  blocker), so Low. Fix: filter through `_local_film_ids`/`_blocked_tmdb_ids`
  as the search doors do.
- **(i) `mentions_from_tags` has no per-activity cap.** `parser.py:172-190`
  resolves every `Mention` tag with no limit (bounded only by the inbox body
  cap), so a peer can pack one Note's `tag` array to burst notifications. Fix:
  cap resolved mentions per activity (~20), as Mastodon does.
- **(j) Activity builders take `actor` independent of the object's author.**
  `objects.py` `create_activity`/`update_activity`/`delete_activity` mint the
  envelope `actor` from a passed `user` while the Note's `attributedTo` comes
  from `status.user`. All current call sites pass them consistently, so it is a
  latent same-origin forge surface, not a live bug. Fix: derive actor from
  `status.user`, or assert they match.
- **(k) `change_email` reads the prior address outside its transaction.**
  `verify.py:367` reads `old_email` before the `transaction.atomic()` block, so
  two concurrent admin address-writes can both fire a tamper notice for the
  losing value. Admin-only, narrow window. Fix: read `old_email` inside the
  atomic block under `select_for_update()`.

## What held up well

`proxy_trust.py` (the right-to-left XFF walk and the scheme gate), the
credential throttles, token hygiene (`secrets.token_urlsafe`, row locks,
uniform refusal copy, address binding), the moderation permission model
(`may_moderate` / `can_act_on` / `can_impose_severity` with the instance-
representative shield), the admin-login form wiring (the `is_staff` trap is
correctly avoided), `csrf_exempt` only on the signature-verified inboxes, no
raw SQL / pickle / eval anywhere, `sensitive_variables()` on the credential
forms, non-root Docker image, and a `.dockerignore` that excludes every real
env file.

## Suggested fix order

1. F1 + F2 (same file, highest severity, small diff each).
2. F3 (one check in `fetch_person_document`).
3. F5 (two settings lines + a header test).
4. F6 (startup guard + a test).
5. F4 (the largest: a shared guard module + call sites + tests).
6. F7-F12 as a cleanup pass.

## Not yet audited in depth

The surfaces the first pass listed as unaudited (`social/admin.py`,
`lists/models.py` + `services.py`, `mentions/parser.py`, the notifications
views, `collections.py`, `objects.py`, `broadcast.py`, `verify.py`) are covered
by the second pass above (F13-F18). What remains unexamined in depth is the test
suite's own coverage of the above, and the fixes F13-F18 themselves once they
land.
