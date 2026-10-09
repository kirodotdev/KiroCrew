// Plain inline `code` in a chat message must read as a distinct run in EVERY
// theme, not only the two Kiro themes.
//
// MarkdownRenderer paints plain inline code with the shared CHIP_BASE classes
// (`bg-bg-elevated px-1.5 py-0.5 rounded text-sm font-mono`) — geometry and a
// mono face, deliberately NO colour (InlineCode.tsx). The only `color` for
// `.msg-content :not(pre)>code` used to live in two theme-scoped rules
// (`[data-theme="kiro-dark"]`, `[data-theme="kiro-light"]`). So on every other
// theme (e.g. Monokai dark) inline code fell back to the body text colour and
// was hard to tell apart from prose.
//
// The base rule must therefore carry a theme-aware colour of its own, so a pack
// that defines no inline-code colour still gets a legible one. It uses
// `var(--accent)`, a token every theme (built-in and custom pack) is required
// to define — see `_THEME_REQUIRED_VARS` in theme_validate.py — so no theme can
// leave inline code uncoloured, and no new token has to be allowlisted.
//
// The two Kiro rules sit at a higher specificity (an added attribute selector)
// and so still win on the Kiro themes: those colours are unchanged.
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const INDEX_CSS = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')

/** The declaration body of the FIRST (unscoped, base) `.msg-content :not(pre)>code`
 *  rule — the one with no `[data-theme=...]` / `[data-mode=...]` prefix. */
function baseInlineCodeBody(): string {
  // Match a rule whose selector is exactly `.msg-content :not(pre)>code` with no
  // leading attribute scope and no trailing attribute (so the chip-action rules
  // and the theme-scoped rules are excluded).
  const m = INDEX_CSS.match(
    /(^|\n)\.msg-content :not\(pre\)>code\s*\{([^}]*)\}/,
  )
  expect(m, 'base .msg-content :not(pre)>code rule must exist').not.toBeNull()
  return m![2]
}

describe('inline code is theme-aware in every theme', () => {
  it('the base inline-code rule sets a theme-token colour (not a hard-coded hue)', () => {
    const body = baseInlineCodeBody()
    // A `color:` must be present...
    expect(body).toMatch(/color\s*:/)
    // ...and it must be a theme token, so it tracks the active theme rather than
    // baking one colour in. A hex/rgb literal would survive a theme switch.
    expect(body).toMatch(/color\s*:\s*var\(--[a-z-]+\)/)
    expect(body).not.toMatch(/color\s*:\s*#[0-9a-fA-F]{3,8}/)
  })

  it('keeps the Kiro inline-code colours exactly as they are', () => {
    // These two rules are intentionally untouched: the fix must not change the
    // Kiro themes' inline-code colour. They override at a higher specificity.
    expect(INDEX_CSS).toContain(
      '[data-theme="kiro-dark"] .msg-content :not(pre)>code{background:#28242e;color:#c19aff}',
    )
    expect(INDEX_CSS).toContain(
      '[data-theme="kiro-light"] .msg-content :not(pre)>code{background:var(--bg-hover);color:#723acc}',
    )
  })
})
