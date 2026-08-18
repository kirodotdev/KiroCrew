/**
 * Transcript row geometry is the HOST's job, never the row component's.
 *
 * The column gutter (`px-4`), the centring (`mx-auto w-full`) and the width
 * clamp (`maxWidth: var(--mc-content-width, …)`) are applied once, by the row
 * wrapper that the host puts around every rendered message: ChatPage's own row
 * div and `ChatMessageList`'s `ctx.row` / `ctx.wrapper`.
 *
 * Four cards used to apply that geometry a SECOND time at their own root. In
 * ChatPage that nested one clamp inside another and inset the card by an extra
 * full gutter, so it sat 20px right of every sibling row and rendered 40px
 * narrower. The registries compensated by deliberately NOT wrapping those four,
 * which left the same cards flush at 0px in ChatPane and ChatEmbed, where
 * nothing else supplies a gutter.
 *
 * Both halves of that arrangement are pinned here, because fixing only one half
 * moves the defect rather than removing it:
 *   1. no row component re-applies the geometry at its root, and
 *   2. every registry entry for those cards routes through `ctx.row`.
 *
 * Layout itself is not asserted — jsdom computes none. These are class-list and
 * source-contract assertions, which is what can actually hold in CI; the visual
 * evidence lives in `capture/transcript-row-geometry.tsx`.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { renderWithProviders } from './helpers'
import WorkflowRunCard from '../pages/chat/WorkflowRunCard'
import SubagentRunCard from '../pages/chat/SubagentRunCard'
import WorkflowCompletionCard from '../pages/chat/WorkflowCompletionCard'
import SubagentCompletionCard from '../pages/chat/SubagentCompletionCard'
import type { ChatMessage } from '../types'

const GUTTER = 'px-4'
const CENTRE = 'mx-auto'
const CLAMP = '--mc-content-width'

const msg = (role: string, content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', meta })

const WF_COMPLETION = [
  '[Workflow completion event]',
  'Workflow `pizza-origins` (wf_1) → **finished**',
  '',
  '### Result',
  'Naples, 18th century.',
].join('\n')

const SA_COMPLETION = [
  '[Subagent completion event]',
  'Agent `53e3e5eb` (kirocrew) completed ✅',
  'Task: Audit the transcript cards',
  '',
  'Reported per-component props.',
].join('\n')

/** The rendered root of each card, i.e. what a host row wrapper receives. */
const CARDS: [string, () => React.ReactElement][] = [
  ['WorkflowRunCard', () => (
    <WorkflowRunCard runId="wf_1" message={msg('tool', '🔧 workflow_run', { input: '{"intent":"x"}' })} />
  )],
  ['SubagentRunCard', () => (
    <SubagentRunCard slot="main" launch={{ ids: ['a1'], announced: 1 }} />
  )],
  ['WorkflowCompletionCard', () => (
    <WorkflowCompletionCard message={msg('assistant', WF_COMPLETION)} />
  )],
  ['SubagentCompletionCard', () => (
    <SubagentCompletionCard message={msg('subagent', SA_COMPLETION)} />
  )],
]

describe('transcript row geometry belongs to the host', () => {
  it.each(CARDS)('%s does not apply the column geometry at its root', (_name, mount) => {
    const { container } = renderWithProviders(mount())
    const root = container.firstElementChild as HTMLElement
    expect(root).toBeTruthy()

    // A gutter here would stack on top of the host row wrapper's own.
    expect(root.classList.contains(GUTTER)).toBe(false)
    expect(root.classList.contains(CENTRE)).toBe(false)
    // A second clamp nested inside the host's clamp silently shrinks the card.
    expect(root.style.maxWidth ?? '').not.toContain(CLAMP)
  })

  /**
   * The other half of the contract. Read as source rather than rendered because
   * what must hold is that the REGISTRY ENTRY delegates to `ctx.row` — a
   * rendered tree cannot distinguish a wrapper the entry asked for from one an
   * ancestor happened to supply, and it is precisely the entry that regressed.
   */
  it.each([
    ['pages/chat/transcriptRenderers.tsx', [
      'WorkflowRunCard', 'SubagentRunCard', 'WorkflowCompletionCard', 'SubagentCompletionCard',
    ]],
    ['app-sdk/messageRenderers.tsx', ['SubagentCompletionCard']],
  ])('%s wraps every self-laying card in ctx.row', (rel, cards) => {
    const src = readFileSync(join(__dirname, '..', rel), 'utf8')
    for (const card of cards as string[]) {
      const at = src.indexOf(`<${card}`)
      expect(at, `${card} is not rendered in ${rel}`).toBeGreaterThan(-1)
      // `ctx.row(` must open within the same render callback, i.e. between the
      // preceding `render:` and the card element itself.
      const renderAt = src.lastIndexOf('render:', at)
      expect(renderAt, `no render callback precedes <${card}> in ${rel}`).toBeGreaterThan(-1)
      const head = src.slice(renderAt, at)
      expect(
        head.includes('ctx.row('),
        `<${card}> in ${rel} is returned without ctx.row, so no host supplies its gutter`,
      ).toBe(true)
    }
  })
})
