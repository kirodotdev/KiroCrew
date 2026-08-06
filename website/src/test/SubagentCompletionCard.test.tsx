import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import SubagentCompletionCard from '../pages/chat/SubagentCompletionCard'
import {
  isSubagentCompletionMessage,
  parseSubagentCompletion,
} from '../pages/chat/subagentCompletion'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

const SINGLE = [
  '[Subagent completion event]',
  'Agent `53e3e5eb` (kirocrew) completed ✅',
  'Task: Add TWO short UI labels to the GERMAN (de) catalog',
  '',
  'Added both keys and ran the parity check.',
].join('\n')

const WAVE = [
  '[Subagent batch completion event]',
  'Batch results 1/1 — wave finished: 8 ✅ · 1 ❌ · 0 ⏹ of 9 agents. All results delivered.',
  'This run is complete. Finish processing all results before spawning any follow-up sub-agents.',
  'Failures are listed first. Full outputs are on disk — read the result paths on demand; do NOT re-run completed agents.',
  '',
  '— `b8185d65` failed ❌ · Add TWO short UI labels to the SPANISH (es) catalog',
  '  Error: catalog parity check failed',
  '— `53e3e5eb` ✅ Add TWO short UI labels to the GERMAN (de) catalog',
  '  → /home/u/.kiro/crew/subagents/53e3e5eb/result.txt',
].join('\n')

const CHUNK = [
  '[Subagent batch completion event]',
  'Batch results 1/3 — 10 of 30 delivered, 20 still running.',
  'Process these results now, but do NOT spawn new sub-agents yet — more result batches from this run are still arriving, and spawning now will interleave with them.',
  'Failures are listed first. Full outputs are on disk — read the result paths on demand; do NOT re-run completed agents.',
  '',
  '— `53e3e5eb` ✅ Add TWO short UI labels to the GERMAN (de) catalog',
].join('\n')

function msg(content = SINGLE, overrides: Partial<ChatMessage> = {}): ChatMessage {
  return { role: 'subagent', content, cls: '', ...overrides }
}

describe('subagentCompletion parsing/detection', () => {
  it('parses a per-agent completion, keeping the payload and dropping the header', () => {
    const p = parseSubagentCompletion(SINGLE)!
    expect(p.kind).toBe('single')
    if (p.kind !== 'single') return
    expect(p.agentId).toBe('53e3e5eb')
    expect(p.agentName).toBe('kirocrew')
    expect(p.outcome).toBe('ok')
    expect(p.task).toBe('Add TWO short UI labels to the GERMAN (de) catalog')
    expect(p.body).toBe('Added both keys and ran the parity check.')
  })

  it('reads the outcome from the glyph, not the English status word', () => {
    const failed = SINGLE.replace('completed ✅', 'failed ❌')
    const stopped = SINGLE.replace('completed ✅', 'stopped by user ⏹')
    // The delivery-timeout variant carries no status word at all.
    const bare = SINGLE.replace('Agent `53e3e5eb` (kirocrew) completed ✅', 'Agent `53e3e5eb` ❌')
    expect((parseSubagentCompletion(failed) as { outcome: string }).outcome).toBe('failed')
    expect((parseSubagentCompletion(stopped) as { outcome: string }).outcome).toBe('stopped')
    expect((parseSubagentCompletion(bare) as { outcome: string }).outcome).toBe('failed')
  })

  it('parses the restart-recovery and delivery-timeout headers, where the glyph sits MID-line', () => {
    // subagent.py's _notify_orphan / notify_injection_failed put an explanation
    // after the glyph AND run the Task line straight into the payload with NO
    // blank line. Fixtures below are byte-shaped like the real messages: an
    // invented blank separator is what previously let the payload loss pass.
    const shapes: [string, string, string, string][] = [
      [
        'Agent `53e3e5eb` ⚠️ orphaned by gateway restart',
        'Result saved at: `/home/u/.kiro/crew/subagents/53e3e5eb/result.txt`\nUse the read tool to retrieve it.',
        'interrupted',
        'orphaned by gateway restart',
      ],
      [
        'Agent `53e3e5eb` ❌ lost to gateway restart',
        'No result was captured before the restart.',
        'failed',
        'lost to gateway restart',
      ],
      [
        'Agent `53e3e5eb` ❌ delivery timed out',
        'The agent finished but result delivery timed out.',
        'failed',
        'delivery timed out',
      ],
    ]
    for (const [header, payload, outcome, note] of shapes) {
      const content = [
        '[Subagent completion event]',
        header,
        'Task: Add TWO short UI labels to the GERMAN (de) catalog',
        payload,
      ].join('\n')
      const p = parseSubagentCompletion(content)
      expect(p, header).not.toBeNull()
      expect((p as { outcome: string }).outcome, header).toBe(outcome)
      // The words beside the glyph are salvaged from the header line...
      expect((p as { body: string }).body, header).toContain(note)
      // ...and the payload lines survive even though no blank line precedes them.
      // Losing these means an orphan card names no result location at all.
      for (const line of payload.split('\n')) {
        expect((p as { body: string }).body, `${header} :: ${line}`).toContain(line)
      }
    }
  })

  it('does not repeat a status word the chip already renders', () => {
    const p = parseSubagentCompletion(SINGLE) as { body: string }
    expect(p.body).not.toMatch(/^completed/i)
    expect(p.body).toBe('Added both keys and ran the parity check.')
  })

  it('parses a final wave digest with its tallies', () => {
    const p = parseSubagentCompletion(WAVE)!
    expect(p.kind).toBe('batch')
    if (p.kind !== 'batch') return
    expect(p.final).toBe(true)
    expect(p.chunk).toBe(1)
    expect(p.chunks).toBe(1)
    expect(p.ok).toBe(8)
    expect(p.failed).toBe(1)
    expect(p.stopped).toBe(0)
    expect(p.total).toBe(9)
    expect(p.delivered).toBe(9)
    // The spawn-discipline instructions are addressed to the model, not the
    // reader, so they must not survive into the disclosed payload.
    expect(p.body).not.toContain('do NOT re-run')
    expect(p.body).not.toContain('This run is complete')
    expect(p.body).toContain('b8185d65')
  })

  it('parses a mid-wave chunk as progress, not completion', () => {
    const p = parseSubagentCompletion(CHUNK)!
    expect(p.kind).toBe('batch')
    if (p.kind !== 'batch') return
    expect(p.final).toBe(false)
    expect(p.delivered).toBe(10)
    expect(p.total).toBe(30)
    expect(p.running).toBe(20)
    expect(p.body).not.toContain('do NOT spawn new sub-agents')
  })

  it('detects the event under every role the gateway injects it as', () => {
    expect(isSubagentCompletionMessage(msg())).toBe(true)
    expect(isSubagentCompletionMessage(msg(WAVE, { role: 'user' }))).toBe(true)
    expect(isSubagentCompletionMessage(msg(SINGLE, { role: 'assistant' }))).toBe(true)
    expect(isSubagentCompletionMessage(msg(SINGLE, { role: 'tool' }))).toBe(false)
  })

  it('ignores ordinary messages, including one that merely mentions the prefix', () => {
    expect(isSubagentCompletionMessage(msg('hello world', { role: 'user' }))).toBe(false)
    expect(
      isSubagentCompletionMessage(msg('tell me about [Subagent completion event]', { role: 'user' })),
    ).toBe(false)
  })

  it('does NOT detect a prefixed message whose header cannot be parsed (falls back, no data loss)', () => {
    // Callers branch to a card that renders null on a failed parse, so a
    // prefix-only match would make the result disappear from the transcript.
    expect(parseSubagentCompletion('[Subagent completion event]\nmalformed')).toBeNull()
    expect(parseSubagentCompletion('[Subagent batch completion event]\nBatch results ???')).toBeNull()
    expect(isSubagentCompletionMessage(msg('[Subagent completion event]\nmalformed'))).toBe(false)
  })
})

describe('SubagentCompletionCard rendering', () => {
  const store = () => createTestStore({ chat: {} as unknown as ChatState })

  it('renders the task as the headline with the payload folded away', () => {
    renderWithProviders(<SubagentCompletionCard message={msg()} />, { store: store() })
    expect(screen.getByText('Add TWO short UI labels to the GERMAN (de) catalog')).toBeTruthy()
    expect(screen.getByText('Completed')).toBeTruthy()
    expect(screen.getByText('Show details')).toBeTruthy()
    expect(screen.queryByText(/ran the parity check/)).toBeNull()
  })

  it('expands to reveal the payload on toggle', () => {
    renderWithProviders(<SubagentCompletionCard message={msg()} />, { store: store() })
    fireEvent.click(screen.getByText('Show details'))
    expect(screen.getByText('Hide details')).toBeTruthy()
    expect(screen.getByText(/ran the parity check/)).toBeTruthy()
  })

  it('falls back to the agent id when the event carries no task line', () => {
    const noTask = ['[Subagent completion event]', 'Agent `53e3e5eb` completed ✅', '', 'done'].join('\n')
    renderWithProviders(<SubagentCompletionCard message={msg(noTask)} />, { store: store() })
    expect(screen.getByText('Agent 53e3e5eb')).toBeTruthy()
  })

  it('summarises a finished wave with per-outcome tallies', () => {
    renderWithProviders(<SubagentCompletionCard message={msg(WAVE)} />, { store: store() })
    expect(screen.getByText('9 of 9 subagents finished')).toBeTruthy()
    expect(screen.getByTestId('chip-ok').textContent).toContain('8')
    expect(screen.getByTestId('chip-failed').textContent).toContain('1')
    // A zero tally is omitted rather than shown as "0".
    expect(screen.queryByTestId('chip-stopped')).toBeNull()
  })

  it('reports a mid-wave chunk as delivered-so-far, without asserting live state', () => {
    renderWithProviders(<SubagentCompletionCard message={msg(CHUNK)} />, { store: store() })
    expect(screen.getByText('10 of 30 results delivered')).toBeTruthy()
    // A partial delivery gets a muted incomplete glyph, NOT the success tick: a
    // green check on a wave whose siblings are still running reads as "done".
    expect(screen.getByTestId('glyph-partial')).toBeTruthy()
    // And no live count: a card is permanent scrollback, so a "20 running" chip
    // would still claim it months after the wave ended. The ratio carries it.
    expect(screen.queryByTestId('chip-running')).toBeNull()
    // The part label is visible text, not a tooltip-only bare fraction.
    expect(screen.getByText('Part 1 of 3')).toBeTruthy()
    expect(screen.queryByText('1/3')).toBeNull()
  })

  it('gives a finished wave the success tick, not the partial glyph', () => {
    const clean = WAVE
      .replace('16 ✅ · 1 ❌ · 1 ⏹ of 18 agents', '18 ✅ · 0 ❌ · 0 ⏹ of 18 agents')
    renderWithProviders(<SubagentCompletionCard message={msg(clean)} />, { store: store() })
    expect(screen.queryByTestId('glyph-partial')).toBeNull()
  })

  it('omits the part counter for a single-chunk wave', () => {
    renderWithProviders(<SubagentCompletionCard message={msg(WAVE)} />, { store: store() })
    expect(screen.queryByText(/^Part /)).toBeNull()
  })

  it('marks a task the headline had to cut, so the truncation is visible', () => {
    const long = 'x'.repeat(200)
    const longTask = SINGLE.replace('Add TWO short UI labels to the GERMAN (de) catalog', long)
    renderWithProviders(<SubagentCompletionCard message={msg(longTask)} />, { store: store() })
    // CSS truncate cannot cue this: 120 chars usually fit the row.
    expect(screen.getByText(`${'x'.repeat(120)}…`)).toBeTruthy()
  })

  it('leaves a task that fits unmarked', () => {
    renderWithProviders(<SubagentCompletionCard message={msg()} />, { store: store() })
    expect(screen.getByText('Add TWO short UI labels to the GERMAN (de) catalog')).toBeTruthy()
  })

  it('names the outcome of each digest row so it does not depend on an emoji font', () => {
    // WAVE carries a failure, so the card is already expanded.
    renderWithProviders(<SubagentCompletionCard message={msg(WAVE)} />, { store: store() })
    const body = screen.getByTestId('subagent-completion-card').textContent || ''
    // A success row carries no status word from the gateway — only the glyph —
    // so the word is substituted in.
    expect(body).toContain('Completed')
    // A failure row already names its status, so its glyph is simply dropped.
    expect(body).toContain('failed')
    expect(body).not.toContain('✅')
    expect(body).not.toContain('❌')
  })

  it('opens a failure expanded so the reason is not behind a click', () => {
    const failedMsg = msg(SINGLE.replace('completed ✅', 'failed ❌').replace(
      'Added both keys and ran the parity check.',
      'Error: catalog parity check failed',
    ))
    renderWithProviders(<SubagentCompletionCard message={failedMsg} />, { store: store() })
    expect(screen.getByText('Hide details')).toBeTruthy()
    expect(screen.getByText(/catalog parity check failed/)).toBeTruthy()
  })

  it('hands the parsed event to onOpenPanel and omits the button without one', () => {
    const onOpenPanel = vi.fn()
    const { unmount } = renderWithProviders(
      <SubagentCompletionCard message={msg()} onOpenPanel={onOpenPanel} />,
      { store: store() },
    )
    fireEvent.click(screen.getByTitle('Open in the Subagents panel'))
    expect(onOpenPanel).toHaveBeenCalledWith(expect.objectContaining({ agentId: '53e3e5eb' }))
    unmount()
    renderWithProviders(<SubagentCompletionCard message={msg()} />, { store: store() })
    expect(screen.queryByTitle('Open in the Subagents panel')).toBeNull()
  })

  it('renders a restart orphan as a warning, expanded, with where the result landed', () => {
    // No blank line before the payload — exactly as _notify_orphan composes it.
    const orphan = [
      '[Subagent completion event]',
      'Agent `53e3e5eb` ⚠️ orphaned by gateway restart',
      'Task: Add TWO short UI labels to the GERMAN (de) catalog',
      'Result saved at: `/home/u/.kiro/crew/subagents/53e3e5eb/result.txt`',
      'Use the read tool to retrieve it.',
    ].join('\n')
    renderWithProviders(<SubagentCompletionCard message={msg(orphan)} />, { store: store() })
    expect(screen.getByTestId('glyph-interrupted')).toBeTruthy()
    expect(screen.getByText('Interrupted')).toBeTruthy()
    // Opens expanded: the reader's next question is where the result went.
    expect(screen.getByText('Hide details')).toBeTruthy()
    expect(screen.getByText(/orphaned by gateway restart/)).toBeTruthy()
    expect(screen.getByText(/result\.txt/)).toBeTruthy()
  })

  it('renders nothing when the content cannot be parsed', () => {
    const { container } = renderWithProviders(
      <SubagentCompletionCard message={msg('[Subagent completion event]\nmalformed')} />,
      { store: store() },
    )
    expect(container.querySelector('[data-testid="subagent-completion-card"]')).toBeNull()
  })
})
