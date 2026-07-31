# Theming / Customization Contract

The dashboard is fully themable. A **theme** ranges from a color palette
(Level 0) up to a full experience pack; a color theme is the degenerate case of
a pack. Themes are a **standalone subsystem built on `useTheme`** — not apps.
Source of truth: the in-repo system spec
[`docs/system-specs/modules/themes.md`](../../docs/system-specs/modules/themes.md).
This document is the **frontend pack-author contract**; the spec governs the
end-to-end subsystem (install pipeline, validation, routes, security model).

## The rule for contributors

**Pack manifest versioning:** every `theme.json` MUST declare
`"formatVersion": 1` (integer). KiroCrew rejects packs with a missing value or
an unknown major with an explicit "this pack requires a newer version of
KiroCrew" error. Author against the current major; breaking manifest changes
bump it.

**Every new UI element MUST be themable at least at the color layer.** Style it
with the theme CSS custom properties or Tailwind classes mapped to them —
**never** a hardcoded `#hex` / `rgb(...)` / `rgba(...)` literal.

```tsx
// ❌ don't
<div style={{ background: '#16213e', color: '#fff' }} />
<div className="bg-gray-900 text-white" />

// ✅ do
<div style={{ background: 'var(--card)', color: 'var(--card-fg)' }} />
<div className="bg-[var(--card)] text-[var(--card-fg)]" />
```

The 43 CSS variables are the single source of truth for color. They are the
customization surface a theme (built-in, custom, or installed) can set.

## Adding a new color role

When you genuinely need a new color role, add the variable to **both** sides in
parity (a parity test guards drift), then define it in **every** built-in theme:

- Frontend: `ALLOWED_CSS_VARS` in `src/hooks/useTheme.tsx`
- Backend: `_THEME_CSS_VARS_SET` in `src/kiro_crew/dashboard/handlers/agents.py`

Never introduce a one-off literal instead of a variable.

## What is / isn't customizable

| Tier | Surface |
|---|---|
| **L0 Color** | the 43 CSS vars (dark + light) |
| **L1 Brand** | logo, favicon, wordmark, botName, fonts, scoped `overrides.css` |
| **L2 Experience** | sandboxed overlays, topbar, audio, persona |

Out of contract: app structure/routing, functional-control behavior, security
chrome, and anything outside the CSS-var set + the `overrides.css` selector
allowlist.

## Chat loader (compiled seam, not an installed pack)

The loading indicator in the chat footer — shown while a turn is running — is
theme-owned, but it is a **compiled seam, not a manifest capability**. It is
declared in code through `registerThemeBranding()` (`src/themeBranding.tsx`),
which runs at module load from the composition root (`src/extensions.ts`), so it
is available to themes **bundled in the build**: the core's own themes and a
downstream edition's. An *installed* `theme.json` pack cannot ship executable
registration, so it cannot set a loader — a pack that needs one has to land as a
compiled theme instead. (A pack can still restyle whatever loader is active via
CSS; see the colour note below.)

Two levels, pick one:

```tsx
import { registerThemeBranding } from '@/themeBranding'

registerThemeBranding({
  mytheme: {
    logo: '/mytheme/logo.png',

    // Level 1 — keep the stock carousel, swap the artwork it cycles.
    loaderIcons: [Sun, Moon, Star, Cloud, Comet],

    // Level 2 — replace the indicator outright (wins over loaderIcons).
    loader: MyMascotLoader,
  },
})
```

**`loaderIcons`** is the easy path and the one to reach for first. The default
loader is a 4-slot carousel: each slot cross-fades between two icons, the slots
cascade 0.25s apart on a 2.8s beat, and every beat re-samples **4 distinct** icons
from your pool (never repeating the set it replaces or the other layer). Supply at
least 4; more gives more variety. You inherit the cross-fade, the cascade timing
and the reduced-motion handling for free.

**`loader`** replaces the whole indicator with your component — a mascot
animation, a progress bar, a canvas, anything. It renders with no wrapper beyond
the footer's padding, so it owns its size, layout and motion. Keep it small (the
band is ~32px tall), mark it `aria-hidden` (it is decorative), and honour
`prefers-reduced-motion` yourself.

Resolution is `loader` → `loaderIcons` → artwork bundled for a core theme → the
default icons, so a theme that registers neither renders exactly what it does
today, and an empty pool falls back rather than rendering nothing. Both branches
render inside an `ErrorBoundary fallback={null}`: the loader is decorative, so a
component that throws collapses to nothing instead of escaping to the route
boundary and replacing the chat UI with an error card.

**Colour belongs in your CSS, not in the artwork.** Each icon renders itself,
takes no props, and is sized to 14px by the carousel. A `lucide-react` glyph
inherits `currentColor` (the accent) and needs no styling at all.

For bespoke brand art, mind the `use-lucide-icons` rule (`website/AUTOSDE.yaml`):
lucide ships no mascot marks, so your own art is exempt — but **only while it stays
an asset**. Keep the art in an `.svg` file, import it by URL, and render it in an
`<img>`; no `<svg>` element or path data may appear in a `.tsx` file (the CI gate
blocks that in every file, tests included). Theme it by filtering the `<img>`,
which traces the rendered alpha, so one asset serves every palette:

```css
[data-theme="mytheme-light"] .csb4 .lyr > .my-mark {
  filter: drop-shadow(.6px 0 0 #000) drop-shadow(0 .6px 0 #000)
          drop-shadow(-.6px 0 0 #000) drop-shadow(0 -.6px 0 #000);
}
```

That is how the bundled Kiro poses get their light-palette outline — see
`src/components/GhostPoses.tsx` and `src/assets/onboarding/GhostIcons.tsx`.

One implementation constraint if you write a custom `loader`: the carousel's
cross-fade animation lives on a persistent `.lyr` wrapper rather than on the icon,
because swapping an icon changes the rendered component type and remounts its
element — animating the icon itself would restart that animation and desync it
from the other layer. If your loader swaps artwork on a timer, animate a stable
wrapper for the same reason.

Registration is read at module load (see `src/extensions.ts`); registering after
the shell has rendered does not take effect until the next theme switch.

## Checker (advisory)

```bash
npm run lint:theme-colors          # report raw literals in src/ (exit 0)
node scripts/check-theme-colors.mjs --strict   # exit 1 if any (future ratchet)
```

The checker excludes the theme-definition files (`useTheme.tsx`,
`themeEditor.tsx`, `index.css`, `cssSanitize.ts`, `sessionColors.ts`), tests,
and generated code. It is **advisory** today (the existing tree has legitimate
literals in themes/icons/palettes) and is **not** wired into the blocking CI
gate — enabling `--strict` in CI is a follow-up once the baseline is burned
down.
