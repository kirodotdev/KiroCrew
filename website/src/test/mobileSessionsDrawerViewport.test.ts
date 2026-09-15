import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const raw = (p: string) => readFile(join(__dirname, '..', p), 'utf8')
// Strip comments before matching. The rules below are explained in prose that
// quotes the very class names and style keys being asserted against, so a
// raw-text negative match (`fixed inset-0 z-[46]`) hits the comment that
// documents the change rather than the code.
const src = async (p: string) =>
  (await raw(p)).replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')

// The same keyboard-stranding mechanism the command palette was pinned against
// (visualViewportKeyboard.test.ts) reaches every `fixed` overlay with a focused
// input. The mobile sessions drawer is the residual surface the issue named: its
// scrim spans `inset-0` and its panel takes its vertical extent from
// `top-safe-offset-[42px]` / `bottom-safe`, and iOS Safari shrinks none of those
// for the keyboard. Both boxes are INSET to the visible band with block-axis
// margins rather than having their edges restated, which is what keeps every
// safe-area class owning its own edge — and keeps the whole fix expressible in
// numbers, where a `calc(env(…))` string would be neither parseable by happy-dom
// nor a shape the i18n literal gate accepts. Pinned in the same source-text style
// the sibling geometry guards use, so a revert fails loudly.
describe('mobile sessions drawer overlay', () => {
  it('consumes the shared visual-viewport hook', async () => {
    const s = await src('pages/ChatPage.tsx')
    expect(s, 'expected the hook').toContain('useVisualViewport()')
  })

  it('derives the covered bottom band from the visual viewport', async () => {
    const s = await src('pages/ChatPage.tsx')
    // Same expression SidePanelLayout uses, and clamped at 0 so a browser whose
    // visual and layout viewports agree contributes no inset at all.
    expect(s).toMatch(/Math\.max\(0, window\.innerHeight - vv\.offsetTop - vv\.height\)/)
  })

  it('insets the scrim to the visible band without restating inset-0', async () => {
    const s = await src('pages/ChatPage.tsx')
    // The class list is untouched — `inset-0` still spans the layout viewport, and
    // the margins pull both block edges in to what the user can actually see.
    expect(s).toMatch(/className="fixed inset-0 z-\[46\]/)
    expect(s).toMatch(/opacity: drawerScrim, marginTop: vv\.offsetTop, marginBottom: keyboardInset/)
  })

  it('keeps the scrim class list adjacent to its key, where the z-order guard looks', async () => {
    // ChatPage.composerChromeOcclusion.test.tsx reads the scrim's z-index with a
    // bounded-distance regex from `key="sessions-backdrop"`. Anything inserted
    // between the two — a comment above the className is the easy mistake — puts
    // `z-[46]` out of its reach and silently turns that whole file's ordering
    // assertions into comparisons against `undefined`. Deliberately the RAW
    // source: a comment is exactly what this measures, so stripping comments
    // first would make the assertion unfailable.
    const s = await raw('pages/ChatPage.tsx')
    expect(s).toMatch(/key="sessions-backdrop"[\s\S]{0,240}?z-\[46\]/)
  })

  it('insets the panel with visual-viewport margins, keeping its safe-area edges', async () => {
    const s = await src('pages/ChatPage.tsx')
    // Scoped to the sessions drawer's OverlayDrawer element, not the whole file:
    // OTHER mobile surfaces in ChatPage (the floating open-sessions button, the
    // right-side inline overlay) also carry `top-safe-offset-[42px]`, so a
    // file-wide match would not tell this panel apart from them.
    const start = s.indexOf('<OverlayDrawer open=')
    expect(start, 'expected the sessions drawer element').toBeGreaterThan(0)
    const call = s.slice(start, start + 1200)
    // The vertical inset is BLOCK-AXIS MARGINS off the visual viewport…
    expect(call).toMatch(/slideStyle=\{isMobile \? \{ marginTop: vv\.offsetTop, marginBottom: keyboardInset \} : undefined\}/)
    // …so the CSS edges that compose `env()` safe insets with the 42px header gap
    // stay exactly where they were. Restating either in JS is what forced a
    // `calc(env(…))` string, which is neither testable in happy-dom nor a shape
    // the i18n literal gate accepts.
    expect(call, 'the safe-area top inset must stay on the className')
      .toMatch(/top-safe-offset-\[42px\]/)
    expect(call, 'the safe-area bottom inset must stay on the className')
      .toMatch(/bottom-safe(?![-\w])/)
    expect(call, 'the vertical extent must not be restated in JS')
      .not.toMatch(/\bcalc\(/)
    // …while the horizontal safe inset is untouched, as is the slide channel.
    expect(call).toMatch(/mobile-sessions-overlay fixed top-safe-offset-\[42px\] bottom-safe left-safe/)
  })
})

describe('OverlayDrawer slide-mode style merge', () => {
  it('keeps x as the sole transform owner and merges the vertical inset without overriding width/x', async () => {
    const s = await src('components/OverlayDrawer.tsx')
    // `slideStyle` is spread FIRST, then width and x, so the caller's vertical
    // inset can never displace the horizontal slide the compositor owns.
    expect(s).toMatch(/style=\{\{ \.\.\.slideStyle, width, x: slideX \}\}/)
    expect(s).toContain('slideStyle?: React.CSSProperties')
  })
})
