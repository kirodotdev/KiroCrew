import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { AnimatePresence } from 'framer-motion'
import VoiceDictationPanel from '../components/VoiceDictationPanel'
import { createAudioSample } from '../hooks/mic'

const sampleRef = { current: createAudioSample() }

function stubTranscriptScroll() {
  const prototype = HTMLDivElement.prototype
  const previousTop = Object.getOwnPropertyDescriptor(prototype, 'scrollTop')
  const previousHeight = Object.getOwnPropertyDescriptor(prototype, 'scrollHeight')
  const previousClientHeight = Object.getOwnPropertyDescriptor(prototype, 'clientHeight')
  let scrollTop = 0
  Object.defineProperty(prototype, 'scrollTop', {
    configurable: true,
    get: () => scrollTop,
    set: (value: number) => { scrollTop = value },
  })
  Object.defineProperty(prototype, 'scrollHeight', { configurable: true, get: () => 999 })
  Object.defineProperty(prototype, 'clientHeight', { configurable: true, get: () => 80 })
  return {
    read: () => scrollTop,
    restore: () => {
      if (previousTop) Object.defineProperty(prototype, 'scrollTop', previousTop)
      else delete (prototype as unknown as Record<string, unknown>).scrollTop
      if (previousHeight) Object.defineProperty(prototype, 'scrollHeight', previousHeight)
      else delete (prototype as unknown as Record<string, unknown>).scrollHeight
      if (previousClientHeight) Object.defineProperty(prototype, 'clientHeight', previousClientHeight)
      else delete (prototype as unknown as Record<string, unknown>).clientHeight
    },
  }
}

describe('VoiceDictationPanel', () => {
  it('renders the listening state with the active device', () => {
    render(<VoiceDictationPanel sampleRef={sampleRef} deviceLabel="MacBook Pro Microphone" onSelectDevice={() => {}} />)
    expect(screen.getByText('Listening')).toBeTruthy()
    expect(screen.getByText('MacBook Pro Microphone')).toBeTruthy()
  })

  it('shows elapsed recording time and freezes it during panel exit', () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'performance'] })
    try {
      const { rerender } = render(
        <AnimatePresence>
          <VoiceDictationPanel key="panel" sampleRef={sampleRef} onSelectDevice={() => {}} />
        </AnimatePresence>,
      )
      const reading = () => screen.getByTestId('voice-recording-elapsed').textContent
      expect(reading()).toBe('0:00')
      act(() => { vi.advanceTimersByTime(9_000) })
      expect(reading()).toBe('0:09')
      rerender(<AnimatePresence>{false}</AnimatePresence>)
      act(() => { vi.advanceTimersByTime(100) })
      expect(reading()).toBe('0:09')
    } finally {
      vi.useRealTimers()
    }
  })

  it('omits the device row entirely when no label is known', () => {
    render(<VoiceDictationPanel sampleRef={sampleRef} onSelectDevice={() => {}} />)
    expect(screen.queryByText('Default microphone')).toBeNull()
    expect(screen.getByText('Listening')).toBeTruthy()
  })

  it('advertises the keyboard affordances that actually exist', () => {
    render(<VoiceDictationPanel sampleRef={sampleRef} onSelectDevice={() => {}} />)
    expect(screen.getByText('Esc to cancel, click the mic to finish')).toBeTruthy()
  })

  it.each([true, false])('names the send and Shift-click under submit-on-release (streaming=%s)', streaming => {
    render(<VoiceDictationPanel sampleRef={sampleRef} streaming={streaming} submitOnRelease onSelectDevice={() => {}} />)
    expect(screen.getByText('Esc to cancel, Shift-click keeps a draft, click the mic to send')).toBeTruthy()
    expect(screen.queryByText('Esc to cancel, Enter to send')).toBeNull()
  })

  it('promises Enter-to-send only in streaming mode', () => {
    render(<VoiceDictationPanel sampleRef={sampleRef} streaming onSelectDevice={() => {}} />)
    expect(screen.getByText('Esc to cancel, Enter to send')).toBeTruthy()
  })

  it('says editing resumes on stop only while streaming fills the text box', () => {
    const { rerender } = render(<VoiceDictationPanel sampleRef={sampleRef} streaming onSelectDevice={() => {}} />)
    expect(screen.getByText('Editing resumes when you stop')).toBeTruthy()
    rerender(<VoiceDictationPanel sampleRef={sampleRef} onSelectDevice={() => {}} />)
    expect(screen.queryByTestId('voice-dictation-edit-locked')).toBeNull()
    rerender(<VoiceDictationPanel sampleRef={sampleRef} streaming gestureDriven onSelectDevice={() => {}} />)
    expect(screen.queryByTestId('voice-dictation-edit-locked')).toBeNull()
  })

  it('says Muted and offers unmute while muted', () => {
    const onToggleMute = vi.fn()
    render(<VoiceDictationPanel sampleRef={sampleRef} muted onToggleMute={onToggleMute} onSelectDevice={() => {}} />)
    expect(screen.getByText('Muted')).toBeTruthy()
    expect(screen.queryByText('Listening')).toBeNull()
    const btn = screen.getByTestId('voice-mute-toggle')
    expect(btn.getAttribute('aria-label')).toBe('Unmute microphone')
    expect(btn.hasAttribute('aria-pressed')).toBe(false)
    fireEvent.click(btn)
    expect(onToggleMute).toHaveBeenCalledTimes(1)
  })

  it('keeps the recording label truthful while the panel exits', () => {
    const { rerender } = render(
      <AnimatePresence>
        <VoiceDictationPanel key="panel" sampleRef={sampleRef} onSelectDevice={() => {}} />
      </AnimatePresence>,
    )
    rerender(<AnimatePresence>{false}</AnimatePresence>)
    expect(screen.queryByText('Getting ready to transcribe your dictation.')).toBeNull()
    expect(screen.getByText('Listening')).toBeTruthy()
  })

  it('keeps the touch transcript pinned to its newest words', () => {
    const scroll = stubTranscriptScroll()
    try {
      const { rerender } = render(
        <VoiceDictationPanel sampleRef={sampleRef} transcript="one" onSelectDevice={() => {}} />,
      )
      expect(scroll.read()).toBe(999)
      rerender(
        <VoiceDictationPanel
          sampleRef={sampleRef}
          transcript="one two three four five six seven eight"
          onSelectDevice={() => {}}
        />,
      )
      expect(scroll.read()).toBe(999)
      expect(screen.getByTestId('hold-to-talk-transcript').className).toContain('overscroll-contain')
    } finally {
      scroll.restore()
    }
  })

  it('pauses touch transcript following while the user reads older words', () => {
    const scroll = stubTranscriptScroll()
    try {
      const { rerender } = render(
        <VoiceDictationPanel sampleRef={sampleRef} transcript="one" onSelectDevice={() => {}} />,
      )
      const transcript = screen.getByTestId('hold-to-talk-transcript')
      transcript.scrollTop = 300
      fireEvent.scroll(transcript)
      rerender(
        <VoiceDictationPanel sampleRef={sampleRef} transcript="one two" onSelectDevice={() => {}} />,
      )
      expect(scroll.read()).toBe(300)

      transcript.scrollTop = 919
      fireEvent.scroll(transcript)
      rerender(
        <VoiceDictationPanel sampleRef={sampleRef} transcript="one two three" onSelectDevice={() => {}} />,
      )
      expect(scroll.read()).toBe(999)
    } finally {
      scroll.restore()
    }
  })
})

describe('useDictationPanelUsable', () => {
  let matchMediaSpy: ReturnType<typeof vi.fn>

  beforeEach(() => {
    matchMediaSpy = vi.fn().mockImplementation((q: string) => ({
      matches: false,
      media: q,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }))
    vi.stubGlobal('matchMedia', matchMediaSpy)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('is off when the setting is off, even with full motion', async () => {
    const { useDictationPanelUsable } = await import('../components/VoiceDictationPanel')
    const seen: boolean[] = []
    const Probe = ({ on }: { on: boolean }) => {
      seen.push(useDictationPanelUsable(on))
      return null
    }
    render(<Probe on={false} />)
    expect(seen[0]).toBe(false)
  })

  it('is off under prefers-reduced-motion', async () => {
    matchMediaSpy.mockImplementation((q: string) => ({
      matches: q.includes('reduced-motion'),
      media: q,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }))
    const { useDictationPanelUsable } = await import('../components/VoiceDictationPanel')
    const seen: boolean[] = []
    const Probe = () => { seen.push(useDictationPanelUsable(true)); return null }
    render(<Probe />)
    expect(seen[0]).toBe(false)
  })
})
