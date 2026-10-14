# Local Development

- GitHub Actions CI/CD is disabled by project decision; do not add workflows.
- Run `sh tools/verify.sh` locally before each push to the primary branch.
- Install the checkout-local gate with `python3 tools/local_checks.py install`.
- Checks must pass for the exact clean commit being pushed. A missing dependency
  blocks the push; do not bypass the hook or install dependencies without permission.
- Prefer backend integration tests with temporary SQLite databases and mocked
  transports. Keep isolated unit tests only for substantial logic, and E2E short
  and limited to critical scenarios. Remove tests only when their cost exceeds
  regression value, together with unused fixtures and commands.
- Live API checks require separate authorization and are not part of the gate.
- Current audit evidence and limitations are in `TASKS.ru.md`.
