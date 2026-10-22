# Security audit handoff

> Status: **S5 is landed and committed (`957eff7`), targeted-green and
> live-verified, but not full-gated.** F7 (inbox body bound + per-source floor +
> retention prune) and F8 (signatures must cover the body) are in the tree; ruff,
> `makemigrations --check`, and the neighbouring suites are green on a rebuilt
> image, and a real Mastodon favourite arrived through the new body-coverage
> gate on the live instance. **One thing is outstanding and it is the owner's:**
> the full gate (~36 min, on your signal).
>
> The next increment is **S6: F10-F12 plus the surfaces the first pass did not
> audit**. No open design questions block it. The `## NOW` block in
> `PROGRESS.md` is the current source of truth; everything below is the handoff
> as written at S5's close.

Point the next session at this file. Everything needed is in the repo; no
production access is required to start.

## State (as of the S5 close, 2026-10-22)

- Closed so far: S1 (F4, F5), S2 (F1), S3 (F2, F3 + the Tombstone gap), S4 part 1
  (F6), S4 part 2 (the DB role split), **S5 (F7, F8)**. Open: **S6** (F10-F12 +
  the unaudited surfaces).
- **The full gate has not been run since S4 part 2.** S5 landed on targeted runs:
  104 green in the three inbox/signature suites, then **684 passed, 5 skipped**
  across twelve neighbouring suites (federation inbound/integrity/interactions/
  sanitize, lists inbound, flag+domain-block, AP follow, suspension read sites,
  proxy trust, credential throttle, clean room). **Predicted next full gate:
  2600 passed, 5 skipped** (2574 + the 20 items `test_inbox_hardening.py`
  collects + 6 clean-room items for the six new files). Count collected items,
  never `def test` lines.
- **The live federation check F8 needed is DONE (owner drove it 2026-10-22).**
  Both app roles were recreated onto the S5 image with `0002` applied, then a
  favourite from `minnix@upallnight.minnix.dev` on a ReelTalk review arrived: a
  new `activitypub_deliveredactivity` row (`.../users/minnix#likes/1386`) and a
  real `core_like` on the post, with `activitypub_inboxattempt` **still at zero
  rows**. So the reference peer's signed `digest` satisfies the new requirement,
  and honest traffic spent no throttle budget. **What it did not prove:** that an
  uncovered body is refused (mock-only), and the 429 floor and 413 bound, which
  no honest test produces.
- **VPS redeploy stays deferred until the audit is finished (owner).**
  reeltalk.dev still runs pre-S1 code. When it redeploys its `.env` needs
  `WEB_BIND=127.0.0.1` and `FEDERATION_ALLOWED_NETWORKS=192.168.1.0/24`.

## What landed in S5, and where

- `settings.INBOX_MAX_BODY_BYTES` (default 1 MiB, documented in `.env.example`,
  pinned in pyproject's pytest-env block). Checked in
  `views._inbox_too_large`: declared `Content-Length` first, then the real
  bytes. Refusal is **413**.
- `reeltalk/activitypub/inbox_throttle.py` + the `InboxAttempt` model
  (migration `0002_inboxattempt_and_more.py`). 60 counted failures per address
  per 15 minutes; refusal is **429 with `Retry-After`**.
- `reeltalk/activitypub/management/commands/prune_inbox.py` (`--days`,
  `--dry-run`), cron documented in `DEPLOYING.md` §10 (the old §10 became §11).
  `DeliveredActivity.delivered_date` gained an index so the prune is not a
  sequential scan.
- `signatures._body_must_be_covered` plus two gates: `_verify_rfc9421` refuses a
  POST-with-body whose `Signature-Input` omits `content-digest`;
  `_verify_legacy` refuses one whose `headers` list omits `digest`.
- `reeltalk/tests/test_inbox_hardening.py`, 20 items. Every refusal in the file
  is paired with its honest inverse, so a bound that refused everything would
  not pass.

### The one deliberate deviation from the plan

The plan asked for "a per-source floor on inbox POSTs". **This floor counts only
POSTs that cost us work and then failed authentication**, not all POSTs. The
reason is that the shared inbox legitimately receives a great many POSTs from
honest peers, so a floor on all of them would let a busy federation spend an
address's budget for doing nothing wrong. Counting only the failures keeps
honest deliveries free (pinned by a test asserting zero rows after successful
deliveries) while still bounding the amplification that made F7 worth fixing:
`resolve_sender` dials out for any unseen keyid, so a POST with no chance of
verifying used to buy an outbound fetch paid by our worker pool and by the peer
we were tricked into dialling. Both new refusals answer **before** that dial-out.

If a future session "corrects" this toward the plan's literal wording, it will
re-arm a denial in which a noisy but honest peer is throttled out of its own
deliveries. The count is the design, not an oversight.

### Measured facts for S5 (verified 2026-10-22, against the running peer)

Read these before touching the signature gates; they came from the **running**
Mastodon 4.7.2 on the LAN peer, not from a GitHub tag.

- **Mastodon 4.7.2 sends no RFC 9421 at all.** It speaks the cavage draft, so
  the `content-digest` gate cannot break it. That gate is for RFC 9421 peers.
- **It sends `Digest` on every POST that has a body**, and its own verifier
  refuses a POST whose digest is unsigned ("Mastodon requires the Digest header
  to be signed when doing a POST request"). So the legacy gate asks of a peer
  nothing the peer does not already demand of us.
- **Mastodon treats 4xx as final except 401, 408 and 429.** That is why the
  floor refuses with 429: any other code tells a healthy peer to discard the
  delivery. 413 correctly stays permanent, because an oversized body is not
  going to shrink on retry.
- **`crypto.load_private_key(pem: str)` takes a `str` and no `password`** - it is
  the project's Ed25519-only helper in `reeltalk/activitypub/crypto.py`, not
  `cryptography`'s serialization function. Passing `.encode()` and
  `password=None` raises `TypeError`.

## What the next session should do

**S6: cleanup pass + finish the audit** (see `security-plan.md` §S6). Two halves.

### Half 1: F10, F11, F12

Premises below were re-verified against the tree on 2026-10-22, with the line
that holds each one.

1. **F10, invite code in the query string.** `reeltalk/social/views.py:358`
   ends the mint with `return redirect(f"{profile}?invite={invite.code}")`, so
   the code lands in browser history, in any `Referer` the profile page leaks,
   and in any proxy log that records query strings. There is already a comment
   at `:346` reasoning about a bookmarked or forwarded `?invite=` on someone
   else's profile, so the surface is understood; the fix is the shape the
   finding names, a fragment (`#invite=`) instead of a query, since fragments
   are not sent to servers. Check what reads the value on the other side before
   changing how it is carried - the signup form prefills from it.
2. **F11, CSV export formula injection.** `export_film_csv`
   (`reeltalk/core/import_export.py:242`) writes `film.title` straight into a
   cell via `csv.writer`, and titles are not always TMDB's: the manual
   create-film path lets a member author one. A title beginning `=` (or `+`,
   `-`, `@`) executes when the export is opened in a spreadsheet. Prefix the
   cells that need it on the way out, and note the export is documented as
   byte-identical across runs and as re-importing as a no-op - a guard that
   changes bytes unconditionally breaks both, so it must trigger on the payload,
   not on every cell.
3. **F12, the three notes.** (a) Private keys sit unencrypted in the DB, which is
   inherent to the design; the ask is to **say so** in `DEPLOYING.md` next to the
   role split, since DB compromise is key compromise. (b) `nodeinfo_2_0`
   (`reeltalk/activitypub/views.py:145`) publishes `software.version` from
   `reeltalk.__version__`; the ask is a decision, not necessarily a change.
   (c) `refresh_mirror_profile`'s throttle uses `cache.get` (`mirrors.py:280`)
   against `LocMemCache` (`settings.py:224`), so it is per-process and resets on
   deploy - the same two failures `attempts.py` exists to avoid. If S6 moves it,
   `InboxAttempt` and `CredentialAttempt` are both existing table shapes to copy.

### Half 2: audit the surfaces the first pass did not cover

`social/admin.py` (811 lines), `lists/models.py` + `lists/services.py`,
`mentions/parser.py`, the notifications views, `collections.py`, `objects.py`,
`broadcast.py`, `verify.py`. Append anything found to `SECURITY-AUDIT.md` as
F13+ and extend `security-plan.md` if material. `social/admin.py` is the one with
history: the user admin was once a bare `ModelAdmin` whose add form wrote a
plaintext password (R77), so admin forms are a known-fragile pattern here.

## Deferred by the owner (from S4, still open): eliminate the bootstrap password

Not yet done. Recorded in full in `PROGRESS.md`'s NOW block and in the S4
handoff history: the password requirement comes from the postgres image's
`docker_verify_minimum_env`, not from Postgres, and `ALTER ROLE ... PASSWORD
NULL` destroys the verifier while socket maintenance keeps working. **The hazard
that must be closed in the same change:** the no-split posture (`POSTGRES_APP_USER`
empty) makes the app connect as the bootstrap login with `POSTGRES_PASSWORD`, so
turning that value into a published literal would hand out a known superuser
password. Also load-bearing: the db container publishes **no port**, which is what
makes "no password" safe at all.

## Ground rules (full list in security-plan.md)

- Rebuild the Docker image before running pytest; the gate runs the baked
  image. A docs-only edit needs it too (the clean-room guard scans the baked
  tree, and ruff formats Python inside markdown fences).
- Targeted pytest + ruff per increment. The full gate (~36 min) only on the
  owner's explicit signal.
- One commit per increment, no co-author trailer. Rewrite the `PROGRESS.md`
  NOW block as the increment's last step.
- Deployer-agnostic: safe no-config defaults in shipped code; host infra stays
  out of the repo. The prune's cron lives on the host for exactly this reason,
  in keeping with backups.
- The VPS is production: read-only unless the owner approves a change, and all
  VPS work is deferred until the audit is complete.
- No em dashes in user-facing text.
- The suite must not inherit operator env: any setting `.env` may legitimately
  set that tests assume a default of belongs pinned in pyproject's pytest-env
  block. **S5 added one:** `INBOX_MAX_BODY_BYTES`. (`POSTGRES_APP_USER` and
  `POSTGRES_APP_PASSWORD` are the S4 examples; `test_db_role_split.py` scrubs
  every `POSTGRES_*` name in its subprocess boots.)

## Traps learned landing S5, so you do not re-derive them

- **The F8 refusal test is a vacuous-test trap.** A signature that omits body
  coverage still has to be *otherwise valid* or the test proves nothing: sign
  with the mirrored actor's own key, not a fresh one, or the request is refused
  for the wrong reason and the test stays green with the gate deleted. This is
  the same failure the R79 mutation pass caught, wearing a different hat.
- **The floor's counter cannot be written inside the delivery transaction.** The
  dedup row and the handler run in one transaction (`activitypub/inbox.py`), so
  a counter written there would roll back its own rows on the failure it is
  trying to record. `record_failure` is called from the view, outside it.
- **A refusal that costs nothing must not be counted.** A POST with no signature
  at all returns 401 and writes no row: it never reached `resolve_sender`, so it
  never cost anything. Counting free refusals is how a floor becomes a
  self-inflicted denial of service.
- **Never key on an empty address.** `client_ip()` can come back empty; an empty
  key is one shared bucket for every caller that gets one. `inbox_blocked` and
  `record_failure` both no-op on empty, and `retry_after_seconds` returns 1.
- **`ruff` on the bind-mounted host tree needs `--no-cache`**, or it dies with
  `Failed to initialize cache at /host/.ruff_cache: Permission denied`. The
  working invocation is
  `docker compose run --rm --no-deps -v "$PWD":/host:z web sh -c "cd /host && ruff check --no-cache <paths>"`.
  Same for `makemigrations`, which must run against the host tree because the
  image bakes the source.
- **`prune_inbox` prunes two tables, not one.** If a future change adds another
  inbox-written table, the prune has to know about it or it becomes the second
  unbounded one - which is the finding this increment just closed.

## Read in this order

1. `security-plan.md` - ground rules first, then §S6.
2. `SECURITY-AUDIT.md` - F10, F11, F12, and the "Not yet audited in depth" list
   that Half 2 works through.
3. `reeltalk/tests/test_inbox_hardening.py` for what S5 actually enforces, then
   `views._handle_inbox_post`, `inbox_throttle.py`, `signatures.py`,
   `prune_inbox.py`.
4. `reeltalk/social/attempts.py` before touching any throttle, including F12(c).
5. `db/role-split.sql` and `docker-compose.test.yml` if the gate command or the
   DB identity comes up.
