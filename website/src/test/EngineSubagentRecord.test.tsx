import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import AssistantMessage from '../pages/chat/AssistantMessage'
import { engineRecordOf } from '../pages/chat/EngineSubagentRecord'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))

/**
 * The reply row of a turn that ran KAS engine sub-agents carries
 * `meta.engine_subagents` (`dashboard/engine_subagent_record.py`), so a reopened
 * chat can say which children ran: each one's agent, task, how it ended and its
 * tool-call titles, as the engine reported them. A reply without the key draws
 * exactly what it drew.
 */
const RECORD = {
  children: [
    { agent: 'kirocrew-helper', task: 'Run sleep 45; echo one', end: 'completed', tools: ['Run sleep then echo'], tool_count: 1 },
    { agent: 'kirocrew-helper', task: 'Run sleep 60; echo two', end: 'failed', reason: 'Sub-agent execution was cancelled', tools: [], tool_count: 0 },
    { agent: 'kirocrew-helper', task: 'ls /x', end: 'not_reported', tools: ['b', 'c'], tool_count: 5 },
  ],
}
const BASE = { content: 'All three ran.', isStreaming: false, slotRunning: false, messageTs: '2026-11-22T12:00:00Z' }

describe('engine sub-agent record under a reply', () => {
  it('draws nothing for a reply without the record', () => {
    render(<AssistantMessage {...BASE} />)
    expect(screen.queryByTestId('engine-subagent-record')).toBeNull()
  })

  it('draws nothing for a malformed record', () => {
    for (const bad of [null, 'x', [], { children: 'x' }, { children: [] }, { children: [7] }]) {
      expect(engineRecordOf(bad)).toBeNull()
    }
    render(<AssistantMessage {...BASE} engineSubagents={{ children: 'nope' }} />)
    expect(screen.queryByTestId('engine-subagent-record')).toBeNull()
  })

  it('is closed by default and names how many ran', () => {
    render(<AssistantMessage {...BASE} engineSubagents={RECORD} />)
    const toggle = screen.getByTestId('engine-subagent-record-toggle')
    expect(toggle).toHaveTextContent('Sub-agents in this turn: 3')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('engine-record-child')).toBeNull()
  })

  it('opens to each child: end, task, reason and tool titles, as reported', () => {
    render(<AssistantMessage {...BASE} engineSubagents={RECORD} />)
    fireEvent.click(screen.getByTestId('engine-subagent-record-toggle'))
    expect(screen.getByTestId('engine-subagent-record-toggle')).toHaveAttribute('aria-expanded', 'true')
    const rows = screen.getAllByTestId('engine-record-child')
    expect(rows.map(r => r.getAttribute('data-end'))).toEqual(['completed', 'failed', 'not_reported'])
    expect(rows[0]).toHaveTextContent('Completed')
    expect(rows[0]).toHaveTextContent('Run sleep 45; echo one')
    expect(rows[0]).toHaveTextContent('Run sleep then echo')
    expect(rows[1]).toHaveTextContent('Failed')
    expect(rows[1]).toHaveTextContent('Sub-agent execution was cancelled')
    expect(rows[2]).toHaveTextContent('End not reported by the engine')
    // Five calls reported, two titles kept: the three earlier ones are counted, not invented.
    expect(rows[2]).toHaveTextContent('3 earlier calls not shown')
  })

  it('draws a failed child\'s reason as the shared error notice, with no hand-off', () => {
    render(<AssistantMessage {...BASE} engineSubagents={RECORD} />)
    fireEvent.click(screen.getByTestId('engine-subagent-record-toggle'))
    // One failed child with a reason: one notice, and it is the error surface.
    const reason = screen.getByTestId('engine-record-reason')
    expect(reason).toHaveAttribute('role', 'alert')
    expect(reason).toHaveTextContent('Sub-agent execution was cancelled')
    expect(screen.getAllByTestId('engine-record-child')[1]).toContainElement(reason)
    // The row sits under a reply in hosts whose composer draft is unsaved, so the
    // notice offers no navigation away from it.
    expect(within(screen.getByTestId('engine-subagent-record')).queryByRole('button', { name: /ask the agent/i })).toBeNull()
  })

  it('draws no notice for a child that did not fail, or failed with no reason', () => {
    const rec = { children: [
      { agent: 'a', task: 't', end: 'completed', reason: 'ignored', tools: [], tool_count: 0 },
      { agent: 'a', task: 't', end: 'failed', tools: [], tool_count: 0 },
    ] }
    render(<AssistantMessage {...BASE} engineSubagents={rec} />)
    fireEvent.click(screen.getByTestId('engine-subagent-record-toggle'))
    expect(screen.getAllByTestId('engine-record-child')).toHaveLength(2)
    expect(screen.queryByTestId('engine-record-reason')).toBeNull()
    expect(within(screen.getByTestId('engine-subagent-record')).queryByRole('alert')).toBeNull()
  })

  it('renders a model-written task as text, never as markup', () => {
    const rec = { children: [{ agent: 'a', task: '<img src=x onerror=alert(1)> **bold**', end: 'completed', tools: [], tool_count: 0 }] }
    const { container } = render(<AssistantMessage {...BASE} engineSubagents={rec} />)
    fireEvent.click(screen.getByTestId('engine-subagent-record-toggle'))
    expect(screen.getByTestId('engine-record-task').textContent).toBe('<img src=x onerror=alert(1)> **bold**')
    expect(container.querySelector('[data-testid="engine-subagent-record"] img')).toBeNull()
    expect(container.querySelector('[data-testid="engine-subagent-record"] strong')).toBeNull()
  })

  it('bounds what it draws and says how many more ran', () => {
    const got = engineRecordOf({ children: [{ agent: 'a', task: 't'.repeat(5000), end: 'weird', tools: [1, 'x'], tool_count: -3 }], more: 4 })!
    expect(got.children[0].task).toHaveLength(600)
    expect(got.children[0].end).toBe('not_reported')
    expect(got.children[0].tools).toEqual(['x'])
    expect(got.children[0].toolCount).toBe(1)
    render(<AssistantMessage {...BASE} engineSubagents={{ ...RECORD, more: 4 }} />)
    expect(screen.getByTestId('engine-subagent-record-toggle')).toHaveTextContent('Sub-agents in this turn: 7')
    fireEvent.click(screen.getByTestId('engine-subagent-record-toggle'))
    expect(screen.getByTestId('engine-subagent-record')).toHaveTextContent('More sub-agents not listed: 4')
  })

  it('is not drawn while the reply streams', () => {
    render(<AssistantMessage {...BASE} isStreaming engineSubagents={RECORD} />)
    expect(screen.queryByTestId('engine-subagent-record')).toBeNull()
  })
})
