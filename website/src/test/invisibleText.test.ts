/**
 * Invisible-only assistant rows must not draw.
 *
 * Quiet monitor-loop cycles post a bare zero-width space (U+200B) as their
 * say-nothing assistant reply. U+200B is Unicode category Cf — truthy in
 * string guards, invisible on screen — so without a filter each such turn
 * renders as an empty chat bubble. These tests pin the shared predicate and
 * pin the chat page's inline chain to it by source, the same way
 * chatRolesParity.contract.test.ts pins role handling (the renderMessage
 * if-chain has no mountable unit seam).
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

describe('chat page inline chain consults the skip (source contract)', () => {
  // Read the OWNING module per contract, not one page file: the chat page is
  // composed from controllers, so the renderer's skip, the page's render anchor
  // and the regenerate scan each live with the behaviour they belong to.
  const read = (rel: string) => readFileSync(resolve(__dirname, '..', rel), 'utf8')
  const transcript = read('pages/chat/useChatPageTranscriptController.tsx')
  const page = read('pages/ChatPage.tsx')
  const actions = read('pages/chat/useChatPageActionsController.ts')

  it('skips hidden rows before the conversational branch', () => {
    // Since chat-core P5-a the transcript controller dispatches through the
    // app-sdk registry: the skip is a shape entry that draws nothing, ordered
    // before the bubble entry (the last one in the host list).
    expect(transcript).toMatch(/id: 'hidden_invisible_assistant',\s*\n\s*roles: \['\*'\],\s*\n\s*match: isHiddenInvisibleAssistantRow,\s*\n\s*render: \(\) => null/)
    const list = transcript.indexOf('const renderers = mergeRenderers([')
    const skip = transcript.indexOf("id: 'hidden_invisible_assistant'", list)
    const bubble = transcript.indexOf('\n      bubble,\n    ])', list)
    expect(skip).toBeGreaterThan(list)
    expect(bubble).toBeGreaterThan(skip)
  })

  it('passes over hidden rows in the footer-host scan', () => {
    expect(transcript).toMatch(/if \(isHiddenInvisibleAssistantRow\(later\)\) continue/)
  })

  it('skips the py-1 wrapper for a hidden row, in BOTH row hosts', () => {
    // renderMessage returning null still leaves the wrapper its caller drew, so
    // each quiet monitor cycle would stack an empty spacer. The two hosts sit in
    // different modules now, so neither can be inferred from the other.
    expect(transcript).toMatch(/isHiddenInvisibleAssistantRow\(it\.msg\)\) return null/)
    expect(read('pages/chat/ChatPageView.tsx')).toMatch(
      /isHiddenInvisibleAssistantRow\(item\.msg\)\) return null/,
    )
  })

  it('anchors Regenerate/variant affordances on the last DRAWN reply', () => {
    // lastTextIdx must agree with the renderer's skip, or those affordances
    // silently vanish for the rest of a quiet monitor run.
    expect(page).toMatch(
      /role === 'assistant' && !isHiddenInvisibleAssistantRow\(messages\[i\]\)/,
    )
  })

  it('regenerate truncation mirrors the server scan, hidden rows included', () => {
    // chat_regenerate.py picks the turn by the last assistant row BY ROLE;
    // the optimistic truncation must scan identically or the UI truncates
    // after a different user row than the history rewrite persists. Pinned as
    // the scan expression rather than a variable name: what must not drift is
    // that this is a lastIndexOf over ROLES with no skip applied. The binding
    // is part of the pattern so the expression cannot satisfy this from inside
    // a comment after the code itself is deleted.
    expect(actions).toMatch(/const \w+ = messages\.map\(\w+ => \w+\.role\)\.lastIndexOf\('assistant'\)/)
    expect(actions).not.toMatch(/isHiddenInvisibleAssistantRow/)
  })
})
