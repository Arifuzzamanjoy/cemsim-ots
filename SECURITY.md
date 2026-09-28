# Security policy

CemSim is a **training simulator**. It must never be connected to a real plant control
system.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting (Security → Report a vulnerability)
rather than a public issue. You can expect a first answer within a week.

## Hardening already in place

- **Command validation:** every operator/trainer command is validated for tag, type,
  finiteness and range. Malformed input returns an error and never reaches the process
  model (see `tests/validation/api_probe.py`).
- **Saved states:** filesets are JSON (no pickle), and fileset names are restricted to
  `[A-Za-z0-9_-]`.
- **No external CDN:** the web UI loads no external scripts; three.js is vendored.
- **CI scanning:** CodeQL, `pip-audit` and Dependabot run in CI.

The server has **no authentication**. Run it on a trusted network, or put it behind an
authenticating reverse proxy.
