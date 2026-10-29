# The kiro-light card gate

`website/scripts/check-kiro-light-cards.mjs`, run as the step "kiro-light cards
carry their own edge" in the `e2e` job of `.github/workflows/ci.yml`.

## The rule it enforces

`website/docs/theming-contract.md` says a card must carry its own edge. In
`kiro-light`, `--card`, `--bg-elevated` and `--bg` are all `#ffffff`, so a
`bg-card` box on the page with no border, ring or shadow paints nothing in that
theme and looks fine in every other one. Before this gate only a reviewer who
remembered the paragraph could catch a new one.

A class-attribute grep cannot answer the question. An edge can come from an
inline `style`, a parent's `divide-*` or a `ring-*` utility (a box-shadow), and
a fill that equals its parent in every theme (an opaque cover) is not a card.
So the gate reads computed styles on rendered pages.

## What it does

1. Serves the built SPA (`website/dist`) on `127.0.0.1` and answers every
   `/api/**` call from the shared boot fixtures (`website/scripts/lib/boot-api.mjs`)
   plus one chat slot with a two-message transcript. It needs no gateway, token or
   network.
2. Opens each route in `ROUTES` in kiro-light: the chat page, Settings (Overview,
   Chat, Display / Theme, Security), Schedule, Artifacts, Knowledge, the Apps
   library, Members, and every built-in app page in
   `website/src/apps/builtinRegistry.ts` (23 pages, from `/worlds` to
   `/project-scaffolder`). App pages that index straight into a response shape
   the generic `[]` / `{}` fallback cannot satisfy get their answers from
   `APP_FIXTURES` in the script: AWS Control, File Explorer, PPTX Maker,
   Mission Control, Notes (`/md-notebook`) and Mochi. Each of those fixtures
   carries one populated row where the page has a list, and the route's `ready`
   text is that row, so the gate checks the page with content on it rather than
   its empty or error state. A route that never renders, renders an error boundary,
   throws a page error, or does not apply `data-theme="kiro-light"` fails the
   gate. A blank page cannot pass.
3. Turns transitions and animations off, then checks every element of at least
   16x16 px that has a background. The element's background is composited over
   its painted ancestors, and the element is reported when:
   - in kiro-light it matches what is behind it (every sRGB channel within 3),
   - it has no visible edge in kiro-light: no border side, `box-shadow` layer
     or outline whose colour differs from the backdrop. A shadow layer also
     needs a non-zero offset, blur or spread, because `shadow-none` and `ring-0`
     compute to transparent zero-size layers rather than `none`,
   - and after switching `data-theme` / `data-mode` in place to `light` or
     `kiro-dark`, it differs from what is behind it.
4. Plants controls on each route (`CANARIES`): a bare `var(--card)` box and one
   carrying a transparent zero-size shadow must be reported, and the same box
   with a `1px solid var(--border)` border or a 1px ring must not be. If any
   control comes out wrong, the probe is broken on that route and the gate
   fails.

Each report line is `route  selector  (size, background, the themes where it is a card)`.
`--json <file>` also writes every hit for each route.

## Fixing a failure

Give the surface a `border-border`, a `ring-1 ring-border` or a `shadow-*`, the
way the settings cards already do. A surface that must stay borderless gets a
kiro-light-scoped hook in `website/src/styles/chat-transcript.css` (pinned by
`website/src/test/kiroLightShellHooks.test.ts`), or as a last resort an entry in
`EXEMPT` at the top of the script with the reason. An `EXEMPT` selector that
matches nothing on any route fails the gate, so the list cannot go stale.

## Adding a route

Add `{ path, name, ready }` to `ROUTES`. A new built-in app page belongs here
as soon as it is registered. A page that only shows its real content once a
specific API answers needs that answer in `APP_FIXTURES`, keyed by pathname.
Without it the page stops at its empty or error state and the gate checks only
that state. Set `ready` to text the real content shows, so a page stuck at a
loading state fails instead of passing.

## Local run

```
cd website && npm run build && node scripts/check-kiro-light-cards.mjs
```
