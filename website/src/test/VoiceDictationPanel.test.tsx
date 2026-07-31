import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import VoiceDictationPanel from '../components/VoiceDictationPanel'
import { createAudioSample } from '../hooks/mic'

// The panel mounts the WebGL shader, which jsdom has no context for. Stub it:
// these tests are about the transcript/chrome contract, and Strands.tsx's own
// GL path is exercised by strandsSupported() + the browser harness.
vi.mock('../components/Strands', () => ({
  __esModule: true,
  default: () => <div data-testid="strands-stub" />,
  strandsSupported: () => true,
}))

const sampleRef = { current: createAudioSample() }

describe('VoiceDictationPanel', () => {
  it('renders the listening state with the active device', () => {
    render(<VoiceDictationPanel sampleRef={sampleRef} value="" deviceLabel="MacBook Pro Microphone" />)
    expect(screen.getByText('Listening')).toBeTruthy()
    expect(screen.getByText('MacBook Pro Microphone')).toBeTruthy()
  })

  it('omits the device row entirely when no label is known', () => {
    // Deliberately different from VoiceStatusBar, which shows "Default
    // microphone": at 17px over a live shader an extra placeholder row is
    // noise, and the panel's job is the transcript.
    render(<VoiceDictationPanel sampleRef={sampleRef} value="" />)
    expect(screen.queryByText('Default microphone')).toBeNull()
    expect(screen.getByText('Listening')).toBeTruthy()
  })

  it('advertises the keyboard affordances that actually exist', () => {
    render(<VoiceDictationPanel sampleRef={sampleRef} value="" />)
    expect(screen.getByText('Esc to stop, Enter to send')).toBeTruthy()
  })

  it('renders the whole value as committed when there is no partial', () => {
    render(<VoiceDictationPanel sampleRef={sampleRef} value="summarize the startup fix" />)
    const t = screen.getByTestId('voice-dictation-transcript')
    expect(t.textContent).toBe('summarize the startup fix')
    // No muted span -> nothing is being presented as provisional.
    expect(t.querySelector('.text-muted')).toBeNull()
  })

  it('splits the trailing partial hypothesis out as muted text', () => {
    render(
      <VoiceDictationPanel
        sampleRef={sampleRef}
        value="summarize the startup fix and tell me"
        partial=" and tell me"
      />,
    )
    const t = screen.getByTestId('voice-dictation-transcript')
    expect(t.textContent).toBe('summarize the startup fix and tell me')
    const muted = t.querySelector('.text-muted')
    expect(muted?.textContent).toBe(' and tell me')
  })

  it('treats everything as committed when the partial is not the suffix', () => {
    // The user typed after the partial landed, so the partial is no longer the
    // tail of the value. Styling a slice that does not match would mute the
    // wrong characters — fall back to all-committed instead.
    render(
      <VoiceDictationPanel
        sampleRef={sampleRef}
        value="summarize the startup fix — typed after"
        partial="and tell me"
      />,
    )
    const t = screen.getByTestId('voice-dictation-transcript')
    expect(t.textContent).toBe('summarize the startup fix — typed after')
    expect(t.querySelector('.text-muted')).toBeNull()
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

  it('is off when the setting is off, even with WebGL2 and full motion', async () => {
    const { useDictationPanelUsable } = await import('../components/VoiceDictationPanel')
    const seen: boolean[] = []
    const Probe = ({ on }: { on: boolean }) => {
      seen.push(useDictationPanelUsable(on))
      return null
    }
    render(<Probe on={false} />)
    expect(seen[0]).toBe(false)
  })

  it('is off under prefers-reduced-motion (the bar meter is the fallback, not a frozen frame)', async () => {
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
