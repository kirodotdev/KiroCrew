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
npm run build        # tsc -p tsconfig.app.json && vite build  → dist/
```

After building, stage `dist/` into the backend package so the gateway serves it.
Clear the destination first: Vite emits content-hashed filenames, so copying over an
existing bundle accumulates stale assets.

```bash
rm -rf ../src/kiro_crew/static/dist && cp -r dist ../src/kiro_crew/static/dist
```

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
