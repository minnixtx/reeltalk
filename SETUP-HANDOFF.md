# Setup handoff: make `setup.sh` fill out the whole `.env`

> Point the next session at this file. Everything needed is in the repo; no
> production access is required.
>
> Written 2026-10-24, after security increment S7 closed the audit (F1-F18 all
> fixed, full gate green at 2654 passed, pushed as `81913a5`). The audit is
> done; this is the next piece of work.
>
> `PROGRESS.md`'s `## NOW` block is the source of truth for what is live.
> Everything below is verified against the tree as of that commit.

## The task

`setup.sh` today asks for one thing and generates three secrets. `.env.example`
carries 39 variable lines. The gap between those two numbers is the new
deployer's onboarding wall, and it got wider during the security audit, which
added `WEB_BIND`, `FEDERATION_ALLOWED_NETWORKS`, `INBOX_MAX_BODY_BYTES`, and the
two-role database split.

Make `setup.sh` the thing that produces a complete, working `.env` by asking the
deployer a short set of plain questions, each with a one-line explanation of what
the variable is for and what happens if they skip it.

What "done" means:

1. A first-time deployer can go from clone to a running instance without opening
   `.env.example` or reading `DEPLOYING.md` first.
2. Every question is answerable by someone who has not read the source. Each
   prompt names the variable, says what it controls in plain words, and shows the
   default that a bare Enter will accept.
3. The resulting `.env` boots. Not "boots on this machine": boots on a box with a
   reverse proxy, and on a box with none.
4. `.env` stays a plain text file the deployer can edit by hand afterwards, and
   re-running `setup.sh` after an edit does not silently undo it.
5. A scripted install with no TTY still works and still fails loudly on a
   placeholder.

## Current state, verified

`setup.sh` (repo root, mode 0755, `bash -n` clean):

- Requires `.env` to already exist. With no `.env` it exits 1 telling the reader
  to `cp .env.example .env`. So today the script fills a template rather than
  producing a file.
- Prompts for `DOMAIN` only, and only when stdin is a TTY (`[ -t 0 ]`).
- Generates `SECRET_KEY` and `POSTGRES_PASSWORD` when blank.
- Generates `POSTGRES_APP_PASSWORD` only when `POSTGRES_APP_USER` is non-empty.
  That condition is load-bearing: inventing a password for a role that does not
  exist points the app at a login that cannot authenticate.
- Never overwrites a value that is already set. This is what makes it safe to
  re-run, and it is the property that keeps `.env` manually editable.
- Runs `.githooks/install.sh` (the co-author-trailer guard) as a side effect.
- Prints a closing banner with both compose paths.

`.env.example`: 39 variable lines, 38 unique. Only 8 are uncommented: `DOMAIN`,
`SECRET_KEY`, `POSTGRES_PASSWORD`, `POSTGRES_USER`, `POSTGRES_DB`,
`POSTGRES_HOST`, `POSTGRES_APP_USER`, `POSTGRES_APP_PASSWORD`. The other 31 are
commented, which is the file's way of saying "uncomment to change a default".

`COOKIES_FOLLOW_SCHEME` appears twice, at line 103 and again at line 151. A
rewrite that writes this key will write it in both places. Worth collapsing while
the file is open.

`STATIC_ROOT` and `MEDIA_ROOT` are read by `settings.py` (lines 283 and 285) and
are in no template at all. `ENV_FILE` and `REELTALK_VERSION` are compose
interpolation variables, not app settings.

No test executes `setup.sh` today. `test_secret_key.py:68` asserts the string
"setup.sh" appears in a failed boot's stderr, which is about the `SECRET_KEY`
refusal message in `settings.py:33`, not about the script.

## The constraint that shapes the whole design

Measured in the container against the installed `environs`:

```
env.bool("")  -> raises EnvValidationError: Not a valid boolean.
env.int("")   -> raises EnvValidationError: Not a valid integer.
env.list("")  -> []   (the empty list, NOT the documented default)
```

Read that against the brief's "fill out the entire .env file". If the rewrite
uncomments every line and writes a value for each, then any typed variable the
deployer leaves blank produces `KEY=` and the app **refuses to start**. The
variables that take a bool or an int are `DEBUG`, `WEB_PORT`, `SECURE_COOKIES`,
`COOKIES_FOLLOW_SCHEME`, `EMAIL_USE_TLS`, `EMAIL_PORT`, `POSTGRES_PORT`,
`DATA_UPLOAD_MAX_MEMORY_MiB`, `INBOX_MAX_BODY_BYTES`, `SECURE_HSTS_SECONDS`.
`SECURE_SSL_REDIRECT` is read with `env.bool` too.

`env.list` is worse because it does not raise. `TRUSTED_PROXIES=` present-but-empty
resolves to `[]`, which means "trust no proxy at all", silently overriding the
shipped default of the private ranges. A deployer with a proxy on the compose
network would get wrong `X-Forwarded-For` handling and no error.

**The rule that follows: write a line only when there is a real value for it.**
Unanswered optionals stay commented or absent, exactly as the template has them.
"Complete `.env`" means every variable the deployer was asked about and answered,
not every variable the app can read.

`env.str` with `default=""` is the one safe case, which is why the secrets can
ship blank in the template.

## Ground rules that already exist, do not re-derive them

- **Rebuild the image before running pytest.** The gate runs the baked image, and
  `test_clean_room.py` walks the baked tree, so a docs-only edit needs the
  rebuild too. Command is in `PROGRESS.md`'s NOW block.
- **Full gate only on the owner's explicit signal.** Targeted pytest plus ruff per
  increment otherwise.
- **One commit per increment, no co-author trailer.** A required status check
  enforces the trailer rule.
- **Rewrite the `PROGRESS.md` `## NOW` block as the increment's last step.**
- **Deployer-agnostic.** Safe no-config defaults in shipped code. Do not shape
  anything around this owner's proxy, and do not put host infra in the repo.
- **No em dashes in user-facing text.** That includes every prompt string
  `setup.sh` prints.
- **Never reset a credential without saying so in the same turn.** The bootstrap
  `POSTGRES_PASSWORD` is deliberately rotated away on first boot and not recorded
  anywhere; do not let a rewrite make a deployer think that value is theirs to
  keep.

## Traps

- **Compose `env_file` keeps a bare inline comment as the value.** `KEY= # note`
  resolves to the literal `# note`. Any note the script writes next to a value
  becomes the value. Notes go on their own line above, as `.env.example` already
  does.
- **Compose interpolation and `env_file` are different layers.** `${VAR}` inside a
  compose file resolves from the shell and the project-root `.env`, never from
  `env_file:`. `POSTGRES_USER`, `POSTGRES_DB`, and `POSTGRES_PASSWORD` are
  referenced with `${VAR:?required}` in both compose files, so they must be
  present in the file `setup.sh` writes. `WEB_BIND`, `WEB_PORT`,
  `POSTGRES_SHM_SIZE`, `REELTALK_VERSION`, and `ENV_FILE` are interpolation-only.
- **`set_var` uses `awk -v`, which processes escape sequences in the assigned
  value.** A deployer who types a backslash into an answer can have it eaten. The
  generated secrets are URL-safe base64 so they are fine; typed answers are the
  risk. Normalize or validate input rather than trusting it.
- **The no-TTY path must stay loud.** `PROGRESS.md` records a real incident: the
  `DOMAIN` guard only checked emptiness, so with no TTY the placeholder
  `your-instance.example.com` survived and a scripted install came up minted on a
  domain nobody owns, exit 0. It now re-checks against `PLACEHOLDER_DOMAIN`.
  Preserve that for every new question, not just `DOMAIN`.
- **A re-run once destroyed this box's `.env`.** `PROGRESS.md` records an
  accidental re-run over a freshly re-copied `.env`: blank values regenerated as
  new secrets and every setting the template does not carry was gone. Whatever the
  new script does, "never overwrite a value that is set" has to survive it.
- **No `shellcheck` in the image.** Validation is `bash -n` plus care.
- **`test_clean_room.py` scans every project file for the upstream attribution
  telltales** and skips `.env`, `.env.dev`, `.env.b`. `.env.example` is scanned,
  and so is any new file at the repo root, including this one. The guard
  whitelists only the attribution docs by name, so prose here that merely *names*
  a telltale trips the guard: reword rather than whitelist.

## Docs that state the current behavior and must move with it

Leaving these behind is the most likely way this increment does harm, because they
are the deployer's first read.

- `README.md` lines 50 to 67: the `cp .env.example .env` then `./setup.sh` flow,
  and the sentence "`setup.sh` prompts for `DOMAIN` and generates `SECRET_KEY`
  and `POSTGRES_PASSWORD` if they are empty."
- `DEPLOYING.md` lines 76 to 97 and line 259: the variable table and the quick
  start.
- `docker-compose.prebuilt.yml` line 7: the one-line install comment.
- `db/init-app-role.sh` line 27: tells the reader to run `setup.sh`.
- `reeltalk/settings.py` line 33: the `SECRET_KEY` refusal text names `setup.sh`,
  and `test_secret_key.py:68` asserts that string is present.
- `PLAN.md` lines 128, 332, 342 describe the old contract.

## Decisions (settled by the owner, 2026-10-24)

**`setup.sh` generates `.env`. There is no pre-copied `.env`.** The deployer runs
`./setup.sh` in a clone and the file appears.

This does **not** mean the script carries its own copy of the template's prose,
and it should not. The script copies `.env.example` to `.env` itself as its first
step, then fills the copy through the existing `set_var`. One source of prose,
`.env.example`, stays the file a deployer reads to learn what every variable
means. That removes the drift cost this section previously worried about: the
script owns the flow, the template owns the documentation, and neither is a
second copy of the other.

**To fix a wrong answer: edit `.env` and re-run.** No `--reconfigure` flag, no
new mode. The existing guarantee that a value already set is never overwritten is
what makes this safe, so that guarantee is now load-bearing for a documented
workflow rather than an internal courtesy, and a rewrite that loses it breaks a
documented recovery path.

Two consequences to carry into the implementation:

- **Blanking a value is how you ask the script to fill it again.** Set means
  leave alone, empty means fill. That is the whole repair vocabulary and it needs
  to be said once in the script's closing output.
- **Blanking a secret and re-running rotates it**, which invalidates every
  existing session and every outstanding verification and reset link. The script
  must say which variables it wrote, so a deliberate rotation is visible and an
  accidental one is not silent.

## Suggested question set

Roughly ten questions, grouped, each with a default on bare Enter. The rest of the
38 stay commented in the file.

1. `DOMAIN`, with the explanation that it is the public hostname with no scheme
   and that it drives allowed hosts and the default mail domain.
2. Is there a reverse proxy on this same host? Yes sets `WEB_BIND=127.0.0.1` and
   leaves `TRUSTED_PROXIES` at its private-range default; no leaves both alone.
3. Any extra hostname or a LAN IP people reach the site by? Feeds
   `CSRF_TRUSTED_ORIGINS` and `ALLOWED_HOSTS`.
4. `WEB_PORT`, default 3030.
5. Plain HTTP or HTTPS through the proxy? HTTPS offers `SECURE_SSL_REDIRECT` and
   `SECURE_HSTS_SECONDS`; plain HTTP must keep `SECURE_COOKIES` working over
   http, which is why `COOKIES_FOLLOW_SCHEME` defaults to true.
6. Outbound mail, as a block: host, port, user, password, sender name. This is
   the question that matters most for a first install, because with no mail
   nobody can verify an address, which means nobody can sign up or sign in at all.
   Say that in the prompt.
7. TMDB key, with the note that skipping it limits film search to the local
   library.
8. Any federation peer on the local network? Feeds
   `FEDERATION_ALLOWED_NETWORKS`, and the explanation is that outbound fetches
   refuse private addresses unless named here.
9. Time zone, default UTC.
10. Confirm, then write.

Secrets are generated, never asked. The five log levels, `DEBUG`,
`POSTGRES_SHM_SIZE`, `DATA_UPLOAD_MAX_MEMORY_MiB`, `INBOX_MAX_BODY_BYTES`,
`POSTGRES_PORT`, `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`,
`CANONICAL_ORIGIN`, and `REELTALK_VERSION` should not be questions at all.

## Verification

There is no test for this script today, and a rewrite of this size should not land
without one. A `test_setup_script.py` that runs `bash setup.sh` against a temp
directory can hold the whole contract without touching the live stack:

- No TTY plus the placeholder `DOMAIN` exits non-zero. This is the regression that
  actually bit.
- No TTY plus a real `DOMAIN` in the file exits 0 and fills both secrets.
- Re-running over a filled file changes nothing byte for byte.
- A value already set is never overwritten.
- The generated file parses: read it back with `environs` and assert every
  uncommented line yields the value that was typed, and that no typed variable
  is present-but-empty.
- `docker compose --env-file <generated> config -q` succeeds, which is the only
  check that catches the interpolation variables being missing.

Then the manual pass, on a throwaway project name so it cannot collide with the
live stack (both compose files hardcode `name: reeltalk`):

```
docker compose -p rtsetup -f docker-compose.yml config -q
bash -n setup.sh
```

and a real boot on a copied tree, checking that `GET /` answers 302 to `/setup/`.

Targeted pytest plus ruff on the touched files is enough for the increment. The
full gate is the owner's call.

## What is not in scope

Operator infrastructure: TLS termination, backups, firewalls, nginx config,
certbot, host-side database roles. Those are the operator's, by the lean-scope
decision, and the shipped code has to be correct without them.
