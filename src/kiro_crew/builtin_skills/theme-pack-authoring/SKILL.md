---
name: theme-pack-authoring
description: Build, validate, and install Kiro Crew theme packs -- pack anatomy, the 56-variable palette, role-tagged fonts, the overrides.css allowlist (what installs vs what actually renders), and the install-verify cycle. Use when the user wants to create or edit a theme pack.
triggers: theme pack, custom theme, theme.json, variables.json, overrides.css, install theme, theme font, dashboard theme, build a theme
---

# Kiro Crew theme-pack authoring

House rules for building theme packs. The authoritative contract is
[`website/docs/theming-contract.md`](https://github.com/kirodotdev/KiroCrew/blob/main/website/docs/theming-contract.md);
this skill is the self-contained task-oriented digest, plus the traps that cost
real debugging time.

## Pack anatomy

```
my-theme/
├── theme.json          # manifest: slug, name, emoji, level, formatVersion, fonts[]
├── variables.json      # dark + light palettes (56 allowlisted CSS vars)
├── readme.md           # optional; attribution and notes
├── styles/
│   ├── overrides.css   # optional; scoped structural CSS (see allowlist below)
│   └── fonts/          # .woff2 / .ttf files, max 6 faces, 512 KB each
└── LICENSE.txt         # validates as meta, is NOT copied into the install
```

Levels: **0** = colors only. **1** = + fonts, branding, overrides.css. **2** = +
overlays/topbar/audio/persona. Declare the lowest level that covers the payload —
a level-0 pack shipping a font is refused.

## theme.json — complete working example

```json
{
  "slug": "my-theme",
  "name": "My Theme",
  "emoji": "🎨",
  "level": 1,
  "formatVersion": 1,
  "loaderIcons": ["star", "sparkles", "moon", "cloud"],
  "fonts": [
    { "family": "My Sans", "file": "my-sans-400.woff2", "weight": 400, "role": "sans" },
    { "family": "My Sans", "file": "my-sans-500.woff2", "weight": 500, "role": "sans" },
    { "family": "My Sans", "file": "my-sans-600.woff2", "weight": 600, "role": "sans" },
    { "family": "My Mono", "file": "my-mono-400.woff2", "weight": 400, "role": "mono" }
  ]
}
```

`loaderIcons` is optional and requires Level 1 or 2. Supply 4–8 distinct names
from: `cloud`, `flower`, `heart`, `moon`, `sparkles`, `star`, `sun`, `zap`.
Kiro Crew maps these names to bundled Lucide symbols and keeps the stock
cross-fading carousel; packs cannot inject loader code or SVG through this field.
Omit it to retain the default Kiro ghost poses.

## Fonts — the role system (the ONLY supported route)

- Each face carries `role: "sans"` or `"mono"`. Absent role = `sans`.
- `sans` faces feed the **Sans** Font Family option; `mono` faces feed **Mono**
  AND code surfaces (code blocks, inline code, diffs) under every option.
- **System always stays the OS font** — a pack cannot reach it. Unfilled roles
  fall back to Kiro Crew's own stacks.
- Max **6 faces across both roles**; pick weights deliberately. The UI uses
  400/500/600/700; with 3 sans slots ship 400/500/600 (`font-bold` resolves to
  600, acceptable; 400/500/700 makes the many semibold elements render heavy).
- CJK/Devanagari/Bengali fallbacks are wired automatically — the generated
  stacks carry the script-fallback aliases. Latin-only faces are fine.
- **Never set fonts in overrides.css.** `--font-body`, `--mono`, the
  `--theme-font-*` tokens, or `font`/`font-family` on `body`/`html`/`*`/`:root`
  are rejected at install and dropped at runtime (CSS-escape evasions included).
  A `font-family` on ONE allowlisted surface (e.g. `.topbar`) is fine.
- Ship the font's license file in the pack source (OFL/Apache/MIT).
- Only `"sans"` and `"mono"` are valid; a wrong role *string* (e.g. the CSS
  keyword `"monospace"`) is **rejected at install** with an error naming the
  font and the bad value. An already-installed pack whose role predates this
  check keeps loading — the coercion-to-`sans` behavior only remains on that
  read path, never on a fresh install.

## variables.json — the palette

Two blocks, `dark` and `light`, each holding up to **56 allowlisted variables**.
Required minimum per block: `--bg`, `--text`, `--accent`. Unknown keys are
REJECTED (install fails), so do not invent variables; the allowlist is
`_THEME_CSS_VARS` in `kiro_crew/dashboard/theme_validate.py`.

- To clone a built-in theme's palette, transcribe its block from
  `website/src/index.css` (e.g. `[data-theme="kiro-dark"]`), keeping only
  allowlisted vars.
- `--accent-fg` and the four `--json-*` highlight colors (`--json-key`,
  `--json-str`, `--json-num`, `--json-bool`) are allowlisted — set them directly
  in `variables.json` like any other palette entry. No overrides.css workaround
  is needed for them.
- Two terminal hues (`--term-magenta`, `--term-cyan`) and the seven `--diff-*`
  vars are allowlisted too. The other fourteen terminal colors are derived from
  `--bg` / `--text` / `--danger` / `--ok` / `--warn` / `--info`, so a pack that
  wants its own terminal palette sets only those two.

## overrides.css — what installs is NOT what renders

Two different filters run, and they disagree by design:

- **Install** = denylist. Refuses known-bad: `@import`, external `url()`,
  `expression()`, font pins, `display:none`, viewport-covering `position:fixed`,
  `z-index` > 9999, selectors touching `iframe`/`script`/`.token`/`[data-auth]`.
- **Runtime** = allowlist, the real boundary. Only rules targeting these
  surfaces survive; EVERYTHING else is silently dropped at apply time:
  `body` (and `body::before/::after`), `button.primary`, `.topbar`, `.sidebar`,
  `.chat-container`, `.message-bubble`, `.input-area`, `.code-block`.
  No descendant/child/sibling combinators, no ids, no attribute selectors
  (one leading `[data-theme="…"]` scoping prefix is allowed and stripped).

Consequence: a rule can pass install and never render. The browser console
lists any rules the runtime dropped from the active theme (`[theme]
overrides.css: dropped …`), and Settings → Display shows the same list under the
theme selector whenever the active theme has any, with a link to the theming
contract. That notice is condition-derived: it disappears on its own once you fix
the pack and re-install, and that vanishing IS the confirmation. If a rule you
wrote has no effect, check there BEFORE suspecting specificity.

A compound on the same base survives, so `.topbar.compact:hover` is fine, and
the scoping prefix may be written `html[data-theme="…"]` too. One failing
selector kills the WHOLE comma group, so keep a risky selector in its own rule.
`@media` wrappers survive — the wrapper is kept and its inner rules filtered by
the same allowlist — while every other at-rule (`@font-face`, `@supports`,
`@import`) is dropped at runtime.

Each level also caps the pack as a whole: 32 entries / 256 KB at level 0, 64 /
2 MB at level 1, 160 / 5 MB at level 2. With 512 KB per font face, the level-1
total is what a font-heavy pack actually hits — budget the faces against 2 MB,
not against the count of 6.

Beyond `theme.json`, `variables.json`, `readme.md`, `styles/` and `LICENSE.txt`,
the classifier also recognizes `branding/logo.{svg,png}`,
`branding/favicon.{ico,png,svg}`, `branding/wordmark.{svg,png}` and
`branding/preview.{png,webp}` at level 1, and `persona.md`, `overlays/*.html`,
`topbar/{dark,light}.html`, `audio/manifest.json` and `audio/*.{mp3,ogg,wav}` at
level 2. `styles/variables.json` is accepted as an alternative to the top-level
file. Every path is classified against that fixed table, so an unrecognized file
fails the install — do not park notes or scratch files in the pack (only VCS and
LICENSE metadata is tolerated). Level-2 caps: at most 5 overlays, 2000 characters
of `persona.md`, 48 characters of bot name.

## Validate and install

**Install IS the validator.** Install via Settings → Display → Install theme
(local folder path or a `github.com` repo URL). A refusal message names exactly
what to change; re-install overwrites, which is the update path. Iterate by
editing the pack source and re-installing — do not hand-edit the installed copy
under the data directory, which bypasses validation.

In a Kiro Crew dev checkout, `_validate_theme_dir(pack_dir, installing=True)`
from `kiro_crew.dashboard.theme_validate` runs the same check programmatically.

## Verify like you mean it

- Toggle a built-in theme vs your pack on the same screen — an A/B flip beats
  memory.
- Check: the chat transcript (body face), small accent badges (bold weight), a
  message with inline code and a link, a JSON payload (the `--json-*` patch),
  primary buttons (`--accent-fg`), Sans/Mono/System switching in Settings, and
  the dropped-rules notice staying absent.
