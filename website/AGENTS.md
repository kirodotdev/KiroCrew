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
| themes, colors, CSS vars, stable class hooks | [theming-contract](docs/theming-contract.md) |
| shared components, a11y, URL sanitization, data fetching | [frontend-conventions](docs/frontend-conventions.md) |
| any user-facing string, date, number, or sort order | [i18n-catalog](docs/i18n-catalog.md) + [i18n gates](../docs/ci/i18n-gates.md) |
| `src/extensions.ts`, edition composition, registries | [extension-seams](docs/extension-seams.md) |
| tests (vitest, MSW, Playwright, Electron) | [testing](docs/testing.md) |
| the Electron desktop shell | [electron/README.md](electron/README.md) |
| anything backend, or a whole-system question | [`../AGENTS.md`](../AGENTS.md) |

Everything under `website/docs/` is indexed by [its README](docs/README.md).

## Stack

React 18, Redux Toolkit, React Query (`@tanstack/react-query`), React Router v7,
Framer Motion, Tailwind CSS 3, Lucide React, DOMPurify, highlight.js, Monaco,
TypeScript, Vite 5. Prefer the library already here over a new dependency.

## Build and test: two gotchas that produce a silent false green

- **`npm run typecheck` checks ZERO files.** It runs `tsc --noEmit`, and the root
  `tsconfig.json` has `"files": []` with project references, so nothing is
  type-checked and it always passes. Use **`npx tsc -b`** (what `npm run build` and
  CI run) whenever you mean to type-check.
- **The `localStorage` test polyfill must stay on `Storage.prototype`.** Assigning
  it elsewhere makes the mock silently miss.

`npm run test` runs the website vitest suite AND the Electron node:test suite, and
a `pretest` jscpd duplication check runs first, so `npm test` can fail on
copy-paste before a single test executes. Commands and layers:
[testing](docs/testing.md).

## This is a public OSS fork: don't reintroduce internal couplings

- **Build/infra:** no `npm-pretty-much`, Brazil, AIM, or CodeArtifact registries.
  The public build is plain npm + Vite; `.npmrc` pins the public registry.
- **Identity/telemetry:** no live Cognito pools or RUM app ids (`src/rum.ts` is an
  inert no-op stub, keep it inert), no `aws-rum-web`.
- **Removed product surfaces:** internal feature-app pages, tabs, API-client
  methods, and the credential-TTL card were deleted with their backend. A
  downstream edition re-adds them additively through the extension seams, never by
  editing core.
- The **Channels** app is hidden from the App Store and the **Board** app is
  removed. An upstream sync must not restore either.

> **`AUTOSDE.yaml` in this directory is live and authoritative.** The frontend
> review rules it declares are read by the `claude-review`, `codex-review`,
> `code-review`, `fork-gpt-review`, and `fork-opus-review` workflows, and a
> `blocking: true` rule there outranks a reviewer's own prompt. Read it before
> changing frontend code; never treat it as historical.

## Browser support

Chrome, Firefox, Safari, Edge. Use standard Web APIs only; guard browser-specific
ones (e.g. `typeof Notification !== 'undefined'`).

## Rules that must not wait for a pointer

- **Icons: `lucide-react` only, with `className="lucide-inline"`.** Never an emoji,
  never a hand-rolled SVG, never `size={N}`. Enforced by `AUTOSDE.yaml`
  (`use-lucide-icons`, `no-emoji-as-icons`).
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
