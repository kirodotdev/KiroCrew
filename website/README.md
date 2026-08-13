# KiroCrew Website

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
npm run build        # tsc -b && vite build  → dist/
```

After building, stage `dist/` into the backend package so the gateway serves it.
Clear the destination first: Vite emits content-hashed filenames, so copying over an
existing bundle accumulates stale assets.

```bash
rm -rf ../src/kiro_crew/static/dist && cp -r dist ../src/kiro_crew/static/dist
```

## Test and lint

```bash
npx tsc -b           # the real type check
npm run lint         # eslint
npm run test         # website vitest suite + the Electron suite (a jscpd pretest runs first)
```

**`npm run typecheck` checks zero files** (it is `tsc --noEmit`, and the root
`tsconfig.json` has `"files": []`), so use `npx tsc -b` when you mean to type-check.
Test layers, when to use which, and how Playwright really runs:
[docs/testing.md](docs/testing.md).

## Documentation

| Document | Covers |
|---|---|
| [AGENTS.md](AGENTS.md) | The frontend rules router. Read this before changing code here. |
| [docs/](docs/README.md) | Frontend contributor docs: layout, theming, conventions, i18n, seams, testing. |
| [electron/README.md](electron/README.md) | The desktop shell's runtime surface: remote hosts, menus, tokens. |

Backend and whole-system documentation is in [../docs/](../docs/README.md).
