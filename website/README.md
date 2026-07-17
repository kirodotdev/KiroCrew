# KiroCrew Website

React + TypeScript + Vite single-page app for the KiroCrew dashboard. Built
assets are emitted to `dist/` and copied into the Python package at
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

After building, copy `dist/` into the backend package so the gateway serves it:

```bash
rm -rf ../src/kiro_crew/static/dist && cp -r dist ../src/kiro_crew/static/dist
```

## Test & lint

```bash
npm run typecheck    # tsc -b
npm run lint         # eslint
npm run test         # vitest
```

## Conventions

See `AGENTS.md` for icon (lucide-react, no emoji), data-fetching (React Query),
styling (Tailwind), accessibility, and page-layout conventions.
