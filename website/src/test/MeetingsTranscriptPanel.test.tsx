import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { VirtuosoMockContext } from 'react-virtuoso'

import MeetingWorkspace from '../apps/meetings/components/MeetingWorkspace'
import TranscriptPanel from '../apps/meetings/components/TranscriptPanel'
import type { TranscriptSegment } from '../apps/meetings/api'

const SEGMENTS: TranscriptSegment[] = [
  {
    id: 'speech-1',
    timestamp: '2026-08-09T10:15:00Z',
    source: 'speech',
    text: 'We agreed to ship on Tuesday.',
  },
  {
    id: 'typed-1',
    timestamp: '2026-08-09T10:16:00Z',
    source: 'typed',
    text: 'Correction: Wednesday.',
  },
]

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, 'scrollTo').mockImplementation(() => {})
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('TranscriptPanel', () => {
  it('renders durable speech and typed lines in one accessible region', () => {
    render(<TranscriptPanel segments={SEGMENTS} />)

    expect(screen.getByRole('region', { name: 'Meeting transcript' })).toBeTruthy()
    expect(screen.getByText('We agreed to ship on Tuesday.')).toBeTruthy()
    expect(screen.getAllByText('Correction: Wednesday.')).toHaveLength(2)
    expect(screen.queryByText('Speech')).toBeNull()
    expect(screen.getByText('Typed')).toBeTruthy()
    expect(document.querySelector('[aria-live="polite"]')).toHaveTextContent(
      'Correction: Wednesday.',
    )
  })

  it('shows partial speech as live without adding a durable row', () => {
    const { rerender } = render(
      <TranscriptPanel segments={[]} partial="still speaking" />,
    )
    expect(screen.getByText('Live')).toBeTruthy()
    expect(screen.getByText('still speaking')).toBeTruthy()
    expect(document.querySelector('[aria-live="polite"]')).toHaveTextContent('')

    rerender(<TranscriptPanel segments={[]} partial="" />)
    expect(screen.getByText('No transcript yet')).toBeTruthy()
    expect(screen.queryByText('still speaking')).toBeNull()
  })

  it('explains the primary transcript layout when every agent is disabled', () => {
    render(<TranscriptPanel segments={SEGMENTS} primary />)

    expect(screen.getByText('No agents enabled')).toBeTruthy()
    expect(screen.getByText('Turn one on in the bar above, or pick a preset.')).toBeTruthy()
  })

  it('uses an ended-state empty hint during review', () => {
    render(<TranscriptPanel segments={[]} status="reviewing" />)

    expect(screen.getByText('No transcript was recorded for this meeting.')).toBeTruthy()
    expect(screen.queryByText(/as the meeting continues/i)).toBeNull()
  })

  it('surfaces a full transcript and hides undurable partial speech', () => {
    render(<TranscriptPanel segments={SEGMENTS} partial="not durable" full />)

    expect(screen.getByRole('status')).toHaveTextContent(
      'Transcript is full. New speech and broadcasts are no longer being recorded or sent to agents.',
    )
    expect(screen.queryByText('not durable')).toBeNull()
  })

  it('pauses follow mode when the reader scrolls up and can jump to the latest row', () => {
    const { container } = render(<TranscriptPanel segments={SEGMENTS} />)
    const scroller = container.querySelector('.overflow-y-auto') as HTMLDivElement
    Object.defineProperties(scroller, {
      scrollHeight: { configurable: true, value: 800 },
      clientHeight: { configurable: true, value: 200 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    })

    fireEvent.scroll(scroller)
    fireEvent.click(screen.getByText('Jump to latest'))
    expect(scroller.scrollTo).toHaveBeenCalledWith({ top: 800, behavior: 'smooth' })
  })

  it('virtualizes long transcripts instead of mounting every row', () => {
    const segments = Array.from({ length: 240 }, (_, index): TranscriptSegment => ({
      id: `speech-${index}`,
      timestamp: '2026-08-09T10:15:00Z',
      source: 'speech',
      text: `Segment ${index}`,
    }))

    render(
      <VirtuosoMockContext.Provider value={{ viewportHeight: 300, itemHeight: 30 }}>
        <TranscriptPanel segments={segments} />
      </VirtuosoMockContext.Provider>,
    )

    expect(screen.getByRole('list')).toBeTruthy()
    expect(screen.getAllByRole('listitem').length).toBeLessThan(segments.length)
  })
})

describe('MeetingWorkspace transcript layout', () => {
  it('promotes the transcript when no agent panels are enabled', async () => {
    const { rerender } = render(
      <MeetingWorkspace
        hasAgentPanels
        agentPanels={<div>Agent notes</div>}
        transcript={<div>Transcript content</div>}
      />,
    )

    expect(screen.getByTestId('meeting-workspace').dataset.transcriptLayout).toBe('split')
    expect(screen.getByTestId('meeting-agent-panels')).toBeTruthy()

    rerender(
      <MeetingWorkspace
        hasAgentPanels={false}
        agentPanels={<div>Agent notes</div>}
        transcript={<div>Transcript content</div>}
      />,
    )

    expect(screen.getByTestId('meeting-workspace').dataset.transcriptLayout).toBe('primary')
    await waitFor(() => expect(screen.queryByTestId('meeting-agent-panels')).toBeNull())
    expect(screen.getByTestId('meeting-transcript-slot').className).toContain('flex-1')
  })
})
