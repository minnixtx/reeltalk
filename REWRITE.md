# Why the rewrite, and the rules that govern it

## Why

The original ReelTalk was a hard fork of [BookWyrm](https://github.com/bookwyrm-social/bookwyrm) and inherited its license: the **Anti-Capitalist Software License v1.4** (© 2020 Mouse Reeve). ACRL is not OSI-approved and carries a field-of-use restriction (it does not permit use by for-profit organizations), so it does not meet the project's goal of being truly free and open source software.

In addition, copyright in BookWyrm-derived code is held by Mouse Reeve and the BookWyrm contributors — the ReelTalk owner cannot relicense that code. And mixing ACRL-licensed code into an AGPLv3 codebase would taint the new project: ACRL's user restriction is incompatible with AGPLv3's universal grant.

Therefore, on 2026-09-04 the original repository was frozen (renamed to [minnixtx/reeltalk-legacy](https://github.com/minnixtx/reeltalk-legacy) and archived), and this repository is a **ground-up rewrite under AGPLv3**.

## Clean-room rules (binding on all work in this repo)

1. **The legacy repo is a functional reference only.** Use it for the feature inventory, behavior, data-model ideas, UX flows, and the decision log (its `PROGRESS.md`). Do not copy code from it into this repository — no verbatim or near-verbatim carryover from ACRL-licensed files.
2. **Implement against open specifications.** Federation is built on the [ActivityPub](https://www.w3.org/TR/activitypub/) and [Activity Streams](https://www.w3.org/TR/activitystreams-core/) specifications, not BookWyrm's source.
3. **Audit dependencies before use.** Every package must carry a license compatible with AGPLv3 (permissive — MIT/BSD/Apache — or GPL/AGPL is fine). Be especially wary of BookWyrm-specific packages (e.g. `bw-file-resubmit`): check its license, and replace it with a native implementation if it is not free.
4. **Audit assets.** Artwork, icons, fonts, and images must be original or properly free-licensed (e.g. CC0/CC-BY/SIL OFL). BookWyrm's wyrm art and other copyrighted assets do not carry over.
5. **Keep attribution.** The README credits BookWyrm as the functional inspiration. That is appropriate and requires no ACRL text, because no code is copied.

## Where things live

| What | Where |
|---|---|
| This rewrite (AGPLv3) | `minnixtx/reeltalk` (this repo); local `/home/minnix/reeltalk` |
| Frozen original (ACRL v1.4, reference-only) | `minnixtx/reeltalk-legacy` (archived); local `/home/minnix/reeltalk-legacy` |
| Feature inventory + owner decision log | `PROGRESS.md` in the legacy repo |
| Seed-era planning docs | `docs/planning/` in the legacy repo |
