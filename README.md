# Tidesight

Self-hosted web analytics that keeps every raw hit. Traffic ebbs and flows like a tide; *sight* is the insight you get from watching it.

- **Collect** with a small JavaScript tracker (`ts.js`) and on-demand plugins: pageviews (including SPAs), custom events, engagement time, clicks, scroll, forms, Web Vitals, errors, heatmaps and optional session replay.
- **Ingest** with a standard-library-only Python collector that appends every hit, raw, to SQLite. It runs on cheap shared hosting (Passenger WSGI or CGI, no daemons).
- **Analyse** with re-runnable jobs that derive sessions, geography, devices, bots, channels and visitor identity, and a static dashboard on top of a small JSON API.

Tidesight is generic and multi-site, and data-rich by default. A privacy mode (IP hashing or truncation, retention windows, DNT/GPC) is a config change plus a clean-up job.

## Status

Under active development. See [`docs/roadmap.md`](docs/roadmap.md) for what is done and what is next.

## Documentation

- [Design principles and decisions](docs/design.md)
- [Architecture](docs/architecture.md)
- [Wire protocol](docs/protocol.md)
- [Database schema](docs/schema.md)
- [Data-point catalogue](docs/datapoints.md)
- [Configuration](docs/configuration.md)
- [Deploying on Dreamhost](docs/deploy-dreamhost.md)
- [Privacy mode](docs/privacy.md)
- [Writing a connector](docs/connectors.md)
- [Roadmap](docs/roadmap.md)

Contributors and coding agents: start with [`AGENTS.md`](AGENTS.md).

## Licence

MIT — see [`LICENSE`](LICENSE). Vendored third-party code keeps its own licence.
