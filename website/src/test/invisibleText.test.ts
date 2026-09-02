/**
 * Invisible-only assistant rows must not draw.
 *
 * Quiet monitor-loop cycles post a bare zero-width space (U+200B) as their
 * say-nothing assistant reply. U+200B is Unicode category Cf — truthy in
 * string guards, invisible on screen — so without a filter each such turn
 * renders as an empty chat bubble. These tests pin the shared predicate and
 * pin ChatPage's inline chain to it by source, the same way
 * chatRolesParity.contract.test.ts pins role handling (ChatPage has no
 * mountable unit seam for its renderMessage if-chain).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { isInvisibleOnly, isHiddenInvisibleAssistantRow } from '../utils/invisibleText'

describe('isInvisibleOnly', () => {
  it('treats a bare ZWSP as invisible (the quiet monitor-cycle reply)', () => {
    expect(isInvisibleOnly('\u200b')).toBe(true)
  })

  it('covers the whole Cf class plus whitespace, not one codepoint', () => {
    expect(isInvisibleOnly('\u200b\u200c \u200d\u2060\t\ufeff\u00ad')).toBe(true)
    expect(isInvisibleOnly('')).toBe(true)
    expect(isInvisibleOnly('   \n')).toBe(true)
  })

  it('keeps real content, embedded format chars included', () => {
    expect(isInvisibleOnly('a\u200bb')).toBe(false)
    expect(isInvisibleOnly('ok')).toBe(false)
  })
})

describe('isHiddenInvisibleAssistantRow', () => {
  it('hides a ZWSP-only assistant row', () => {
    expect(isHiddenInvisibleAssistantRow({ role: 'assistant', content: '\u200b' })).toBe(true)
    expect(isHiddenInvisibleAssistantRow({ role: 'assistant', content: '' })).toBe(true)
  })

  it('never hides user or streaming rows', () => {
    expect(isHiddenInvisibleAssistantRow({ role: 'user', content: '\u200b' })).toBe(false)
    expect(isHiddenInvisibleAssistantRow({ role: 'streaming', content: '\u200b' })).toBe(false)
  })

  it('keeps a row whose file-change chips are the content', () => {
    expect(
      isHiddenInvisibleAssistantRow({
        role: 'assistant',
        content: '\u200b',
        meta: { file_changes: [{ path: 'a.ts' }] },
      }),
    ).toBe(false)
  })

  it('keeps a row when a regeneration variant holds visible content', () => {
    // Hiding it would strand the variant switcher and make the visible
    // predecessor unreachable.
    expect(
      isHiddenInvisibleAssistantRow({
        role: 'assistant',
        content: '\u200b',
        variants: [{ content: 'the earlier visible reply' }, { content: '\u200b' }],
      }),
    ).toBe(false)
    // All-invisible variants add nothing worth drawing.
    expect(
      isHiddenInvisibleAssistantRow({
        role: 'assistant',
        content: '\u200b',
        variants: [{ content: '\u200b' }],
      }),
    ).toBe(true)
  })

  it('keeps an ordinary reply untouched', () => {
    expect(isHiddenInvisibleAssistantRow({ role: 'assistant', content: 'done café' })).toBe(false)
  })
})

describe('ChatPage inline chain consults the skip (source contract)', () => {
  const src = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')

  it('skips hidden rows before the conversational branch', () => {
    expect(src).toMatch(/if \(isHiddenInvisibleAssistantRow\(m\)\) return null/)
  })

  it('passes over hidden rows in the footer-host scan', () => {
    expect(src).toMatch(/if \(isHiddenInvisibleAssistantRow\(later\)\) continue/)
  })

  it('anchors Regenerate/variant affordances on the last DRAWN reply', () => {
    // lastTextIdx must agree with the renderer's skip, or those affordances
    // silently vanish for the rest of a quiet monitor run.
    expect(src).toMatch(
      /role === 'assistant' && !isHiddenInvisibleAssistantRow\(messages\[i\]\)/,
    )
  })

  it('regenerate truncation mirrors the server scan, hidden rows included', () => {
    // chat_regenerate.py picks the turn by the last assistant row BY ROLE;
    // the optimistic truncation must scan identically or the UI truncates
    // after a different user row than the history rewrite persists.
    expect(src).toMatch(/const aiIdx = messages\.map\(mm => mm\.role\)\.lastIndexOf\('assistant'\)/)
  })
})
