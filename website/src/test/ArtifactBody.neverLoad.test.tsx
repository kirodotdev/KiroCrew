import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import type { Artifact } from '../types'

// ArtifactBody has always carried `docSilent`, but it covers a DIFFERENT silent
// condition: a frame that LOADED and then never reported its height (its timer
// arms only once `loadedUrlRef.current === blobUrl`, i.e. after `load` has
// fired). The case where `load` NEVER fires — the mint succeeded, a url is in
// hand, but the document never loaded at all — left that timer un-armed and the
// frame invisible with no notice, the same never-load trap the three sibling
// frames had (#11199). ArtifactBody now uses the shared `useSilentLoadWatch` for
// it too, so it is a real 4-of-4, not 3-of-4 with the flagship surface left out.
//
// These tests deliberately never fire `load`: that is the whole point. Removing
// the `useSilentLoadWatch` wiring in ArtifactBody must red the first test (no
// notice ever appears), while the second guards against a false positive on a
// frame that does load.

const SLUG = 'my-widget'
const HTML_CONTENT = '<p>hello</p>'
const DOC_URL = '/sandbox-doc/abc123/1700000000.mac'

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ theme: 'dark', colorTheme: 'default', themeVersion: 0 }),
}))

vi.mock('../hooks/useCommentBridge', () => ({
  useCommentBridge: () => ({ scrollToAnchor: vi.fn() }),
}))

vi.mock('../lib/widgetSrcdoc', () => ({
  THEME_VAR_NAMES: [] as string[],
  buildSrcdoc: (opts: { html: string }) => opts.html,
}))

const mintSpy = vi.fn()
vi.mock('../api/client', () => ({
  api: { sandboxDocUrl: (html: string) => mintSpy(html) },
  ApiError: class extends Error {},
}))

import { ArtifactBodyIframe } from '../components/ArtifactBody'

function makeArtifact(content: string): Artifact {
  return { slug: SLUG, name: 'Widget', kind: 'widget', content } as unknown as Artifact
}

describe('ArtifactBodyIframe surfaces a load that never fires', () => {
  beforeEach(() => {
    mintSpy.mockReset()
    mintSpy.mockResolvedValue({ url: DOC_URL })
    // shouldAdvanceTime keeps promises and waitFor working while still allowing
    // the grace window to be advanced deliberately.
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('offers a Show artifact action when the frame never fires load', async () => {
    // The mint resolves (a url is in hand) but the frame's `load` never fires —
    // exactly what a hung /sandbox-doc response produces. Before this wiring the
    // frame sat invisible with no notice, because docSilent's timer only arms
    // AFTER a load. Now the shared watch surfaces the same notice + action.
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    await waitFor(() => {
      const el = document.querySelector('iframe')
      if (!el) throw new Error('frame never mounted')
      return el
    })
    expect(screen.queryByText(/no longer showing/i)).toBeNull()

    // Flush the passive effect first so the watch's arming timer exists, then
    // advance past the grace window. No fireEvent.load — the load never arrives.
    await act(async () => { await Promise.resolve() })
    await act(async () => { vi.advanceTimersByTime(4000) })

    // Cause-neutral copy (a never-loaded frame is not a proven failure) and the
    // same Eye + "Show artifact" recovery ArtifactBody's docSilent branch uses.
    expect(await screen.findByText(/this artifact is no longer showing/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /show artifact/i })).toBeTruthy()
    expect(screen.queryByText(/couldn't render this artifact/i)).toBeNull()
  })

  it('stays quiet when the frame loads and reports its height', async () => {
    // The false-positive guard: a frame that loads AND reports height must never
    // trip either silent path, or the surface would cry wolf on every healthy
    // open. (A load with no height report is docSilent's own case, covered
    // elsewhere; here the document is fully healthy.)
    const { fireEvent } = await import('@testing-library/react')
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    const frame = await waitFor(() => {
      const el = document.querySelector('iframe')
      if (!el) throw new Error('frame never mounted')
      return el as HTMLIFrameElement
    })
    fireEvent.load(frame)
    fireEvent(window, new MessageEvent('message', {
      data: { type: 'mc-widget-height', height: 420 },
      source: frame.contentWindow as MessageEventSource,
    }))

    await act(async () => { vi.advanceTimersByTime(4000) })

    expect(screen.queryByText(/no longer showing/i)).toBeNull()
    expect(screen.queryByText(/couldn't render this artifact/i)).toBeNull()
  })
})
