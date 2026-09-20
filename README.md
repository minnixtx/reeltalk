# ReelTalk

A federated social network for tracking, reviewing, and discovering films. B-movies, cult classics, midnight shows, double features, and everything in between. The name is a pun on "real talk": honest conversation, anchored to the reel.

## Status

🚧 **Ground-up rewrite in progress — core film domain, TMDB integration, file import/export, federation and the social surface are done and running; the site now carries its own visual identity, and a real-domain public deployment is the one milestone item left.** ReelTalk was originally built as a fork of [BookWyrm](https://github.com/bookwyrm-social/bookwyrm) under the Anti-Capitalist Software License v1.4. Because ACRL is not an OSI-approved free/open-source license, the project is being rewritten from scratch under the **AGPLv3**, using the original codebase purely as a functional reference. See [REWRITE.md](REWRITE.md) for the rationale and the rules that govern the rewrite.

The frozen original lives at **[minnixtx/reeltalk-legacy](https://github.com/minnixtx/reeltalk-legacy)** (archived, reference-only — feature inventory, behavior, design decisions; no code is carried over).

Milestone-by-milestone state, with verification records and the decision log: [PROGRESS.md](PROGRESS.md).

## What ReelTalk is

- 🌐 **Federated** — built on ActivityPub; instances can follow each other across the fediverse
- 🎬 **Film-first** — track what you've watched, rate it, write reviews, and build shelves of favorites
- 👥 **Community-driven** — small, trusted communities instead of one giant feed
- 🔓 **Free and open source** — AGPLv3, no corporate middleman

## The look

ReelTalk has an original **theatrical grindhouse / midnight-movie identity** — late-seventies, early-eighties: warm neon on near-black brown, worn surfaces, marquee lettering. It is deliberately not a clean product-grey theme and not a re-skin of any existing film site; the design is being co-created with the owner, milestone by milestone.

- **Palette** — layered near-black browns under dirty cream text, with red used as *stage lighting* (glow, active states) rather than as a brand colour, and thin amber borders.
- **Surfaces** — a weathered background plate under a dark scrim, worn-chrome rails painted along the header and footer edges, content panels sitting nearly opaque over the grain.
- **Light** — the active nav page is marked by a lit neon tube (white-hot core inside red, with real bloom), and the wordmark is relit the same way: a hot filament inside red glass, its stroke scaled in `em` so it survives the mobile step-down.
- **Controls** — a primary button is a *lit* object: an original mottled crimson tile over a dark red gradient, boxed by a hot rim with real bloom and labelled in Bebas Neue caps. Quieter actions (block, follow, one-click add to watchlist) stay plain outlined controls, so the lit ones still mean something.
- **Type** — self-hosted [SIL OFL](reeltalk/social/static/fonts/) faces, no CDN: **Monoton** for the wordmark, **Bebas Neue** for titles, section headings and lit button labels, **Oswald** for metadata, tabs and genre pills, **Archivo** for body copy and for the header's own nav text, so the marquee speaks the same face as the film titles it leads to.
- **Film page** — the poster and title form one centred plate on a phone, and each field (runtime, genres, directors, cast) stacks its small caps label over a full-width value, so a long cast list reads as a paragraph instead of a narrow gutter; on a wider screen the labels take their own column beside the poster.
- **Home** — a two-pane "Now Playing" layout: the feed beside a rail of ticket-stub graphics heading the Trending Films and Popular Genres cards, each genre pill opening its own review subfeed.
- **Footer** — three zones under the lower chrome rail: the wordmark with the tagline *A Federated Home For Film Fans*, wayfinding links in the middle, and a closing "**Keep Watching.**" in Kaushan Script neon beside a film reel drawn as inline SVG.

All of it is one hand-written stylesheet with no CSS framework, and every asset is original or freely licensed.

## Roadmap

- [x] Project seed (AGPLv3)
- [x] Functional spec from the frozen original ([PLAN.md](PLAN.md), incl. dependency + asset license audit)
- [x] Core film domain — shelves, reviews/ratings, feeds, film pages (M1)
- [x] TMDB integration — search, metadata, posters, import backfill (M2)
- [x] File import/export in the canonical TMDB format (M3)
- [x] Federation (ActivityPub) — follows, reviews + films exchanged between instances, Mastodon-compatible signatures (M4; verified live between two instances)
- [x] Social surface — profile pages, follow/unfollow, remote-user discovery, blocking + feed filters, review delete (M5; verified live across two instances). The wider list from [PLAN.md](PLAN.md) — lists, groups, notifications, directory, RSS, moderation, 2FA — is future work.
- [ ] Polish & first public instance (M6) — **in progress**: daily `pg_dump` backup job ✅, the getting-started page (`/welcome/`, which settles the old "guided tour" item) ✅, artwork and the visual identity above ✅, with an owner-led polish pass refining it item by item. The real-domain target is settled and pre-flight-checked; deploy-readiness work has started — the forwarded TLS scheme is now believed only from the operator's own proxy, and session cookies carry `Secure` according to the transport they actually arrived on. Remaining: collapse the app onto that single trusted scheme, close the plain-HTTP exposure, make the sign-up calls follow the invite-only policy, rehearse a backup restore, and cut over.

Discuss the project on Matrix: `#reeltalk:minnix.dev`

## License

[GNU AGPLv3](LICENSE). ReelTalk is functionally inspired by [BookWyrm](https://github.com/bookwyrm-social/bookwyrm); no BookWyrm code is included in this repository.

## AI Disclosure:

ReelTalk was written in conjunction with local AI using the llama.cpp application to load and serve the local model to a custom coding harness all in network. No code was written by or exposed to an external provider. Most of the scaffolding and planning was built in tandem with Qwen 27b, Qwen providing basic framework suggestions and a module map based on features I wrote specs for, and me providing the Python code and Django framework. Qwen would then build the testing suite for each phase and milestone and I would run the tests and troubleshoot the results using Qwen as a reference.

I am not an application developer by trade, but a DevOps engineer, so local AI has been instrumental in allowing me to contribute to the open source software community. Many people have their reasons for opposing AI, and I fully respect those beliefs. I have done my best to minimize the impact that my use of AI has contributed to the environment during this project. 

No external API provider has had any part in the ReelTalk project. All inference has been ran in-house.

The inference server used is based on AMD's Strix Halo platform and idles at ~ 5 watts, with the power limited to 90 watts at full load. My energy provider is 100% renewable energy.

### Commit hygiene: no co-author trailers

Commits in this repository carry **no `Co-authored-by` trailer of any kind** — they show only the repo identity. This is deliberate, and it is enforced rather than merely requested:

- **`.githooks/commit-msg`** rejects any commit that tries to add a co-author trailer.
- **`.githooks/pre-push`** refuses to push a range that contains one (catches amends, cherry-picks and merges).
- **`.github/workflows/attribution-guard.yml`** fails the build if one arrives anyway — the layer that `--no-verify` cannot skip.

The reason it matters: GitHub parses `Co-authored-by:` trailers into the **contributors graph** and credits whichever GitHub account the email address maps to. A trailer naming an address nobody in this project controls therefore puts a **stranger** in the contributor list — and blocking that account does not remove them, because the graph is recomputed on every push and simply re-reads the trailer.

Hooks are per-clone local config. Run `bash .githooks/install.sh` after cloning (`./setup.sh` does it for you). To make the CI layer blocking rather than advisory, set it as a required check: **Settings → Branches → branch protection for `main` → Require a status check to pass → `attribution-guard`**. 
