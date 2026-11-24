# An app's loading-placeholder logo is its own opt-in field, `ui.loading.logo`, and a side-panel tab shows none

Decided by: Bolin Chen (bolichen97), Kiro Crew maintainer, 2026-11-23
Date: 2026-11-23

## Decision

An app shows a logo above the "Loading <app>…" label of its loading placeholder only when its manifest names one in `ui.loading.logo`, an image path relative to its `ui/` directory that the backend drops when it has `..`, a leading `/`, a `\` or a `:`, as the app's UI file route refuses such a path; `ui.pages[].iconUrl` and the store icon are not reused, and a side-panel tab's placeholder shows no logo.

## Why

- Reusing `ui.pages[].iconUrl` or the store icon would change the placeholder of every app that already has one without it opting in, and that sidebar art is drawn for a 20 px row. A field of its own keeps "an app that declares nothing renders exactly as today" literally true.
- `ui.loading` is already the placeholder's own object, and a side-panel tab already gets no `ui.loading` and keeps the default shape, so the logo follows the same rule there at no extra cost.
- Options not chosen: reuse the routed page's `iconUrl` (the issue author's first option); use the top-level store icon; show the logo in a side-panel tab too (can be added later without breaking this).

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/39622#issuecomment-7090628313 -- the decision, as a maintainer listed in `MAINTAINERS.md`.
- https://github.com/kirodotdev/KiroCrew/issues/39622 -- the request.
