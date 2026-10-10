# Contributing

Thank you for helping. Tidesight is small on purpose: a standard-library Python collector, a dependency-free tracker and a static dashboard that run on cheap shared hosting.

- **Read first:** [`AGENTS.md`](AGENTS.md) (the hard rules, conventions and commands, for people and coding agents alike) and [`docs/design.md`](docs/design.md) (why things are the way they are).
- **Questions and ideas:** open an issue. For a security problem, see [`SECURITY.md`](SECURITY.md) instead.
- **Changes:** small, focused pull requests, with tests, and the matching documentation in `docs/` updated in the same change. Run what your change touches:

| What | Command |
|---|---|
| Python | `python -m pytest` |
| Tracker | `cd tracker && npm install && npx playwright install chromium && npm test` |
| Dashboard | `cd tracker && npm run test:dashboard` |
| Connectors | `TIDESIGHT_CONNECTORS=php,node python -m pytest server/python/tests/test_conformance.py` |

- **Try your change on realistic data:** `python tools/demo.py --out demo-data`, then `python server/python/devserver.py --config demo-data/tidesight.toml --no-auth --port 8790`.
- **Screenshots** in `docs/images` are made by `tools/screenshots.mjs` from that demo data; regenerate them when the dashboard's look changes.

By contributing, you agree that your work is published under the [MIT licence](LICENSE).
