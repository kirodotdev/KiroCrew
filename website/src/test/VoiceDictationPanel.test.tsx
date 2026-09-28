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

  it('advertises the keyboard affordances that actually exist (batch: click the mic to finish)', () => {
    render(<VoiceDictationPanel sampleRef={sampleRef} value="" />)
    expect(screen.getByText('Esc to cancel, click the mic to finish')).toBeTruthy()
  })

  it('promises Enter-to-send only in streaming mode (live transcript in composer)', () => {
    render(<VoiceDictationPanel sampleRef={sampleRef} value="" streaming />)
    expect(screen.getByText('Esc to cancel, Enter to send')).toBeTruthy()
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

describe('word revision tracking', () => {
  it('climbs stability for surviving words and flashes the one revised in place', () => {
    const { rerender } = render(
      <VoiceDictationPanel sampleRef={sampleRef} value="fix the wold" partial="fix the wold" streaming />,
    )
    rerender(
      <VoiceDictationPanel sampleRef={sampleRef} value="fix the world" partial="fix the world" streaming />,
    )
    const words = screen.getAllByTestId('dictation-word')
    expect(words.map(w => w.textContent)).toEqual(['fix', 'the', 'world'])
    // Survivors of two consecutive hypotheses ramp toward solid…
    expect(words[0].getAttribute('data-stability')).toBe('1')
    expect(words[1].getAttribute('data-stability')).toBe('1')
    // …while "wold" → "world" is a revision: reset to dim, generation bump = flash.
    expect(words[2].getAttribute('data-stability')).toBe('0')
    expect(words[2].getAttribute('data-generation')).toBe('1')
  })

  it('does not flash words appended past the previous hypothesis (dictation, not revision)', () => {
    const { rerender } = render(
      <VoiceDictationPanel sampleRef={sampleRef} value="fix the" partial="fix the" streaming />,
    )
    rerender(
      <VoiceDictationPanel sampleRef={sampleRef} value="fix the build" partial="fix the build" streaming />,
    )
    const words = screen.getAllByTestId('dictation-word')
    expect(words.map(w => w.textContent)).toEqual(['fix', 'the', 'build'])
    expect(words[2].getAttribute('data-generation')).toBe('0')
    expect(words[2].getAttribute('data-stability')).toBe('0')
  })

  it('confines an insertion to the inserted word — the unchanged tail keeps its stability', () => {
    const { rerender } = render(
      <VoiceDictationPanel sampleRef={sampleRef} value="fix the build" partial="fix the build" streaming />,
    )
    rerender(
      <VoiceDictationPanel sampleRef={sampleRef} value="fix in the build" partial="fix in the build" streaming />,
    )
    const words = screen.getAllByTestId('dictation-word')
    expect(words.map(w => w.textContent)).toEqual(['fix', 'in', 'the', 'build'])
    // "fix" survived; "in" was inserted (flashes); "the"/"build" re-anchor and
    // KEEP their earned stability instead of reading as revised.
    expect(words[0].getAttribute('data-stability')).toBe('1')
    expect(words[1].getAttribute('data-generation')).toBe('1')
    expect(words[1].getAttribute('data-stability')).toBe('0')
    expect(words[2].getAttribute('data-stability')).toBe('1')
    expect(words[3].getAttribute('data-stability')).toBe('1')
  })

  it('keeps the surviving words\u2019 DOM nodes across an insertion (no remount, no spurious flash)', () => {
    const { rerender } = render(
      <VoiceDictationPanel sampleRef={sampleRef} value="fix the build" partial="fix the build" streaming />,
    )
    const beforeNodes = screen.getAllByTestId('dictation-word')
    rerender(
      <VoiceDictationPanel sampleRef={sampleRef} value="fix in the build" partial="fix in the build" streaming />,
    )
    const after = screen.getAllByTestId('dictation-word')
    // "the" and "build" re-anchored as the SAME words: identical DOM nodes,
    // so React never remounted them and their flash cannot restart.
    expect(after[2]).toBe(beforeNodes[1])
    expect(after[3]).toBe(beforeNodes[2])
    // The inserted word is a new node.
    expect(beforeNodes).not.toContain(after[1])
  })

  it('flashes a replacement even when the new text duplicates a later word (a b c \u2192 a c c)', () => {
    const { rerender } = render(
      <VoiceDictationPanel sampleRef={sampleRef} value="a b c" partial="a b c" streaming />,
    )
    rerender(<VoiceDictationPanel sampleRef={sampleRef} value="a c c" partial="a c c" streaming />)
    const words = screen.getAllByTestId('dictation-word')
    expect(words.map(w => w.textContent)).toEqual(['a', 'c', 'c'])
    // "b" \u2192 "c" is an in-place revision and must flash; anchoring to the old
    // "c" would have read it as deletion-plus-append and never flashed.
    expect(words[1].getAttribute('data-generation')).toBe('1')
    expect(words[1].getAttribute('data-stability')).toBe('0')
    // The original trailing "c" survived.
    expect(words[2].getAttribute('data-stability')).toBe('1')
  })

  it('resets tracking when a phrase commits, instead of diffing across unrelated phrases', () => {
    const { rerender } = render(
      <VoiceDictationPanel sampleRef={sampleRef} value="hello world" partial="hello world" streaming />,
    )
    rerender(
      <VoiceDictationPanel
        sampleRef={sampleRef}
        value="hello world. next phrase"
        partial=" next phrase"
        streaming
      />,
    )
    const words = screen.getAllByTestId('dictation-word')
    expect(words.map(w => w.textContent)).toEqual(['next', 'phrase'])
    for (const w of words) {
      expect(w.getAttribute('data-stability')).toBe('0')
      expect(w.getAttribute('data-generation')).toBe('0')
    }
  })

  it('preserves exact transcript text (whitespace included) through the word spans', () => {
    render(
      <VoiceDictationPanel
        sampleRef={sampleRef}
        value="summarize the fix  and   tell me"
        partial="  and   tell me"
      />,
    )
    const t = screen.getByTestId('voice-dictation-transcript')
    expect(t.textContent).toBe('summarize the fix  and   tell me')
    expect(t.querySelector('.text-muted')?.textContent).toBe('  and   tell me')
  })
})
