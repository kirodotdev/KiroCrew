/**
 * The roster-miss report: a transcript that names a well-formed session key the
 * roster does not list tells the host (via `SessionRosterMissCtx`) so it can
 * widen the roster — the closed-sessions list is fetched lazily, so on a fresh
 * load a closed session an agent linked to is simply not loaded yet.
 *
 * The report ASKS; it never resolves. Resolution stays the roster's call, so
 * every case where no chip could ever be offered (no routing wired, a non-key
 * span, the active session) is not a miss either.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

import MarkdownRenderer from '../components/MarkdownRenderer'
import { SessionRosterMissCtx } from '../lib/sessionRoster'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => undefined) }))

const KNOWN = 'chat-24-1784661951'
const UNKNOWN = 'chat-99-1700000000'

const roster = () => new Map([[KNOWN, 'Fix the pagination bug']])

let onSessionOpen: ReturnType<typeof vi.fn>
let onMiss: ReturnType<typeof vi.fn>

beforeEach(() => {
  onSessionOpen = vi.fn()
  onMiss = vi.fn()
})

const withHost = (ui: React.ReactElement) => (
  <SessionRosterMissCtx.Provider value={onMiss}>{ui}</SessionRosterMissCtx.Provider>
)

describe('session roster miss — reported to the host', () => {
  it('reports a bare key the roster does not list, exactly once', () => {
    const { rerender } = render(withHost(
      <MarkdownRenderer content={`see \`${UNKNOWN}\``} onSessionOpen={onSessionOpen} sessions={roster()} />,
    ))
    expect(onMiss).toHaveBeenCalledTimes(1)
    expect(onMiss).toHaveBeenCalledWith(UNKNOWN)
    // The span still renders as the copy chip: the report asked, it did not resolve.
    expect(screen.getByText(UNKNOWN)).not.toHaveAttribute('data-session-key')

    // A re-render of the same span is not a second miss.
    rerender(withHost(
      <MarkdownRenderer content={`see \`${UNKNOWN}\``} onSessionOpen={onSessionOpen} sessions={roster()} />,
    ))
    expect(onMiss).toHaveBeenCalledTimes(1)
  })

  it('reports a /chat?sid= link whose key the roster does not list', () => {
    render(withHost(
      <MarkdownRenderer content={`[gone](/chat?sid=${UNKNOWN})`} onSessionOpen={onSessionOpen} sessions={roster()} />,
    ))
    expect(onMiss).toHaveBeenCalledTimes(1)
    expect(onMiss).toHaveBeenCalledWith(UNKNOWN)
  })

  it('reports a key inside a link once, from the anchor alone', () => {
    // Inside a link the anchor owns the reference; the inline span must not
    // double-report the same key.
    render(withHost(
      <MarkdownRenderer content={`[\`${UNKNOWN}\`](/chat?sid=${UNKNOWN})`} onSessionOpen={onSessionOpen} sessions={roster()} />,
    ))
    expect(onMiss).toHaveBeenCalledTimes(1)
  })

  it('resolves once the host widens the roster with the reported key', () => {
    // The whole point of the report: the host answers by listing the session, and
    // the same span becomes a live chip that switches on click.
    const { rerender } = render(withHost(
      <MarkdownRenderer content={`\`${UNKNOWN}\``} onSessionOpen={onSessionOpen} sessions={roster()} />,
    ))
    expect(screen.getByText(UNKNOWN)).not.toHaveAttribute('data-session-key')

    const widened = new Map([...roster(), [UNKNOWN, 'Closed but on disk']])
    rerender(withHost(
      <MarkdownRenderer content={`\`${UNKNOWN}\``} onSessionOpen={onSessionOpen} sessions={widened} />,
    ))
    const chip = screen.getByText(UNKNOWN)
    expect(chip).toHaveAttribute('data-session-key', UNKNOWN)
    chip.click()
    expect(onSessionOpen).toHaveBeenCalledWith(UNKNOWN)
    // Listed now, so no further miss.
    expect(onMiss).toHaveBeenCalledTimes(1)
  })
})

describe('session roster miss — not a miss', () => {
  it('stays silent for a key the roster lists', () => {
    render(withHost(
      <MarkdownRenderer content={`\`${KNOWN}\` and [it](/chat?sid=${KNOWN})`} onSessionOpen={onSessionOpen} sessions={roster()} />,
    ))
    expect(onMiss).not.toHaveBeenCalled()
  })

  it('stays silent for the session the reader is already in', () => {
    render(withHost(
      <MarkdownRenderer
        content={`\`${UNKNOWN}\` and [here](/chat?sid=${UNKNOWN})`}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        activeSession={UNKNOWN}
      />,
    ))
    expect(onMiss).not.toHaveBeenCalled()
  })

  it('stays silent when the renderer cannot route sessions', () => {
    // No roster (offline, or a caller that never wired one) and no handler: there
    // is nothing a widened roster could enable, so there is nothing to ask for.
    render(withHost(<MarkdownRenderer content={`\`${UNKNOWN}\``} onSessionOpen={onSessionOpen} />))
    render(withHost(<MarkdownRenderer content={`\`${UNKNOWN}\``} sessions={roster()} />))
    expect(onMiss).not.toHaveBeenCalled()
  })

  it('stays silent for a span that merely resembles a key', () => {
    render(withHost(
      <MarkdownRenderer
        content={'`chat-24` and `chat-24-1784661951.jsonl` and [x](/chat?sid=nope)'}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
      />,
    ))
    expect(onMiss).not.toHaveBeenCalled()
  })

  it('is a no-op with no host listening', () => {
    // A renderer outside ChatPage has no provider; a miss is then simply a miss.
    render(<MarkdownRenderer content={`\`${UNKNOWN}\``} onSessionOpen={onSessionOpen} sessions={roster()} />)
    expect(screen.getByText(UNKNOWN)).not.toHaveAttribute('data-session-key')
  })
})
