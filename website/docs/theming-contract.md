# Theming / Customization Contract

The dashboard is fully themable. A **theme** ranges from a color palette
(Level 0) up to a full experience pack; a color theme is the degenerate case of
a pack. Themes are a **standalone subsystem built on `useTheme`**, not apps.
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
with the theme CSS custom properties or Tailwind classes mapped to them,
**never** a hardcoded `#hex` / `rgb(...)` / `rgba(...)` literal.

```tsx
// don't
<div style={{ background: '#16213e', color: '#fff' }} />
<div className="bg-gray-900 text-white" />

// do
<div style={{ background: 'var(--card)', color: 'var(--card-fg)' }} />
<div className="bg-[var(--card)] text-[var(--card-fg)]" />
```

The 43 CSS variables are the single source of truth for color. They are the
customization surface a theme (built-in, custom, or installed) can set.

## Adding a new color role

When you genuinely need a new color role, add the variable to **both** sides in
parity (a parity test guards drift), then define it in **every** built-in theme:

- Frontend: `ALLOWED_CSS_VARS` in `src/hooks/useTheme.tsx`
- Backend: `_THEME_CSS_VARS_SET` (built from `_THEME_CSS_VARS`) in
  `src/kiro_crew/dashboard/theme_validate.py`

Never introduce a one-off literal instead of a variable.

Both sides are checked from Python: `test/test_theme_css_security.py`
(`TestCssVarsSetSync`) asserts the required roles and the shadow roles are in the
backend set and that an unknown name is not, and `TestThemeVarsFilter` asserts the
filter keeps known keys, drops unknown ones, and drops unsafe values. The CSS
parsers on the two sides are pinned against each other by a shared fixture,
`test/fixtures/theme_css_corpus.json`: `test/test_theme_install.py`
(`TestCssParserCorpus`) asserts the `installAccepts` column and
`src/test/themeCssCorpus.test.tsx` asserts the `runtimeKeeps` column of the same
cases, so a future divergence between them fails a test rather than a user. The
two parsers differ by design (install-time is a denylist, runtime is a positive
allowlist), which is exactly why the corpus pins both verdicts.

`src/test/PhasedViewTheme.test.tsx` guards the other drift direction: it parses
`index.css` for every custom property any theme block defines, and asserts a view
references no token that does not exist. A `var(--nope, #16213e)` fallback would
otherwise always win and silently ignore the active theme.

## What is / isn't customizable

| Tier | Surface |
|---|---|
| **L0 Color** | the 43 CSS vars (dark + light) |
| **L1 Brand** | logo, favicon, wordmark, botName, fonts, scoped `overrides.css` |
| **L2 Experience** | sandboxed overlays, topbar, audio, persona |

Out of contract: app structure/routing, functional-control behavior, security
chrome, and anything outside the CSS-var set + the `overrides.css` selector
allowlist.

## Stable hooks

An L1 pack's `overrides.css` may only target the surfaces below. This is the list
that the source comments citing this file point at, and it is the runtime
boundary, not a style suggestion: `_scopeOverridesCss` in
`src/hooks/useTheme.tsx` DROPS every rule whose selector group does not pass, so a
rule aimed at anything else never reaches the document.

**Six class hooks** (`_ALLOWED_CLASSES`):

| Class | Where it is applied |
|---|---|
| `topbar` | the header shell (`App.tsx`) |
| `sidebar` | the chat session list (`ChatSidebar.tsx`) |
| `chat-container` | the chat scroll region (`ChatPane.tsx`, `ChatPage.tsx`) |
| `message-bubble` | a user or assistant turn (`chat/UserMessage.tsx`, `chat/AssistantMessage.tsx`) |
| `input-area` | the composer (`ChatInput.tsx`) |
| `code-block` | a rendered fenced block (`CodeBlock.tsx`, `MonacoCodeBlock.tsx`) |

Do not rename or drop one of these classes when refactoring the component that
carries it. There is no compiler reference to break, so the only signal is a
shipped pack quietly losing its styling. Keep the comment next to the class too:
it is what tells the next reader the class is API.

**Element hooks** (`_ALLOWED_ELEMENTS` is `''`, `body`, `button`):

- **`button.primary`** is a special case: a bare `button` selector is rejected, and
  a `button` compound is kept ONLY when it also carries `.primary`. So a pack can
  restyle the primary action and cannot restyle every button in the app.
- **Bare `body`** is allowed, but only bare: `body`, `body::before`, `body::after`
  (a single-colon `:before` / `:after` is tolerated). Any class on `body`, or any
  other pseudo-element on it, is rejected. The two pseudo-elements exist for the
  decorative-overlay idiom (a scanline, a vignette).
- The empty-string element means a class-only compound such as `.topbar:hover`,
  which is the normal case.

**Forbidden classes** (`_FORBIDDEN_CLASSES`, rejected even when chained onto an
otherwise-allowed compound): `token`, `credential`. These name credential-bearing
chrome, and a pack that could restyle them could hide or spoof them.

**Selector shape.** Every selector in a comma group must pass, and each one must
be a single compound:

- **No combinators.** Whitespace, `>`, `+` and `~` are all rejected, so
  `.topbar .btn` never applies. Style the hook itself, not its descendants.
- **No ids and no attribute selectors** in the compound. This is what blocks
  `#app-root` and `[data-auth]`.
- **One optional `[data-theme="…"]` prefix** may lead the selector, with or without
  a leading `html`, and it is stripped before the compound is checked. So
  `[data-theme="mytheme-dark"] .topbar` and `html[data-theme="mytheme-dark"] body`
  are both fine, but a second prefix is not.
- Chained classes and pseudo-classes on the SAME base are fine
  (`.topbar:hover`, `.message-bubble.mine`). Single-colon pseudo-classes are
  ignored by the check.

An `@media` block is recursed into with its wrapper preserved and its inner rules
filtered the same way; every other at-rule is dropped. A kept rule's declaration
body is then denylisted (`@import`, `expression()`, `javascript:`, `-moz-binding`,
an external `url()`), both raw and after CSS escape-decoding, so an escaped token
cannot hide from the scoper.

**Install-time forbidden selectors.** The backend has its own, independent check.
`_THEME_CSS_FORBIDDEN` in `src/kiro_crew/dashboard/theme_validate.py` rejects a
pack outright if its `overrides.css` contains any of `iframe`, `script`,
`[data-auth]`, `.token`, `.credential`, `#app-root`, matched case-insensitively
against both the raw text and a comment-stripped, escape-decoded copy. The same
module also rejects rules that could hijack the viewport or block interaction
(`z-index` above 9999, `display:none`, `pointer-events:none`, a
viewport-covering `position:fixed`), with an exemption for purely decorative
`body::before` / `body::after`.

The two layers are deliberately different models: install-time is a denylist that
refuses the pack with an explainable error, and the runtime scoper is the positive
allowlist that is the actual enforced boundary. A rule that slips past the former
still gets dropped by the latter.

## Chat loader (compiled seam, not an installed pack)

The loading indicator in the chat footer (shown while a turn is running) is
theme-owned, but it is a **compiled seam, not a manifest capability**. It is
declared in code through `registerThemeBranding()` (`src/themeBranding.tsx`),
which runs at module load from the composition root (`src/extensions.ts`), so it
is available to themes **bundled in the build**: the core's own themes and a
downstream edition's. An *installed* `theme.json` pack cannot ship executable
registration, so it cannot set a loader; a pack that needs one has to land as a
compiled theme instead. (A pack can still restyle whatever loader is active via
CSS; see the colour note below.)

Two levels, pick one:

```tsx
import { registerThemeBranding } from '@/themeBranding'

registerThemeBranding({
  mytheme: {
    logo: '/mytheme/logo.png',

    // Level 1: keep the stock carousel, swap the artwork it cycles.
    loaderIcons: [Sun, Moon, Star, Cloud, Comet],

    // Level 2: replace the indicator outright (wins over loaderIcons).
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

**`loader`** replaces the whole indicator with your component: a mascot
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
lucide ships no mascot marks, so your own art is exempt, but **only while it stays
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

That is how the bundled Kiro poses get their light-palette outline; see
`src/components/GhostPoses.tsx` and `src/assets/onboarding/GhostIcons.tsx`.

One implementation constraint if you write a custom `loader`: the carousel's
cross-fade animation lives on a persistent `.lyr` wrapper rather than on the icon,
because swapping an icon changes the rendered component type and remounts its
element, and animating the icon itself would restart that animation and desync it
from the other layer. If your loader swaps artwork on a timer, animate a stable
wrapper for the same reason.

Registration is read at module load (see `src/extensions.ts`); registering after
the shell has rendered does not take effect until the next theme switch.

Authoring a compiled (edition) theme end to end — CSS specificity against the
core palette, module resolution, typechecking — is covered in
[extension-seams § Authoring an edition](extension-seams.md#authoring-an-edition-the-build-pitfalls).

## Checker (advisory)

```bash
npm run lint:theme-colors          # report raw literals in src/ (exit 0)
node scripts/check-theme-colors.mjs --strict   # exit 1 if any (future ratchet)
```

The checker excludes the five files where a raw literal is legitimate
(`src/hooks/useTheme.tsx`, `src/components/themeEditor.tsx`, `src/index.css`,
`src/lib/cssSanitize.ts`, `src/utils/sessionColors.ts`), plus tests, generated
code, and type declarations. It is **advisory** (the existing tree has legitimate
literals in themes, icons and palettes) and is **not** wired into the blocking CI
gate; `--strict` becomes a CI gate once the baseline is burned down.
