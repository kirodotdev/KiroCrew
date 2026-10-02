# Kiro Crew Website

React + TypeScript + Vite single-page app for the Kiro Crew dashboard. Built assets
are emitted to `dist/` and copied into the Python package at
`../src/kiro_crew/static/dist/` so the gateway can serve them.

## Develop

```bash
npm install          # install dependencies (public npm registry)
npm run dev          # Vite dev server on http://localhost:3000 (proxies API to the gateway on :5476)
```

## Build

```bash
npm run build        # tsc -p tsconfig.app.json && vite build  → dist/
```

Every Vite build except `--watch` or one into a mount-point `dist/` writes a
scratch sibling of `dist/` and swaps it in only when it succeeds
(`scripts/publish-dist.mjs`), so `dist/` is never empty or half-written; those
two write in place.
Then stage it into the backend package so the gateway serves it:

```bash
cd .. && PYTHONPATH=src python -m kiro_crew.frontend stage .
```

The stager points `src/kiro_crew/static/dist` at `dist/` (or, for an edition, at
a fresh private copy); a running gateway follows it on its next request.

## Test and lint

```bash
npx tsc -p tsconfig.app.json   # the real type check
npm run lint         # eslint
npm run test         # website vitest suite + the Electron suite (a jscpd pretest runs first)
```

`npm run typecheck` runs `tsc -p tsconfig.app.json`, the same check as `npm run build`
and CI. The project has to be named: the root `tsconfig.json` is `files: []` plus a
reference, so a plain `tsc --noEmit` there compiles an empty program and passes
unconditionally. It is named with `-p` rather than `-b` because `tsconfig.app.json`
keeps an incremental cache (`tsconfig.app.tsbuildinfo`, gitignored): a warm no-op
run takes ~5 s instead of ~35 s, and `-p` still re-hashes every program file, so a
changed dependency is re-checked. `tsc -b` would judge staleness from the project's
own inputs only and report a changed `node_modules` `.d.ts` as up to date.
Test layers, when to use which, and how Playwright really runs:
[docs/testing.md](docs/testing.md).

## Documentation

| Document | Covers |
|---|---|
| [AGENTS.md](AGENTS.md) | The frontend rules router. Read this before changing code here. |
| [docs/](docs/README.md) | Frontend contributor docs: layout, theming, conventions, i18n, seams, testing. |
| [electron/README.md](electron/README.md) | The desktop shell's runtime surface: remote hosts, menus, tokens. |

Backend and whole-system documentation is in [../docs/](../docs/README.md).
