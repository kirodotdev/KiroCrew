import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { contrastRatio, parseCssColor, relativeLuminance } from '../../lib/iconContrast'
import { derivedDefaultVars } from '../../hooks/themeCss'
import { CHAT_AA_TEXT, chatThemeVars, readableText } from './pageChatTheme'

/** The measured WCAG contrast of one CSS colour on another (both opaque). */
function ratio(fg: string, bg: string): number {
  const f = parseCssColor(fg)!
  const b = parseCssColor(bg)!
  return contrastRatio(relativeLuminance(f.r, f.g, f.b), relativeLuminance(b.r, b.g, b.b))
}

describe('readableText', () => {
  it('picks white on a dark background and black on a light one', () => {
    expect(readableText('#101418')?.color).toBe('#ffffff')
    expect(readableText('#f3f4f6')?.color).toBe('#000000')
  })

  it('reports the contrast of its chosen text, >= 4.5 on both ends', () => {
    const dark = readableText('#101418')!
    const light = readableText('#f3f4f6')!
    expect(dark.ratio).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
    expect(light.ratio).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
    // The reported ratio is the real one for the returned colour.
    expect(dark.ratio).toBeCloseTo(ratio(dark.color, '#101418'), 5)
  })

  it('returns null for an unparseable colour, and for a non-opaque one', () => {
    expect(readableText('rebeccapurple')).toBeNull()
    expect(readableText('')).toBeNull()
    expect(readableText('rgba(0,0,0,0)')).toBeNull()
  })
})

describe('chatThemeVars', () => {
  it('is empty with no tokens — the default-off, byte-for-byte state', () => {
    expect(chatThemeVars(null)).toEqual({})
    expect(chatThemeVars(undefined)).toEqual({})
    expect(chatThemeVars({})).toEqual({})
  })

  it('keeps --text and --muted >= 4.5:1 on EVERY derived surface (bg, card, elevated)', () => {
    // Dark, light, and the mid-tones the reviewers cited (#515151 / #6a6a6a /
    // #687030), where an uncapped elevated surface or a bg-only muted check used
    // to drop below the 4.5:1 floor.
    for (const bg of ['#101418', '#f3f4f6', '#1e3a8a', '#fef3c7', '#8855ee', '#515151', '#6a6a6a', '#687030']) {
      const vars = chatThemeVars({ '--bg': bg })
      expect(vars['--bg']).toBe(bg)
      // Normal text clears AA on the background, the card AND the elevated surface.
      for (const surface of [vars['--bg'], vars['--card'], vars['--bg-elevated']]) {
        expect(ratio(vars['--text'], surface)).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
        expect(ratio(vars['--muted'], surface)).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
      }
      expect(ratio(vars['--card-fg'], vars['--card'])).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
    }
  })

  it('steps the card and chrome surfaces OFF the background so bubbles are visible', () => {
    const vars = chatThemeVars({ '--bg': '#2a1a4d' })
    // The card is a distinct surface, not the raw background (the UX finding:
    // bubbles must not melt into the column).
    expect(vars['--card']).toBeDefined()
    expect(vars['--card']).not.toBe(vars['--bg'])
    expect(vars['--bg-hover']).not.toBe(vars['--bg'])
    expect(vars['--bg-elevated']).not.toBe(vars['--bg'])
    // A visible edge is defined.
    expect(vars['--border']).toBeTruthy()
  })

  it('refuses an accent-only page (no --bg): links stay on the app accent', () => {
    // With no page background there are no chat surfaces to measure the accent
    // as text against, so the accent is not followed (links/caret keep the app
    // accent, which is readable) rather than tinted unchecked.
    expect(chatThemeVars({ '--accent': '#7c3aed' })).toEqual({})
    expect(chatThemeVars({ '--accent': '#1e1b4b' })).toEqual({})
  })

  it('pairs the accent with a readable label colour (with a page background)', () => {
    const vars = chatThemeVars({ '--bg': '#ffffff', '--accent': '#5b21b6' })
    expect(vars['--accent']).toBe('#5b21b6')
    expect(ratio(vars['--accent-fg'], vars['--accent'])).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
  })

  it('derives >= 4.5:1 text for every sRGB background (the worst case still clears)', () => {
    // Black-or-white against any sRGB colour never drops below ~4.58:1, so the
    // chat always gets readable text. `#8855ee` is the measured worst case.
    for (const bg of ['#8855ee', '#767676', '#008080', '#9c6b2f']) {
      const vars = chatThemeVars({ '--bg': bg })
      expect(vars['--bg']).toBe(bg)
      expect(ratio(vars['--text'], vars['--bg'])).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
    }
  })

  it('refuses a non-opaque background rather than theme against a false backdrop', () => {
    // rgba with zero alpha would composite to a fiction; the chat keeps its theme.
    expect(chatThemeVars({ '--bg': 'rgba(0,0,0,0)' })).toEqual({})
    expect(chatThemeVars({ '--bg': 'rgba(10,10,40,0.4)' })).toEqual({})
  })

  it('ignores an unparseable token instead of applying it', () => {
    expect(chatThemeVars({ '--bg': 'var(--something)' })).toEqual({})
  })

  it('clamps out-of-range channels to what the browser paints (rgb(0,0,500))', () => {
    // CSS clamps the blue to 255, so the real background is rgb(0,0,255); the
    // derived text must be readable against THAT, matching the clamped #0000ff.
    const vars = chatThemeVars({ '--bg': 'rgb(0,0,500)' })
    expect(vars['--bg']).toBe('rgb(0,0,500)')
    expect(ratio(vars['--text'], '#0000ff')).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
  })

  it('derives the accent hover and subtle so the button does not revert on hover', () => {
    // A light violet: readable as a filled-button label AND as text on the dark
    // page's surfaces, so the chat follows it.
    const vars = chatThemeVars({ '--bg': '#12141a', '--accent': '#a78bfa' })
    expect(vars['--accent']).toBe('#a78bfa')
    expect(vars['--accent-hover']).toBeTruthy()
    expect(vars['--accent-hover']).not.toBe(vars['--accent'])
    expect(vars['--accent-subtle']).toBeTruthy()
  })

  it('pulls an accent that is unreadable as text toward --text until it clears', () => {
    // White bg + white accent: the accent would be a white link on a near-white
    // card. It is not applied as-is, and the app accent is not kept either (it
    // was never measured on this background): a darker step of it is used.
    const vars = chatThemeVars({ '--bg': '#ffffff', '--accent': '#ffffff' })
    expect(vars['--bg']).toBe('#ffffff')
    expect(vars['--accent']).not.toBe('#ffffff')
    for (const s of ['--bg', '--card', '--bg-hover', '--bg-elevated']) {
      expect(ratio(vars['--accent'], vars[s])).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
    }
    expect(ratio(vars['--accent-fg'], vars['--accent'])).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
  })

  it('sets a readable accent for a page that recolours the background without one', () => {
    // Emerald-light's app accent is #047558; a page with that very --bg and no
    // accent would leave links at 1:1 if the app accent stayed in place.
    for (const bg of ['#047558', '#ffffff', '#101418', '#8855ee']) {
      const vars = chatThemeVars({ '--bg': bg })
      expect(vars['--accent']).toBeDefined()
      for (const s of ['--bg', '--card', '--bg-hover', '--bg-elevated']) {
        expect(ratio(vars['--accent'], vars[s])).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
      }
      expect(ratio(vars['--accent-fg'], vars['--accent'])).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
    }
  })

  it('takes its surfaces from the theme gap-fill, not a second ramp', () => {
    // A page background with comfortable contrast is never touched by the AA
    // guard, so every surface and border is exactly the gap-fill's.
    const bg = '#2a1a4d'
    const vars = chatThemeVars({ '--bg': bg })
    const fill = derivedDefaultVars({ '--bg': bg, '--text': vars['--text'] })
    for (const k of ['--card', '--bg-elevated', '--bg-hover', '--panel', '--border', '--muted', '--json-key']) {
      expect(vars[k]).toBe(fill[k])
    }
  })

  it('pins a surface the gap-fill steps below AA back to the background (mid-tone page)', () => {
    // #8855ee carries black-or-white text at only ~4.58:1, so any step toward
    // the text colour drops it under 4.5:1; the guard keeps the text readable.
    const vars = chatThemeVars({ '--bg': '#8855ee' })
    const fill = derivedDefaultVars({ '--bg': '#8855ee', '--text': vars['--text'] })
    expect(ratio(vars['--text'], fill['--card'])).toBeLessThan(CHAT_AA_TEXT)
    expect(vars['--card']).toBe('#8855ee')
  })

  it('applies an accent that clears AA as text on the chat surfaces', () => {
    // A dark accent on a light page reads as text on bg and card.
    const vars = chatThemeVars({ '--bg': '#ffffff', '--accent': '#5b21b6' })
    expect(vars['--accent']).toBe('#5b21b6')
    expect(ratio(vars['--accent'], vars['--bg'])).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
    expect(ratio(vars['--accent'], vars['--card'])).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
  })

  it('re-anchors the composer glass tint on the page background', () => {
    const vars = chatThemeVars({ '--bg': '#2a1a4d' })
    // The glass ladder follows the page bg, so the composer paints from this
    // palette rather than the app theme's fixed light/dark glass.
    expect(vars['--glass-tint']).toContain('42,26,77') // rgb of #2a1a4d
    expect(vars['--glass-tint-thin']).toBeTruthy()
    expect(vars['--glass-tint-ultrathick']).toBeTruthy()
  })
})

describe('the fixed Kiro accent colours yield inside a page-coloured column', () => {
  // Kiro dark paints message links, `.text-accent` and navigate chips a FIXED
  // #b07fff, which ignores the column's `--accent`: on a white page that is
  // 2.87:1. Inside `[data-page-colors]` each must read the local variable.
  const css = readFileSync(join(__dirname, '../../styles/chat-transcript.css'), 'utf8')
  it.each([
    ['.msg-content a', 'var(--accent)'],
    ['.text-accent', 'var(--accent)'],
    [':not(pre)>code[data-chip-action="navigate"]', 'var(--accent)'],
    [':not(pre)>code[data-chip-action="copy"]', 'var(--text)'],
  ])('%s reads %s', (sel, value) => {
    const rule = css.split('\n').find((l) => l.startsWith('[data-theme^="kiro-"] [data-page-colors]') && l.includes(`${sel}{`))
    expect(rule).toBeDefined()
    expect(rule).toContain(`color:${value}`)
  })

  it('keeps the accent hover readable under its label (it moves AWAY from the label)', () => {
    for (const bg of ['#101418', '#f3f4f6', '#1e3a8a', '#515151', '#6a6a6a', '#8855ee', '#cccccc']) {
      for (const accent of ['#f59e0b', '#a78bfa', '#16a34a', '#fde047', '#1e3a8a', '#767676', '#e11d48', '#0ea5e9', '#84cc16', '#ffffff', '#000000']) {
        const vars = chatThemeVars({ '--bg': bg, '--accent': accent })
        expect(ratio(vars['--accent-fg'], vars['--accent'])).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
        expect(ratio(vars['--accent-fg'], vars['--accent-hover'])).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
      }
    }
  })

  it('re-derives status colours (error, warning, success, info) so they clear AA on every chat surface', () => {
    for (const bg of ['#101418', '#f3f4f6', '#1e3a8a', '#fef3c7', '#8855ee', '#515151', '#6a6a6a', '#687030']) {
      const vars = chatThemeVars({ '--bg': bg })
      for (const k of ['--ok', '--warn', '--danger', '--info']) {
        for (const name of k === '--warn' ? [k] : [k, k + '-text']) {
          for (const surface of [vars['--bg'], vars['--card'], vars['--bg-elevated']]) {
            expect(ratio(vars[name], surface), `${name} on ${surface} for bg ${bg}`).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
          }
        }
        expect(ratio(vars[k + '-fg'], vars[k]), `${k}-fg for bg ${bg}`).toBeGreaterThanOrEqual(CHAT_AA_TEXT)
      }
    }
  })
})
