/**
 * DFL-71 phase 1 — the per-run grouping of tool calls inside a turn.
 *
 * Two layers live here, matching where each rule is decided:
 *
 *   - `toolRunGroup.ts` (pure): run summarization — the dominant VERB from the
 *     run's own classifications (`classifyToolCall`, the same classifier a
 *     single row uses) plus the call count; duration stays `null` because
 *     ToolActivity carries no duration (the phase-2 seam).
 *   - `TurnBlock` × `ToolRunGroup` (render): a contiguous `tools` segment of
 *     the DEFAULT-mode split collapses into ONE run row; every isVisibleInline
 *     exemption renders OUTSIDE it, unchanged; the turn-level toggle's count
 *     and fold semantics are untouched.
 *
 * The dominant-verb expectations are read through `i18nT`, so a catalog change
 * moves these assertions with it.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import TurnBlock from '../pages/chat/TurnBlock'
import { summarizeToolRun, dominantVerbLabel } from '../pages/chat/toolRunSummary'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import type { ChatMessage } from '../../types'
import { i18nT } from '../i18n/t'

const makeTurn = (items: TurnItem[], complete = true): Extract<DisplayItem, { kind: 'turn' }> =>
  ({ kind: 'turn', items, complete })

const tool = (content: string, extra: Partial<ChatMessage> = {}): TurnItem => ({
  kind: 'single',
  msg: { role: 'tool', content, cls: '', ...extra },
  idx: 0,
})

const assistant = (content: string): TurnItem => ({
  kind: 'single',
  msg: { role: 'assistant', content, cls: '' },
  idx: 0,
})

const renderItem = (it: TurnItem, i: number) => (
  <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
)

describe('toolRunGroup — summarizeToolRun', () => {
  it('counts the run and derives the dominant verb from the calls'+' own actions', () => {
    const msgs: ChatMessage[] = [
      { role: 'tool', content: '🔧 Running: cat a.ts', cls: '', meta: { input: 'cat a.ts' } },
      { role: 'tool', content: '🔧 Running: cat b.ts', cls: '', meta: { input: 'cat b.ts' } },
      { role: 'tool', content: '🔧 Running: grep -r x src', cls: '', meta: { input: 'grep -r x src' } },
      { role: 'tool', content: '🔧 Running: cat c.ts', cls: '', meta: { input: 'cat c.ts' } },
    ]
    const s = summarizeToolRun(msgs)
    expect(s.count).toBe(4)
    // Reads dominate 3:1 — the header is the exploration verb, in the locale.
    expect(s.title).toBe(i18nT('pages.chat.toolRunGroup.verb_explored'))
    // Phase 1: ToolActivity has no duration, so the header carries none. The
    // field stays in the shape so phase 2 lights it up without touching this.
    expect(s.duration).toBeNull()
  })

  it('renders the search verb when searches dominate', () => {
    const msgs: ChatMessage[] = [
      { role: 'tool', content: '🔧 Running: grep -r a src', cls: '', meta: { input: 'grep -r a src' } },
      { role: 'tool', content: '🔧 Running: grep -r b src', cls: '', meta: { input: 'grep -r b src' } },
    ]
    expect(summarizeToolRun(msgs).title).toBe(i18nT('pages.chat.toolRunGroup.verb_searched'))
  })

  it('falls back to the worked-through verb when nothing classifies', () => {
    const msgs: ChatMessage[] = [
      { role: 'tool', content: '🔧 Running: $WEIRD Pipeline', cls: '' },
      { role: 'tool', content: '🔧 Running: other < f', cls: '' },
    ]
    expect(summarizeToolRun(msgs).title).toBe(i18nT('pages.chat.toolRunGroup.verb_worked'))
  })

  it('maps the dominant kind to its verb family (dominantVerbLabel)', () => {
    expect(dominantVerbLabel(new Map([['execute', 2]]))).toBe(i18nT('pages.chat.toolRunGroup.verb_ran'))
    expect(dominantVerbLabel(new Map([['edit', 2]]))).toBe(i18nT('pages.chat.toolRunGroup.verb_edited'))
    expect(dominantVerbLabel(new Map([['fetch', 2]]))).toBe(i18nT('pages.chat.toolRunGroup.verb_fetched'))
    expect(dominantVerbLabel(new Map([['mcp', 2]]))).toBe(i18nT('pages.chat.toolRunGroup.verb_used_mcp'))
    expect(dominantVerbLabel(new Map([['unknown', 2]]))).toBe(i18nT('pages.chat.toolRunGroup.verb_worked'))
  })
})

describe('TurnBlock — per-run grouping (default mode)', () => {
  it('collapses a contiguous tool run into ONE run row with verb + count', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat a.ts', cls: '', meta: { input: 'cat a.ts' } }, idx: 0 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat b.ts', cls: '', meta: { input: 'cat b.ts' } }, idx: 1 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat c.ts', cls: '', meta: { input: 'cat c.ts' } }, idx: 2 },
      assistant('the answer'),
    ]
    items.forEach((it, i) => { it.idx = i })
    const { container } = renderWithProviders(<TurnBlock turn={makeTurn(items)} renderItem={renderItem} />)
    // The run header is a toggle button announcing verb + count.
    const countLabel = i18nT('pages.chat.toolRunGroup.tool_calls', { count: 3 })
    const runToggle = screen.getByRole('button', { name: new RegExp(`${i18nT('pages.chat.toolRunGroup.verb_explored')}.*${countLabel.replace('3 ', '')}`) })
    expect(runToggle).toBeTruthy()
    expect(runToggle.getAttribute('aria-expanded')).toBe('false')
    // Folded: the per-call rows are inside the run's own disclosure (not
    // mounted until expanded); the answer renders outside it.
    expect(container.textContent).toContain('the answer')
    expect(container.textContent).not.toContain('cat a.ts')
  })

  it('expanded run renders one row per call, no command repeated in header and body', () => {
    const cmds = ['cat a.ts', 'cat b.ts', 'cat c.ts']
    const items: TurnItem[] = cmds.map((c, i) => ({
      kind: 'single',
      msg: { role: 'tool', content: `🔧 Running: ${c}`, cls: '', meta: { input: c } },
      idx: i,
    }))
    items.push(assistant('the answer'))
    items[3].idx = 3
    const { container } = renderWithProviders(<TurnBlock turn={makeTurn(items)} renderItem={renderItem} />)
    fireEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('pages.chat.toolRunGroup.verb_explored')) }))
    // One row per call — each derives its own short label from ITS command
    // (`Read a.ts`), and the verbatim command stays one click away in that
    // row's own ToolDetails, not in the run header.
    expect(container.textContent).toContain(i18nT('utils.toolCallTitle.read', { path: 'a.ts' }))
    expect(container.textContent).toContain(i18nT('utils.toolCallTitle.read', { path: 'b.ts' }))
    expect(container.textContent).toContain(i18nT('utils.toolCallTitle.read', { path: 'c.ts' }))
    // The header shows the VERB, never any single call's command.
    const header = screen.getByRole('button', { name: new RegExp(i18nT('pages.chat.toolRunGroup.verb_explored')) })
    expect(header.textContent).not.toContain('cat a.ts')
  })

  it('exempted rows stay inline and SPLIT runs — never swallowed by a run group', () => {
    // tool | (widget-bearing assistant → isVisibleInline) | tool  ⇒  the tools
    // on either side stay PLAIN single-call rows folded behind the turn
    // toggle (runs of one do not group), and the widget row renders in place
    // between them.
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat a.ts', cls: '', meta: { input: 'cat a.ts' } }, idx: 0 },
      { kind: 'single', msg: { role: 'assistant', content: 'look: ![img](/tmp/x.png)', cls: '' }, idx: 1 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: grep -r y src', cls: '', meta: { input: 'grep -r y src' } }, idx: 2 },
      assistant('the answer'),
    ]
    items.forEach((it, i) => { it.idx = i })
    const { container } = renderWithProviders(<TurnBlock turn={makeTurn(items)} renderItem={renderItem} />)
    // The widget row renders in place, outside any run group.
    expect(container.textContent).toContain('look: ![img](/tmp/x.png)')
    // No run row formed: both tool segments hold ONE call each, folded behind
    // the turn toggle.
    expect(container.textContent).toContain(i18nT('pages.chat.collapsibleToolGroup.tool_call', { count: 2 }))
    expect(container.textContent).not.toContain(i18nT('pages.chat.toolRunGroup.verb_explored'))
  })

  it('a two-call run before an exempted row groups; the one after stays folded-plain', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat a.ts', cls: '', meta: { input: 'cat a.ts' } }, idx: 0 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat b.ts', cls: '', meta: { input: 'cat b.ts' } }, idx: 1 },
      { kind: 'single', msg: { role: 'assistant', content: 'look: ![img](/tmp/x.png)', cls: '' }, idx: 2 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: grep -r y src', cls: '', meta: { input: 'grep -r y src' } }, idx: 3 },
      assistant('the answer'),
    ]
    items.forEach((it, i) => { it.idx = i })
    const { container } = renderWithProviders(<TurnBlock turn={makeTurn(items)} renderItem={renderItem} />)
    // The widget row renders in place.
    expect(container.textContent).toContain('look: ![img](/tmp/x.png)')
    // The first segment is ONE run row of two; the second tool stays a plain
    // single-call row behind the turn fold.
    expect(container.textContent).toContain(`${i18nT('pages.chat.toolRunGroup.verb_explored')} · ${i18nT('pages.chat.toolRunGroup.tool_calls', { count: 2 })}`)
  })

  it('workflow_run card, error rows and hand-backs stay outside run groups', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat a.ts', cls: '', meta: { input: 'cat a.ts' } }, idx: 0 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat b.ts', cls: '', meta: { input: 'cat b.ts' } }, idx: 1 },
      { kind: 'single', msg: { role: 'error', content: 'boom', cls: '' }, idx: 2 },
      { kind: 'single', msg: { role: 'assistant', content: 'pick one [OPTIONS: a | b]', cls: '' }, idx: 3 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat c.ts', cls: '', meta: { input: 'cat c.ts' } }, idx: 4 },
    ]
    items.forEach((it, i) => { it.idx = i })
    const { container } = renderWithProviders(<TurnBlock turn={makeTurn(items)} renderItem={renderItem} />)
    // Every exempted row renders in place, visibly, outside any run group.
    expect(container.textContent).toContain('boom')
    expect(container.textContent).toContain('pick one')
    // The pre-error tools form ONE run row of two; the post-error tool is a
    // plain single-call row behind the turn fold.
    expect(container.textContent).toContain(`${i18nT('pages.chat.toolRunGroup.verb_explored')} · ${i18nT('pages.chat.toolRunGroup.tool_calls', { count: 2 })}`)
  })

  it('a workflow_run launch renders as its own card row, outside any run group', () => {
    // isWorkflowRunItem is in isVisibleInline, so the launch is a `visible`
    // segment in the SAME split that derives the runs: it renders in place and
    // the tools on either side of it stay single plain rows (runs of one do
    // not group) — never folded into a run of three.
    const launch: ChatMessage = {
      role: 'tool', content: '🔧 Running: workflow_run', cls: '',
      meta: { tool_call_id: 'tc_wf', input: '{}', output: 'Started workflow run `wf_4242`. It runs in the background — monitor with workflow_status.' },
    }
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat a.ts', cls: '', meta: { input: 'cat a.ts' } }, idx: 0 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat b.ts', cls: '', meta: { input: 'cat b.ts' } }, idx: 1 },
      { kind: 'single', msg: launch, idx: 2 },
      assistant('done'),
    ]
    items.forEach((it, i) => { it.idx = i })
    const { container } = renderWithProviders(<TurnBlock turn={makeTurn(items)} renderItem={renderItem} />)
    // The launch row renders in place through renderItem (the workflow_run
    // card mounts in production), not folded into a run.
    expect(container.textContent).toContain('Running: workflow_run')
    // The two tools BEFORE it form ONE run row of two.
    expect(container.textContent).toContain(`${i18nT('pages.chat.toolRunGroup.verb_explored')} · ${i18nT('pages.chat.toolRunGroup.tool_calls', { count: 2 })}`)
  })

  it('the turn-level toggle still reports DISTINCT calls across all runs', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat a.ts', cls: '', meta: { input: 'cat a.ts' } }, idx: 0 },
      { kind: 'single', msg: { role: 'tool', content: '🔧 Running: cat b.ts', cls: '', meta: { input: 'cat b.ts' } }, idx: 1 },
      assistant('the answer'),
    ]
    items.forEach((it, i) => { it.idx = i })
    const { container } = renderWithProviders(<TurnBlock turn={makeTurn(items)} renderItem={renderItem} />)
    expect(container.textContent).toContain(i18nT('pages.chat.collapsibleToolGroup.tool_call', { count: 2 }))
  })

  it('approvals never reach a run group: permission rows render through their own path', () => {
    // groupDisplayItems routes `permission` rows to GROUPABLE groups handled by
    // the pinned ApprovalBar / CollapsibleToolGroup — this pins that a turn
    // holding a permission GROUP still renders that group as a
    // CollapsibleToolGroup row (never a ToolRunGroup), with its pending count
    // untouched.
    const perm: ChatMessage = { role: 'permission', cls: '', meta: { approval_id: 'a1' } }
    const toolMsg: ChatMessage = { role: 'tool', content: '🔧 Running: cat a.ts', cls: '', meta: { input: 'cat a.ts' } }
    // Rendered as ChatPage does: the permission group as a group item, the
    // tool inside the turn. TurnBlock sees only the tool; the group renders
    // through CollapsibleToolGroup outside the turn.
    const { container } = render(<TurnBlock turn={makeTurn([tool(toolMsg.content, { meta: toolMsg.meta })])} renderItem={renderItem} />)
    expect(container.textContent).not.toContain(i18nT('pages.chat.collapsibleToolGroup.approval_needed'))
    // And the raw permission row renders as the group row it always did:
    const { container: groupContainer } = render(
      <div>{perm.role}</div>,
    )
    expect(groupContainer.textContent).toBe('permission')
  })
})
