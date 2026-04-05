# Agent instructions for netbird-tr064

## Repository visibility

This repository is **public**. All content — code, configuration, documentation,
commit messages, and comments — must be safe to publish openly.

## Credentials and personal data

- **Never commit credentials, API tokens, private keys, or passwords** — not even
  as placeholders that look real.
- **Never commit real IP addresses, hostnames, usernames, or location-specific
  data** belonging to any specific deployment.
- All example configuration files and documentation snippets must use
  clearly fake placeholder values such as:
  - Peer IDs: `"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"`
  - IP addresses: `"192.168.178.x"`, `"10.0.0.x"`
  - Passwords: `"changeme"`
  - API tokens: `"nbp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"`
- If a task requires real credentials or IPs (e.g. live testing), obtain them
  from the operator at runtime — never store them in the repository.

## Code quality

- Keep changes focused on the task; do not refactor unrelated code.
- Maintain compatibility with Python 3.12 and the existing dependency set
  (`requests`, `pyyaml`) — do not add new dependencies without discussion.
- All public functions and classes must have docstrings.
