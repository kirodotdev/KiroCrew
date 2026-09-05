# Frontend documentation

Contributor documentation for the dashboard SPA. The always-loaded rules live in
[`../AGENTS.md`](../AGENTS.md); this directory holds the detail that rules point at.
Backend and whole-system docs are in [`../../docs/`](../../docs/README.md).

| Document | Covers |
|---|---|
| [page-layout.md](page-layout.md) | The page skeleton every dashboard page follows, and the layout patterns to copy. |
| [theming-contract.md](theming-contract.md) | The CSS variable contract, the stable class hooks a theme may target, and what is deliberately not customizable. |
| [frontend-conventions.md](frontend-conventions.md) | Shared components, accessibility, keyboard isolation for dialogs and the overlays above them, URL sanitization, data fetching, live-collection identity, animation, and styling. |
| [i18n-catalog.md](i18n-catalog.md) | Catalog structure, key naming, plurals, and the formatting seam. |
| [testing.md](testing.md) | The three test layers, which to use when, what a `setupFiles` entry costs per test file, how Playwright really runs, how to keep a test deterministic under a loaded shard, and the manual procedures. |
| [extension-seams.md](extension-seams.md) | The registry seams a downstream edition composes against, and the fail-closed edition opt-in. |

Related, outside this directory:

- [`../../docs/ci/i18n-gates.md`](../../docs/ci/i18n-gates.md) for the i18n gate
  chain and the ratchet rule (this directory covers authoring, that one covers CI).
- [`../electron/README.md`](../electron/README.md) for the desktop shell's runtime
  surface, and
  [`../../docs/build/desktop-app.md`](../../docs/build/desktop-app.md) for its build
  pipeline.
- [`../../docs/system-specs/common/testing-conventions.md`](../../docs/system-specs/common/testing-conventions.md)
  for test determinism and suite speed.
