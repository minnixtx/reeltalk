# ReelTalk

A federated social network for tracking, reviewing, and discovering films. B-movies, cult classics, midnight shows, double features, and everything in between. The name is a pun on "real talk": honest conversation, anchored to the reel.

## Status

🚧 **Ground-up rewrite in progress.** ReelTalk was originally built as a fork of [BookWyrm](https://github.com/bookwyrm-social/bookwyrm) under the Anti-Capitalist Software License v1.4. Because ACRL is not an OSI-approved free/open-source license, the project is being rewritten from scratch under the **AGPLv3**, using the original codebase purely as a functional reference. See [REWRITE.md](REWRITE.md) for the rationale and the rules that govern the rewrite.

The frozen original lives at **[minnixtx/reeltalk-legacy](https://github.com/minnixtx/reeltalk-legacy)** (archived, reference-only — feature inventory, behavior, design decisions; no code is carried over).

## What ReelTalk will be

- 🌐 **Federated** — built on ActivityPub; instances can follow each other across the fediverse
- 🎬 **Film-first** — track what you've watched, rate it, write reviews, and build shelves of favorites
- 👥 **Community-driven** — small, trusted communities instead of one giant feed
- 🔓 **Free and open source** — AGPLv3, no corporate middleman

## Roadmap

- [x] Project seed (AGPLv3)
- [ ] Functional spec from the frozen original
- [ ] Core rewrite: film domain model + metadata (TMDB)
- [ ] Federation (ActivityPub)
- [ ] First public instance

Discuss the project on Matrix: `#reeltalk:minnix.dev`

## License

[GNU AGPLv3](LICENSE). ReelTalk is functionally inspired by [BookWyrm](https://github.com/bookwyrm-social/bookwyrm); no BookWyrm code is included in this repository.
