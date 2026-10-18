import { describe, it, expect } from 'vitest'
import { readIndexCss } from './indexCssSource'

/**
 * CLI mode draws the crewmate chat's accent user bubble neutral, like every
 * other user bubble. cli-mode.css paints a chat-pane user bubble with
 * `background: var(--bg-elevated) !important; color: var(--text) !important`,
 * and index.css redefines exactly those tokens on the accent bubble itself
 * (a 15%-black patch, `--accent-fg` text). Without a guard the CLI rule reads
 * the redefined values and draws dark text on a dark box. So every index.css
 * rule that starts with the accent hook carries the zero-specificity
 * `:where(:root:not([data-ui="cli"]))` guard. The `--bubble-accent` snapshot
 * on the bubble's parent redefines nothing the CLI rule reads, so it is left
 * out here; UserMessage.accentTone.test.tsx pins it.
 *
 * happy-dom cannot compute this cascade (an attribute scope, `:where()` and
 * `!important` across two sheets), so the guard is pinned at the source level.
 */
const HOOK = '.msg-content.user-bubble.user-bubble-accent'
const GUARD = ':where(:root:not([data-ui="cli"]))'
const SNAPSHOT = `:has(>${HOOK})`

const css = readIndexCss()

/** Split a selector list on its top-level commas (not the ones inside `:has(…)` or `[…]`). */
function splitSelectorList(list: string): string[] {
  const parts: string[] = []
  let depth = 0
  let current = ''
  for (const ch of list) {
    if (ch === '(' || ch === '[') depth++
    if (ch === ')' || ch === ']') depth--
    if (ch === ',' && depth === 0) {
      parts.push(current.trim())
      current = ''
    } else {
      current += ch
    }
  }
  parts.push(current.trim())
  return parts.filter(Boolean)
}

/** Every style rule in index.css whose selector names the accent hook: one entry per selector. */
function hookRules(): { selector: string; body: string }[] {
  const source = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const rules: { selector: string; body: string }[] = []
  for (const m of source.matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
    // Text after the last `;` drops any declarations a nested rule follows.
    const prelude = m[1].split(';').pop() ?? ''
    for (const selector of splitSelectorList(prelude)) {
      if (selector.includes('user-bubble-accent')) rules.push({ selector, body: m[2].trim() })
    }
  }
  return rules
}

describe('index.css keeps the accent user bubble out of CLI mode', () => {
  const rules = hookRules()

  it('guards every rule that starts with the accent hook', () => {
    const guarded = rules.filter(r => r.selector.startsWith(GUARD)).map(r => r.selector.slice(GUARD.length).trim())
    // The token redefinition, and the inline code, link, link pill, pill label and `.text-accent` rules.
    expect(guarded).toEqual(expect.arrayContaining([
      HOOK,
      `${HOOK} :not(pre)>code`,
      `${HOOK} a`,
      `${HOOK} span.group:has(>a)`,
      `${HOOK} span.group>a.no-underline`,
      `${HOOK} .text-accent`,
    ]))
    const unguarded = rules.filter(r => !r.selector.startsWith(GUARD) && r.selector !== SNAPSHOT).map(r => r.selector)
    expect(unguarded, 'an accent-bubble rule without the guard repaints the CLI user bubble').toEqual([])
  })

  it('keeps the two tokens the CLI rule paints with behind the guard', () => {
    const block = rules.find(r => r.selector === `${GUARD} ${HOOK}`)
    expect(block?.body).toMatch(/--bg-elevated:rgba\(0,0,0,\.15\)/)
    expect(block?.body).toMatch(/--text:var\(--accent-fg\)/)
  })
})
