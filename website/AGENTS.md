# KiroCrewWebsite — Agent Guidelines

**This file is a ROUTER, not a manual.** It carries only the rules whose violation
causes damage before a pointer could be read. Everything else is a link you MUST
open before touching that area. The backend router is [`../AGENTS.md`](../AGENTS.md).

React + TypeScript + Vite SPA for the Kiro Crew dashboard. Built assets go to
`dist/` and are staged into `../src/kiro_crew/static/dist/` so the gateway serves
them.

## Read before you touch

| If you are touching… | Read first |
|---|---|
| layout of a page, panels, headers | [page-layout](docs/page-layout.md) |
| narrow screens, gutters, page zoom, touch gestures | [narrow-viewport](docs/narrow-viewport.md) |
| themes, colors, CSS vars, stable class hooks | [theming-contract](docs/theming-contract.md) |
| shared components, a11y, URL sanitization, data fetching, the stack, adding a dependency | [frontend-conventions](docs/frontend-conventions.md) |
| any user-facing string, date, number, or sort order | [i18n-catalog](docs/i18n-catalog.md) + [i18n gates](../docs/ci/i18n-gates.md) |
| `src/extensions.ts`, edition composition, registries | [extension-seams](docs/extension-seams.md) |
| tests (vitest, MSW, Playwright, Electron), or a test that fails only in CI | [testing](docs/testing.md) |
| the Electron desktop shell | [electron/README.md](electron/README.md) |
| anything backend, or a whole-system question | [`../AGENTS.md`](../AGENTS.md) |

Everything under `website/docs/` is indexed by [its README](docs/README.md).

## Build and test: a gotcha that produces a silent false green

- **The `localStorage` test polyfill must stay on `Storage.prototype`.** Assigning
  it elsewhere makes the mock silently miss.

`npm run test` runs the website vitest suite AND the Electron node:test suite, and
a `pretest` jscpd duplication check runs first, so `npm test` can fail on
copy-paste before a single test executes. Commands and layers:
[testing](docs/testing.md).

## This is a public OSS fork: don't reintroduce internal couplings

The public build is plain npm + Vite, `src/rum.ts` is an inert no-op stub that stays
inert, and a downstream edition re-adds a removed surface **additively** through the
extension seams, never by editing core. The full list — build and infra, identity and
telemetry, the removed product surfaces, and the Channels / Board divergences an
upstream sync must not restore — is
[oss-fork-boundaries](../docs/system-specs/oss-fork-boundaries.md), gated by
the `internal-content-scan` check.

> **`AUTOSDE.yaml` in this directory is live and authoritative.** The frontend
> review rules it declares are read by the `claude-review`, `codex-review`,
> `code-review`, `fork-gpt-review`, and `fork-opus-review` workflows, and a
> `blocking: true` rule there outranks a reviewer's own prompt. Read it before
> changing frontend code; never treat it as historical.

## Rules that must not wait for a pointer

- **Settings primitives: pass `configKey` on every new `SettingsToggle`/`SettingsField`**
  that writes a config path, or the `<SettingRef configKey="...">` chip silently
  degrades to a CLI popover even though a toggle exists. Backend drift guards catch bad
  keys, not missing ones, so this rule is the only gate for the missing case.
- **Icons: `lucide-react` only, with `className="lucide-inline"`.** Never an emoji,
  never a hand-rolled SVG, never `size={N}`. Enforced by `AUTOSDE.yaml`
  (`use-lucide-icons`, `no-emoji-as-icons`).
- **Errors shown to the user render through `ErrorNotice`**, never a hand-written
  `<div className="text-danger">{err}</div>`, with `askAgent` on wherever the hand-off
  cannot lose anything. It navigates away and destroys unsaved local state, so next to
  an unsaved draft leave `askAgent` off and say why in a `{/* No hand-off: … */}`
  comment — a silent omission reads as "forgot". Enforced by `AUTOSDE.yaml`
  (`errors-use-error-notice`).
- **Security: every `dangerouslySetInnerHTML` goes through DOMPurify** via
  `md()` / `sanitize()` / `esc()` in `src/api/helpers.ts`. A bypass is an XSS bug,
  so there is no acceptable pointer for this one.
- **Never format a date, number, or sort order without naming a locale.** Route
  through the `src/i18n/format.ts` seam; naming a locale explicitly IS the opt-out.
  CI-gated, and the failure (a Chinese UI rendering `7/30/2026`) ships silently.
- **Never hardcode a user-facing English string.** The dashboard ships in 12
  languages; add a catalog key. CI-gated.
- **Data fetching is React Query**, never `useState` + `useEffect`. Follow the
  existing query-key convention.
- **Animation is Framer Motion.** Do not add new CSS `@keyframes`.
- **A persistent element that changes form or place stays one element.** When
  a user action or a state flip minimizes, collapses, relocates or replaces
  something the user already has on screen, animate that one element between the
  two states (`layoutId` / `layout`, a landing spot the eye can follow, text that
  stays continuous, restore as the reverse). Never a hard swap of two components
  (`flag ? <Chip/> : <Card/>`): the user reads it as "that vanished and something
  else appeared" and no label repairs it. `prefers-reduced-motion` drops the
  motion, not the continuity. Loading/empty/error → content transitions are not
  this rule. Evidence for such a change is a recording, not a screenshot. Gated by
  the UX Review lane (lens 13) — a hard swap with no stated reason is a BLOCK.
- **Styling uses design tokens** (`var(--bg)`, `var(--text)`, …), never a literal
  color.
- **Typography:** no `text-xs`, and no text below 10px.
- **Accessibility:** use `<Clickable>` rather than `<div onClick>`; give every
  icon-only button an `aria-label`; use `<Btn>` / `<SendBtn>` rather than a raw
  `<button>`; announce streaming regions with `aria-live`; honor the modal
  focus-trap contract.
- **Compose from `src/components/ui.tsx`.** Never hand-roll a panel section
  header; use `PanelSectionHeader`.
- **`src/extensions.ts` is core-owned and must register nothing.** Core
  registrations belong in the seed maps.
- **Edition composition is fail-closed:** it needs `KIROCREW_EDITION_DIR` **and**
  `KIROCREW_ALLOW_EDITION=1`. Never set the latter in a release or publish job. A
  contaminated public wheel cannot be unpublished.
