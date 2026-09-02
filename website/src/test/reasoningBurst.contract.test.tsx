/**
 * Gate→fold contract for the shared reasoning-burst predicate (#6406).
 *
 * groupDisplayItems' wrap gate (contentThinkingCount) routes multi-burst
 * reasoning batches into a {kind:'turn'} wrapper for the ONE purpose of
 * feeding TurnBlock's mergeTurnThinking fold. The gate, the fold, ChatPage's
 * renderMessage, and the shared-transcript registry entry all derive from a
 * single definition (REASONING_ROLES / isReasoningRole / hasReasoningContent /
 * isReasoningBurst). Two layers pin the contract:
 *
 * 1. Behavioral: grouping output rendered straight through TurnBlock must show
 *    exactly one thinking row, so a SEMANTICALLY divergent fork (the gate
 *    wrapping batches the fold no longer merges, regrowing the duplicate
 *    "Thought process" rows of #6376) fails here rather than passing each
 *    side's isolated tests.
 * 2. Structural: a re-inlined copy of the condition anywhere in the display
 *    layer would keep the behavioral tests green today and only diverge later,
 *    so a source scan asserts no chat-surface file re-spells the role
 *    predicate. (The scan matches the literal `role === 'thinking'` spellings;
 *    an exotic re-spelling can evade any regex — the honest floor is that the
 *    common forms fail loudly. ChatPage's real renderMessage path is covered
 *    by ChatPageCoverage.test.tsx.)
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { readFile, readdir } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import TurnBlock from '../pages/chat/TurnBlock'
import {
  groupDisplayItems,
  isReasoningBurst,
  isReasoningRole,
  hasReasoningContent,
  REASONING_ROLES,
} from '../pages/chat/groupDisplayItems'
import type { ChatMessage } from '../types'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

const PAGES_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'pages')
const CHAT_DIR = join(PAGES_DIR, 'chat')

const msg = (role: string, content = ''): ChatMessage =>
  ({ role, content, cls: '' } as ChatMessage)

// Content-bearing bursts render like ChatPage's renderMessage (same shared
// predicate). Empty placeholders — which every real surface renders as
// NOTHING — get a visible marker here on purpose: it lets the assertions
// observe TurnBlock's post-fold item stream (that the fold left the
// placeholder in place rather than deleting or hoisting it), which a null
// render would hide.
const renderItem = (it: TurnItem, i: number) => {
  if (it.kind !== 'single') return <div data-testid={`row-${i}`} data-role="group">group</div>
  const role = isReasoningRole(it.msg) && !hasReasoningContent(it.msg)
    ? 'thinking-placeholder'
    : it.msg.role
  return (
    <div data-testid={`row-${i}`} data-role={role}>
      {it.msg.content}
    </div>
  )
}

const findTurn = (turns: DisplayItem[]): Extract<DisplayItem, { kind: 'turn' }> => {
  const turn = turns.find((t): t is Extract<DisplayItem, { kind: 'turn' }> => t.kind === 'turn')
  expect(turn).toBeDefined()
  return turn!
}

describe('reasoning-burst gate→fold contract (#6406)', () => {
  it('a reasoning-only multi-burst batch renders exactly ONE thinking row through TurnBlock', () => {
    // The exact #6376 shape: a trailing turn that has only emitted reasoning.
    // The gate must wrap it, and the fold the gate feeds must merge it — the
    // contract holds only when both sides agree on what a burst is.
    const { turns } = groupDisplayItems([
      msg('thinking', 'burst 1'),
      msg('thinking', 'burst 2'),
      msg('thinking', 'burst 3'),
    ])
    const turn = findTurn(turns)
    const { container } = render(<TurnBlock turn={turn} renderItem={renderItem} />)
    const thinkingRows = container.querySelectorAll('[data-role="thinking"]')
    expect(thinkingRows).toHaveLength(1)
    // The fold concatenates burst content, so the one row carries all of it.
    expect(thinkingRows[0].textContent).toContain('burst 1')
    expect(thinkingRows[0].textContent).toContain('burst 3')
  })

  it('empty placeholder bursts neither trip the gate nor count as bursts', () => {
    // One real burst + empties: the gate must NOT wrap (nothing to dedup). If
    // a future predicate fork made the gate count empties while the fold
    // ignores them, this batch would wrap despite having one real burst.
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      msg('thinking', 'one real'),
      msg('thinking', ''),
      msg('thinking', ''),
    ])
    expect(turns.some(t => t.kind === 'turn')).toBe(false)
  })

  it('mixed batch: real bursts fold to one hoisted row, the empty placeholder stays in place below it', () => {
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      msg('thinking', 'plan A'),
      msg('tool', 'tool step'),
      msg('thinking', ''),
      msg('thinking', 'plan B'),
      msg('assistant', 'answer'),
    ])
    const turn = findTurn(turns)
    const { container } = render(<TurnBlock turn={turn} renderItem={renderItem} />)
    // Exactly one merged (content-bearing) thinking row survives the fold…
    expect(container.querySelectorAll('[data-role="thinking"]')).toHaveLength(1)
    // …and the empty placeholder survives IN the rendered stream (the fold
    // leaves it in place, it is neither merged nor dropped), positioned after
    // the hoisted merged row. Asserted on the rendered output, not on the
    // pre-fold turn.items, so a fold that deleted or moved it fails here.
    const rows = [...container.querySelectorAll('[data-role]')].map(el => el.getAttribute('data-role'))
    expect(rows.filter(r => r === 'thinking-placeholder')).toHaveLength(1)
    expect(rows.indexOf('thinking')).toBeLessThan(rows.indexOf('thinking-placeholder'))
    // The answer is still rendered (the fold is a hoist, not a filter).
    expect(screen.getByText('answer')).toBeInTheDocument()
  })

  it('the shared predicates classify the enumerated shapes', () => {
    // Direct predicate pins: content-bearing thinking counts (whitespace-only
    // is content-bearing today — refining that is legal, but it must happen in
    // the shared predicate so gate, fold, and renderers move together), empty
    // and non-thinking shapes do not.
    const single = (role: string, content: string): TurnItem =>
      ({ kind: 'single', msg: msg(role, content), idx: 0 })
    expect(isReasoningBurst(single('thinking', 'text'))).toBe(true)
    expect(isReasoningBurst(single('thinking', '  '))).toBe(true)
    expect(isReasoningBurst(single('thinking', ''))).toBe(false)
    expect(isReasoningBurst(single('assistant', 'text'))).toBe(false)
    expect(isReasoningBurst({ kind: 'group', msgs: [msg('thinking', 'text')], startIdx: 0 })).toBe(false)
    expect(hasReasoningContent(msg('thinking', 'text'))).toBe(true)
    expect(hasReasoningContent(msg('thinking', ''))).toBe(false)
    expect(isReasoningRole(msg('thinking', ''))).toBe(true)
    expect(isReasoningRole(msg('assistant', 'x'))).toBe(false)
    expect(REASONING_ROLES).toContain('thinking')
  })

  it('no display-layer file re-spells the reasoning-role predicate (structural pin)', async () => {
    // A byte-identical re-inline in any consumer would keep every behavioral
    // test above green and only drift later — the regression #6406 exists to
    // prevent. Scan the directories where the chat transcript is rendered
    // (pages/chat/ and app-sdk/ recursively, plus ChatPage.tsx — classification
    // CONSUMERS elsewhere, e.g. utils/pinnedPrompt.ts, import from the shared
    // module and are covered by their own contracts) for the predicate's
    // common spellings — === and !== comparisons and hardcoded role-list
    // membership; the single definition site is the REASONING_ROLES list,
    // which is not spelled as a role comparison at all. (chatSlice's
    // burst-lifecycle checks are store mechanics, not display classification,
    // and live outside this scope. An exotic re-spelling can evade any regex —
    // the honest floor is that the common forms fail loudly.)
    const respell = /\brole\s*[!=]==\s*['"]thinking['"]|roles:\s*\[\s*['"]thinking['"]/
    const walk = async (dir: string): Promise<string[]> => {
      const out: string[] = []
      for (const e of await readdir(dir, { withFileTypes: true })) {
        const p = join(dir, e.name)
        if (e.isDirectory()) {
          if (e.name === '__mocks__' || e.name === '__fixtures__') continue
          out.push(...await walk(p))
        } else if (/\.tsx?$/.test(e.name) && !/\.(test|spec)\./.test(e.name)) {
          out.push(p)
        }
      }
      return out
    }
    const files = [
      ...await walk(CHAT_DIR),
      ...await walk(join(PAGES_DIR, '..', 'app-sdk')),
      join(PAGES_DIR, 'ChatPage.tsx'),
    ]
    expect(files.length).toBeGreaterThan(10) // the walk really found the display layer
    for (const file of files) {
      const src = await readFile(file, 'utf8')
      expect(
        src.match(respell),
        `${file} re-spells the reasoning predicate — use isReasoningRole/hasReasoningContent/REASONING_ROLES from groupDisplayItems`,
      ).toBeNull()
    }
    // The consumers import the shared forms instead, and the definition site
    // exists (its MEMBERS are deliberately not pinned — growing the list is
    // the exact change the shared definition exists to make safe).
    const turnBlock = await readFile(join(CHAT_DIR, 'TurnBlock.tsx'), 'utf8')
    expect(turnBlock).toMatch(/import \{ isReasoningBurst \} from '\.\/groupDisplayItems'/)
    const groupSrc = await readFile(join(CHAT_DIR, 'groupDisplayItems.ts'), 'utf8')
    expect(groupSrc).toMatch(/export const REASONING_ROLES = \[/)
  })
})
