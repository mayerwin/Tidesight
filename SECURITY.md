# Security

Tidesight stores what visitors do on websites, so its security matters to more people than its users.

## Reporting a vulnerability

Please report it privately: on GitHub, **Security → Report a vulnerability** ([private vulnerability reporting](https://github.com/mayerwin/Tidesight/security/advisories/new)). Do not open a public issue for it.

Say what an attacker can do, against which component (tracker, collector, dashboard, a connector, the jobs), and how to reproduce it. You will get an answer within a week; a fix comes with a test that fails without it, and a release.

## Supported versions

The latest release. Installations see new releases in their dashboard (Status page) and update with a click; see [`docs/deploy-dreamhost.md`](docs/deploy-dreamhost.md).

## What Tidesight already guards against

The design and the past reviews are described in [`README.md`](README.md#security-and-abuse-protection) and [`docs/roadmap.md`](docs/roadmap.md): bounded public endpoints, no secrets stored from requests, redacted URLs, a strict Content Security Policy for the dashboard, sign-in throttling, HTTPS-only passwords, read-only demos, and an outside check that the data folder cannot be downloaded.
