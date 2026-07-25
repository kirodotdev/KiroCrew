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
