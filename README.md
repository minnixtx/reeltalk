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
- [x] Functional spec from the frozen original ([PLAN.md](PLAN.md), incl. dependency + asset license audit)
- [ ] Core rewrite: film domain model + metadata (TMDB)
- [ ] Federation (ActivityPub)
- [ ] First public instance

Discuss the project on Matrix: `#reeltalk:minnix.dev`

## License

[GNU AGPLv3](LICENSE). ReelTalk is functionally inspired by [BookWyrm](https://github.com/bookwyrm-social/bookwyrm); no BookWyrm code is included in this repository.

## AI Disclosure:

ReelTalk was written in conjunction with local AI using the llama.cpp application to load and serve the local model to a custom coding harness all in network. No code was written by or exposed to an external provider. Most of the scaffolding and planning was built in tandem with Qwen 27b, Qwen providing basic framework suggestions and a module map based on features I wrote specs for, and me providing the Python code and Django framework. Qwen would then build the testing suite for each phase and milestone and I would run the tests and troubleshoot the results using Qwen as a reference.

I am not an application developer by trade, but a DevOps engineer, so local AI has been instrumental in allowing me to contribute to the open source software community. Many people have their reasons for opposing AI, and I fully respect those beliefs. I have done my best to minimize the impact that my use of AI has contributed to the environment during this project. 

No external API provider has had any part in the ReelTalk project. All inference has been ran in-house.

The inference server used is based on AMD's Strix Halo platform and idles at ~ 5 watts, with the power limited to 90 watts at full load. My energy provider is 100% renewable energy. 
