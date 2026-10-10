## What and why

<!-- What this changes, and the problem it solves. -->

## Checks

- [ ] Tests for the change, and the suites it touches pass (`python -m pytest`, `cd tracker && npm test`, `npm run test:dashboard`)
- [ ] The matching documentation in `docs/` is updated (and `docs/roadmap.md` when an item lands)
- [ ] The hard rules of `AGENTS.md` hold (raw first, standard library in the collector, nothing from other hosts…)
- [ ] For dashboard changes: `docs/images` regenerated if the look changed (`tools/screenshots.mjs`), and the live demo still works (`tools/build_site.py`)
