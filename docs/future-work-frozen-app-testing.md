# Future Work: Real Frozen-App (Desktop / PyInstaller) Test Coverage

Status: **deferred** (captured 2026-07-19). Not blocking the CI test-time work.

## Problem

The frozen-app (PyInstaller / Electron desktop) install behavior is currently
**only ever mocked**, never validated against a real bundled binary.

`test/test_installer_shim_and_cleanup.py` pins down two real defects:

- **Defect A** — `agent._resolve_kirocrew_bin()` must return `sys.executable`
  (the bundled `kirocrew-backend`) in a frozen app, so `build_agent_config` /
  `rebuild_agent_config` keep `kirocrew-core` / `kirocrew-cron` instead of
  dropping them (which would take `spawn_run`, `cron_add`, `learn_add` offline).
- **Defect B** — `mcp_cleanup.clean_stale_managed_mcp()` must purge the
  `meshclaw-*` predecessor entries.

These tests monkeypatch `sys.frozen`, `sys.executable`, `shutil.which`, and
`agent._bin_is_usable`. They are good unit tests, but because the frozen
environment is simulated, a **real** packaging change can break the shipped app
while every test stays green, e.g.:

- PyInstaller layout change (`.../kirocrew-backend/_internal/kiro_crew`)
- the `kirocrew-backend` executable rename
- a missing PyInstaller hidden import (binary won't even boot)

## Gap in CI today

- `.github/workflows/build.yml` → `build-desktop` job **builds** the real
  binary (`make desktop` → `packaging/kirocrew-backend.spec`) but only uploads
  the artifact. **No smoke test** — nothing ever runs the frozen binary.
- Contrast `build-wheel`, which at least runs `kirocrew --version`.
- Meanwhile `pyinstaller` (the `desktop` extra) is installed into the
  `backend-test` leg but never imported there and no binary is built there —
  dead weight in that leg.

## Proposed fix (two complementary parts)

1. **Remove `desktop` from the `backend-test` install** (`".[voice,desktop]"`
   → `".[voice]"`). pyinstaller is not imported by any test; frozen validation
   belongs where the binary is actually built. Small CI speedup.

2. **Add a real frozen-binary smoke test to `build-desktop`** (the binary is
   already built there, so marginal cost is just *running* it once). Escalating
   strength:
   - **Minimum:** run the built `kirocrew-backend` once (`--version`) — proves
     it boots; catches missing hidden imports / broken spec. Frozen analogue of
     the wheel's `kirocrew --version`.
   - **Recommended:** invoke it in a mode that triggers `rebuild_agent_config`
     against a temp `KIROCREW_HOME`, then assert `kirocrew-core` and
     `kirocrew-cron` are present with an absolute, executable command — the
     end-to-end version of `test_managed_servers_survive_in_frozen_app`, run
     against a genuinely frozen process instead of a mocked one.
   - Needs a CLI-surface check first: confirm a subcommand exists that rebuilds
     / prints the agent config (see `src/kiro_crew/cli.py`) so the smoke test
     can be strong, not just `--version`.

## Pointers

- `test/test_installer_shim_and_cleanup.py` — the mocked unit tests
- `src/kiro_crew/agent.py` — `_resolve_kirocrew_bin`, `ensure_kirocrew_on_path`,
  `build_agent_config`, `rebuild_agent_config`
- `src/kiro_crew/mcp_cleanup.py` — `clean_stale_managed_mcp`
- `packaging/kirocrew-backend.spec`, `packaging/build-desktop.sh`, `Makefile`
  (`backend-bin`, `desktop` targets)
- `.github/workflows/build.yml` (`build-desktop`), `.github/workflows/nightly.yml`
