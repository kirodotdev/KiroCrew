# Architecture

This is a short map for the factory. The canonical map is
[`docs/architecture/overview.md`](docs/architecture/overview.md); subsystem
contracts live in [`docs/system-specs/`](docs/system-specs/README.md).

## Modules and seams

- **Surfaces → gateway → sessions:** CLI, dashboard, desktop, and messaging
  surfaces enter the asyncio gateway and resolve through the session layer.
- **Sessions → ACP:** `src/kiro_crew/acp/` carries the harness boundary.
  Providers adapt harnesses; shared orchestration must not depend on one harness.
- **Tools → authorization → OS:** the PreToolUse gate applies policy and profile;
  the sandbox enforces filesystem and process boundaries.
- **State:** config, history, memory, lessons, and sessions live under the data home.
  Keep project state and machine-wide agent configuration distinct.
- **Frontend:** `website/` builds the SPA; the Python gateway serves staged
  `src/kiro_crew/static/dist/`. Electron wraps the web surface.

## Tests and contracts

- Backend tests live in `test/`; dashboard tests live under `website/src/test/`.
- The owning module spec under `docs/system-specs/` defines behavior contracts.
- Read the overview's feature map to find the source owner and the related spec.
