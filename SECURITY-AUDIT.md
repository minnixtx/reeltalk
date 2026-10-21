# Security audit (2026-10-20)

Read-only audit of the federation, auth, moderation, and deploy surfaces.
Nothing here has been fixed yet; each finding lists a suggested fix. Line
numbers are as of the audit date.

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

`social/admin.py` (811 lines), `lists/models.py` + `services.py`,
`mentions/parser.py`, `notifications` views, `collections.py`,
`objects.py`, `broadcast.py`, `verify.py` internals, and the test suite's
own coverage of the above.
