import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import type { Artifact } from '../types'

// The frame loads a real DOCUMENT minted by the gateway, not a `blob:` URL built
// in the browser: some WebKit-based in-app browsers refuse a blob load outright
// ("invalid url or response") and can take the whole page down with it, and a
// sandboxed `srcdoc` frame blank-renders on WebKit.
//
// Pinned here: the frame addresses the minted URL, it never builds a blob for
// itself (that form renders fine in Chromium, which is why it could come back
// unnoticed), and the URL is cleared when content empties so the frame is never
// left pointing at a stale document — the same class of bug the previous blob
// lifecycle test guarded.

const SLUG = 'my-widget'
const HTML_CONTENT = '<p>hello</p>'
const DOC_URL = '/sandbox-doc/abc123/1700000000.mac'

vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ theme: 'dark', colorTheme: 'default', themeVersion: 0 }),
}))

vi.mock('../hooks/useCommentBridge', () => ({
  useCommentBridge: () => ({ scrollToAnchor: vi.fn() }),
}))

const { buildSrcdocSpy } = vi.hoisted(() => ({ buildSrcdocSpy: vi.fn() }))
vi.mock('../lib/widgetSrcdoc', () => ({
  THEME_VAR_NAMES: [] as string[],
  buildSrcdoc: (opts: { html: string }) => {
    buildSrcdocSpy(opts)
    return opts.html
  },
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

describe('ArtifactBodyIframe document URL lifecycle', () => {
  const originalCreate = globalThis.URL.createObjectURL

  beforeEach(() => {
    mintSpy.mockReset()
    mintSpy.mockResolvedValue({ url: DOC_URL })
    // Any use of this for the frame is a regression to the crashing form.
    globalThis.URL.createObjectURL = vi.fn(() => {
      throw new Error('the artifact frame must not use a blob: URL')
    }) as never
  })

  afterEach(() => {
    globalThis.URL.createObjectURL = originalCreate
  })

  it('points the iframe at the minted document URL', async () => {
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    await waitFor(() => {
      const frame = document.querySelector('iframe')
      expect(frame?.getAttribute('src')).toBe(DOC_URL)
    })
    expect(mintSpy).toHaveBeenCalledWith(HTML_CONTENT)
  })

  it('builds no blob URL for the frame', async () => {
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    await waitFor(() => expect(mintSpy).toHaveBeenCalled())
    expect(globalThis.URL.createObjectURL).not.toHaveBeenCalled()
  })

  it('keeps the frame invisible until its document reports load', async () => {
    // Swapping the themed placeholder for the iframe the moment the URL arrives
    // shows the ENGINE's own canvas for the length of the document fetch, and
    // some engines paint that canvas white whatever this element's background
    // says — a flash on every open. The frame therefore starts transparent with
    // a themed panel underneath, exactly as WidgetFrame already did.
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    const frame = await waitFor(() => {
      const el = document.querySelector('iframe')
      if (!el) throw new Error('frame never mounted')
      return el as HTMLIFrameElement
    })
    expect(frame.style.opacity).toBe('0')

    fireEvent.load(frame)
    expect(frame.style.opacity).toBe('1')
  })

  it('keeps a rendered document visible while the next one is minted', async () => {
    // The frame used to be re-hidden on every new url, on the reasoning that a
    // previous load must not vouch for a still-loading document. The cost is
    // worse than the problem: nothing reveals the frame until the NEXT `load`
    // fires, so a slow navigation leaves a document the reader was already
    // reading covered for the length of a round trip, and a further re-mint
    // landing first makes that permanent.
    //
    // So the visibility gate covers the FIRST document only. Swapping `src` tears
    // the old document down immediately, so this cannot present stale content as
    // the new document; the cost is a brief engine canvas instead of the themed
    // placeholder, which is the deliberate trade.
    const { rerender } = render(
      <ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />,
    )
    const frame = await waitFor(() => {
      const el = document.querySelector('iframe')
      if (!el) throw new Error('frame never mounted')
      return el as HTMLIFrameElement
    })
    fireEvent.load(frame)
    expect(frame.style.opacity).toBe('1')

    mintSpy.mockResolvedValue({ url: '/sandbox-doc/second/tok' })
    rerender(<ArtifactBodyIframe artifact={makeArtifact('<p>changed</p>')} />)
    await waitFor(() =>
      expect(document.querySelector('iframe')?.getAttribute('src')).toBe(
        '/sandbox-doc/second/tok',
      ),
    )
    // The new document has NOT reported load yet, and the frame stays visible.
    expect(document.querySelector('iframe')?.style.opacity).toBe('1')
  })

  it('offers a retry instead of an eternal progress label when the mint fails', async () => {
    // A failed mint used to leave the "Rendering…" placeholder up forever — a
    // label asserting progress when nothing is in flight. This matters more now
    // that the document is single-use: a spent URL is a real outcome, so the
    // retry IS the recovery path rather than a courtesy.
    mintSpy.mockRejectedValueOnce(new Error('gateway said no'))
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)

    const failure = await screen.findByText(/couldn't render this artifact/i)
    expect(failure).toBeTruthy()
    expect(screen.queryByText(/rendering…/i)).toBeNull()
    expect(document.querySelector('iframe')).toBeNull()

    // Retry must MINT AGAIN. Re-rendering the spent URL would recover nothing,
    // so a second call to the gateway is the behaviour under test.
    mintSpy.mockResolvedValue({ url: DOC_URL })
    const retry = screen.getByRole('button', { name: /retry/i })
    retry.click()

    await waitFor(() => {
      expect(document.querySelector('iframe')?.getAttribute('src')).toBe(DOC_URL)
    })
    expect(mintSpy).toHaveBeenCalledTimes(2)
    expect(screen.queryByText(/couldn't render this artifact/i)).toBeNull()
  })

  it('holds the previous document while a new one is in flight', async () => {
    // Clearing the URL before re-minting flashed an open artifact out to the
    // placeholder on every theme change, since a theme change rebuilds the
    // document and now costs a round trip.
    const { rerender } = render(
      <ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />,
    )
    await waitFor(() =>
      expect(document.querySelector('iframe')?.getAttribute('src')).toBe(DOC_URL),
    )

    let release: (v: { url: string }) => void = () => {}
    mintSpy.mockReturnValueOnce(
      new Promise<{ url: string }>((resolve) => {
        release = resolve
      }),
    )
    rerender(<ArtifactBodyIframe artifact={makeArtifact('<p>changed</p>')} />)

    // Still showing the OLD document, not a placeholder, while the mint runs.
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe(DOC_URL)
    expect(screen.queryByText(/rendering…/i)).toBeNull()

    release({ url: '/sandbox-doc/second/tok' })
    await waitFor(() =>
      expect(document.querySelector('iframe')?.getAttribute('src')).toBe(
        '/sandbox-doc/second/tok',
      ),
    )
  })

  it('clears the URL when content empties, leaving no stale document', async () => {
    const { rerender } = render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    rerender(<ArtifactBodyIframe artifact={makeArtifact('')} />)
    await waitFor(() => expect(document.querySelector('iframe')).toBeNull())
  })

  it('renders no frame when minting fails', async () => {
    mintSpy.mockRejectedValueOnce(new Error('gateway down'))
    render(<ArtifactBodyIframe artifact={makeArtifact(HTML_CONTENT)} />)
    await waitFor(() => expect(mintSpy).toHaveBeenCalled())
    expect(document.querySelector('iframe')).toBeNull()
  })
})

// A frame TALLER than the document inside it is not a cosmetic imperfection:
// iOS WebKit leaves such a frame unpainted, so a short artifact read as an empty
// box on iPhone while the same document rendered in the gallery (whose thumbnail
// frame is always shorter than its content) and on desktop, which paints either
// way. Measured on the reporting device: 385px and 465px of content in a 573px
// frame were blank; 617px and ~800px in the same frame rendered. Neither a
// script forcing layout nor repeated DOM mutation changed the outcome — only the
// height did.
//
// The frame therefore takes its document's own reported height and carries NO
// floor. These tests pin the two halves that a well-meaning later change would
// undo: the reporter must be requested, and a height BELOW the fixed box this
// replaced must be honored rather than clamped up to it.
describe('ArtifactBodyIframe frame height follows its document', () => {
  beforeEach(() => {
    mintSpy.mockReset()
    mintSpy.mockResolvedValue({ url: DOC_URL })
    buildSrcdocSpy.mockClear()
  })

  async function mountFrame(content: string): Promise<HTMLIFrameElement> {
    render(<ArtifactBodyIframe artifact={makeArtifact(content)} />)
    const frame = await waitFor(() => {
      const el = document.querySelector('iframe')
      if (!el) throw new Error('frame never mounted')
      return el as HTMLIFrameElement
    })
    fireEvent.load(frame)
    return frame
  }

  function report(frame: HTMLIFrameElement, height: unknown, from?: unknown): void {
    fireEvent(window, new MessageEvent('message', {
      data: { type: 'mc-widget-height', height },
      source: (from ?? frame.contentWindow) as MessageEventSource,
    }))
  }

  it('asks its document to report its own height', async () => {
    await mountFrame('<p>reporter</p>')
    // Without this the parent has nothing to size against and falls back to a
    // fixed box — which is the blank-frame shape on iOS.
    expect(buildSrcdocSpy).toHaveBeenCalledWith(
      expect.objectContaining({ includeHeightReporter: true }),
    )
  })

  it('sizes the frame to the height its document reports', async () => {
    const frame = await mountFrame('<p>sized</p>')
    report(frame, 742)
    await waitFor(() => expect(frame.style.height).toBe('742px'))
  })

  it('honors a reported height well below the fixed box it replaced', async () => {
    // THE regression guard. The frame used to be `calc(100vh - 240px)` with
    // `minHeight: 480`, so this document's 385px of content sat in a frame
    // hundreds of pixels taller than itself — the exact condition iOS refuses to
    // paint. A floor reintroduced anywhere above the reported height brings the
    // blank box straight back, and only an iOS device would notice.
    const frame = await mountFrame('<p>short</p>')
    report(frame, 385)
    await waitFor(() => expect(frame.style.height).toBe('385px'))
    expect(frame.style.minHeight).toBe('')
  })

  it('ignores a height posted by a window that is not its own frame', async () => {
    // The document is built from model- or user-authored HTML and its scripts can
    // postMessage the parent directly, so an unrelated sender must not be able to
    // resize this frame.
    const frame = await mountFrame('<p>foreign</p>')
    report(frame, 640)
    await waitFor(() => expect(frame.style.height).toBe('640px'))
    report(frame, 4000, window)
    expect(frame.style.height).toBe('640px')
  })

  it('refuses to collapse the frame on a nonsense report', async () => {
    // A collapsed frame is unrecoverable from the reader's side, so a report of
    // zero — or of anything that is not a finite number — must not be applied as
    // given.
    const frame = await mountFrame('<p>floor</p>')
    report(frame, 600)
    await waitFor(() => expect(frame.style.height).toBe('600px'))
    report(frame, 0)
    await waitFor(() => expect(frame.style.height).toBe('80px'))
    report(frame, Number.NaN)
    expect(frame.style.height).toBe('80px')
    report(frame, 'tall')
    expect(frame.style.height).toBe('80px')
  })

  it('gives the frame its own compositing layer so its first paint is not skipped', async () => {
    // iOS WebKit was measured laying this document out and then never
    // rasterizing it: it loaded, its scripts ran, it reported a correct layout
    // height, and it sat in a correctly sized VISIBLE frame while painting
    // nothing. Four unrelated post-load invalidations each made it appear, so
    // the frame is promoted up front instead — the only remedy of the four that
    // does not depend on firing at the right moment after load.
    //
    // Only an iOS device can observe the regression, so this assertion is the
    // whole guard: dropping the property looks completely harmless in Chromium.
    const frame = await mountFrame('<p>promoted</p>')
    expect(frame.style.transform).toBe('translateZ(0)')
  })

  it('refuses to blow the page up on a huge report', async () => {
    // Not a hostile-input guard — a self-sizing frame feeds its own measurement
    // when the document's height depends on the frame's viewport, and a multiplier
    // above 1 diverges. Unbounded growth has no natural stopping point.
    const frame = await mountFrame('<p>ceiling</p>')
    report(frame, 1e9)
    await waitFor(() => expect(frame.style.height).toBe('100000px'))
  })

  it('leaves the side panel fitting its pane instead of its content', async () => {
    // The side panel passes an explicit heightStyle because that surface fits a
    // fixed pane and scrolls inside it. Content-height sizing must not reach in
    // and override a caller that asked for a specific box.
    render(
      <ArtifactBodyIframe
        artifact={makeArtifact('<p>panel</p>')}
        heightStyle={{ height: '100%' }}
      />,
    )
    const frame = await waitFor(() => {
      const el = document.querySelector('iframe')
      if (!el) throw new Error('frame never mounted')
      return el as HTMLIFrameElement
    })
    fireEvent.load(frame)
    report(frame, 385)
    expect(frame.style.height).toBe('100%')
  })
})

// The document URL is single-use: the gateway spends it on the first GET. So a
// navigation the ENGINE starts on its own — memory pressure, a back/forward
// cache eviction — re-requests a spent URL and lands the frame on a 404 page,
// which fires `load` like any other navigation. Nothing about that reaches the
// mint, so `failed` stays false and the reader is left with a silent empty box
// and no way out. Every document this surface builds carries the injected height
// reporter, so silence is the signal.
describe('ArtifactBodyIframe surfaces a frame showing something that is not ours', () => {
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

  async function loadedFrame(content: string): Promise<HTMLIFrameElement> {
    render(<ArtifactBodyIframe artifact={makeArtifact(content)} />)
    const frame = await waitFor(() => {
      const el = document.querySelector('iframe')
      if (!el) throw new Error('frame never mounted')
      return el as HTMLIFrameElement
    })
    fireEvent.load(frame)
    return frame
  }

  function reportHeight(frame: HTMLIFrameElement, height: number): void {
    fireEvent(window, new MessageEvent('message', {
      data: { type: 'mc-widget-height', height },
      source: frame.contentWindow as MessageEventSource,
    }))
  }

  it('offers a Show artifact action when the loaded document never reports its height', async () => {
    await loadedFrame('<p>silent</p>')
    expect(screen.queryByText(/no longer showing/i)).toBeNull()

    await act(async () => { vi.advanceTimersByTime(4000) })

    // Cause-neutral copy: from outside the opaque sandbox a spent-url 404 is
    // indistinguishable from the reader following a link inside the artifact,
    // so the notice must not claim a render failure (#6489).
    expect(screen.getByText(/this artifact is no longer showing/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /show artifact/i })).toBeTruthy()
    expect(screen.queryByText(/couldn't render this artifact/i)).toBeNull()
    // The frame stays mounted: with a document still showing, the notice overlays
    // it rather than replacing what the reader may still be able to see.
    expect(document.querySelector('iframe')).toBeTruthy()
  })

  it('stays quiet when the document does report', async () => {
    // The whole guard hinges on our own documents always reporting. If a healthy
    // document could trip this, the surface would cry wolf on every open.
    const frame = await loadedFrame('<p>healthy</p>')
    reportHeight(frame, 420)

    await act(async () => { vi.advanceTimersByTime(4000) })

    expect(screen.queryByText(/no longer showing/i)).toBeNull()
    expect(screen.queryByText(/couldn't render this artifact/i)).toBeNull()
  })

  it('offers a Show artifact action when the engine renavigates a spent url after a good render', async () => {
    // THE case this feature exists for, and the one an earlier version silently
    // skipped: the document rendered and reported, then the engine navigated the
    // frame again on its own (a back/forward-cache eviction) and re-requested a
    // single-use url, landing on a 404. Keying the observation on the url meant a
    // previous document's report suppressed the window exactly when it mattered.
    const frame = await loadedFrame('<p>evicted</p>')
    reportHeight(frame, 420)
    await act(async () => { vi.advanceTimersByTime(4000) })
    expect(screen.queryByText(/no longer showing/i)).toBeNull()

    // Same url, second load — nothing reports this time, because what the frame
    // is showing is not ours.
    fireEvent.load(frame)
    await act(async () => { vi.advanceTimersByTime(4000) })
    expect(screen.getByText(/this artifact is no longer showing/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /show artifact/i })).toBeTruthy()
  })

  it('re-mints rather than re-rendering the spent url when Show artifact is taken', async () => {
    // Re-pointing the frame at the same spent URL recovers nothing — it 404s
    // again. Recovery is a fresh mint.
    await loadedFrame('<p>silent</p>')
    await act(async () => { vi.advanceTimersByTime(4000) })

    mintSpy.mockResolvedValue({ url: '/sandbox-doc/fresh/tok' })
    screen.getByRole('button', { name: /show artifact/i }).click()

    await waitFor(() => {
      expect(document.querySelector('iframe')?.getAttribute('src'))
        .toBe('/sandbox-doc/fresh/tok')
    })
    expect(mintSpy).toHaveBeenCalledTimes(2)
    expect(screen.queryByText(/no longer showing/i)).toBeNull()
  })

  it('renders DIFFERENT copy for a failed mint than for a frame that stopped showing', async () => {
    // The defect in #6489 was one notice for two states: `failed` (the mint
    // itself failed — a failure claim is accurate) and `docSilent` (the frame
    // may simply be showing a page the reader chose to open — a failure claim
    // is a lie half the time). This pins the split: merging the two branches
    // back into one message must red this test.
    const silent = render(<ArtifactBodyIframe artifact={makeArtifact('<p>silent</p>')} />)
    const frame = await waitFor(() => {
      const el = document.querySelector('iframe')
      if (!el) throw new Error('frame never mounted')
      return el as HTMLIFrameElement
    })
    fireEvent.load(frame)
    await act(async () => { vi.advanceTimersByTime(4000) })
    const silentMessage = screen.getByText(/no longer showing/i).textContent
    const silentAction = screen.getByRole('button', { name: /show artifact/i }).textContent
    silent.unmount()

    mintSpy.mockRejectedValueOnce(new Error('gateway said no'))
    render(<ArtifactBodyIframe artifact={makeArtifact('<p>doomed</p>')} />)
    const failedMessage = (await screen.findByText(/couldn't render this artifact/i)).textContent
    const failedAction = screen.getByRole('button', { name: /retry/i }).textContent

    expect(silentMessage).not.toBe(failedMessage)
    expect(silentAction).not.toBe(failedAction)
  })

  it('lets failed win when both states are set at once', async () => {
    // Reachable state: with the silent notice up, a content change re-mints and
    // the mint FAILS. The previous url survives a failed mint (see
    // useSandboxDoc), so no new `load` fires and docSilent stays set while
    // failed turns on. A known failed mint is the more specific diagnosis, so
    // its copy must win — inverting the notice ternaries to key on docSilent
    // would keep the split but regress this order.
    const view = render(<ArtifactBodyIframe artifact={makeArtifact('<p>silent</p>')} />)
    const frame = await waitFor(() => {
      const el = document.querySelector('iframe')
      if (!el) throw new Error('frame never mounted')
      return el as HTMLIFrameElement
    })
    fireEvent.load(frame)
    await act(async () => { vi.advanceTimersByTime(4000) })
    expect(screen.getByText(/no longer showing/i)).toBeTruthy()

    mintSpy.mockRejectedValueOnce(new Error('gateway said no'))
    view.rerender(<ArtifactBodyIframe artifact={makeArtifact('<p>silent v2</p>')} />)

    await screen.findByText(/couldn't render this artifact/i)
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
    expect(screen.queryByText(/no longer showing/i)).toBeNull()
    // Pin the premise: docSilent is genuinely still set here, not cleared. A
    // second mint was attempted and NO new url landed (same iframe src, no new
    // load), so the only docSilent-clearing path — a blobUrl change — did not
    // run. Without these, the assertion above is equally satisfied by a code
    // path that clears docSilent whenever failed turns on, and the test would
    // green as a tautology.
    expect(mintSpy).toHaveBeenCalledTimes(2)
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe(DOC_URL)
    // The document the reader may still be looking at stays mounted throughout.
    expect(document.querySelector('iframe')).toBeTruthy()
  })

  it('keeps the notice when a re-mint returns the same spent url', async () => {
    // The recovery path with nothing to recover: the gateway can mint the same
    // url string for the same html, which is a React no-op — no src change, no
    // new load, nothing ever re-arms the silence window. If the click cleared
    // docSilent, this outcome would leave the reader on a dead frame with NO
    // affordance at all, strictly worse than before the click. The click is
    // acknowledged by disabling the button while the mint is in flight, never
    // by hiding the notice.
    await loadedFrame('<p>silent</p>')
    await act(async () => { vi.advanceTimersByTime(4000) })
    expect(screen.getByText(/no longer showing/i)).toBeTruthy()

    // Same-url outcome under explicit settlement control: the default mock
    // settles inside a single act() flush, which would make the settle
    // unobservable as a transition.
    let settle: (v: { url: string }) => void = () => {}
    mintSpy.mockImplementationOnce(() => new Promise((res) => { settle = res }))
    const button = screen.getByRole('button', { name: /show artifact/i }) as HTMLButtonElement
    button.click()
    // Flush the passive effect first: the mint (and with it the deferred
    // resolver) does not exist until the [srcdoc, attempt] effect runs.
    await act(async () => { await Promise.resolve() })
    await act(async () => { settle({ url: DOC_URL }); await Promise.resolve() })
    // The re-enable proves the settle happened (the disabled→enabled
    // transition itself is pinned by the dedicated test below); if a
    // regression cleared docSilent on a same-url settle, the notice unmounts
    // and this detached node keeps disabled=true, so this reds rather than
    // passing on a null query.
    await waitFor(() => expect(button.disabled).toBe(false))

    expect(mintSpy).toHaveBeenCalledTimes(2)
    expect(document.querySelector('iframe')?.getAttribute('src')).toBe(DOC_URL)
    // The notice survives the SETTLE: it is the only affordance the reader has
    // left, and the re-enabled button proves pending is not stuck either way.
    expect(screen.getByText(/no longer showing/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /show artifact/i })).toBeTruthy()
  })

  it('disables the action while the re-mint is in flight', async () => {
    // The acknowledgment for the click: the button visibly cannot be pressed
    // again until the attempt settles, instead of looking inert (or worse,
    // hiding the notice before anything about the frame has changed).
    await loadedFrame('<p>silent</p>')
    await act(async () => { vi.advanceTimersByTime(4000) })

    let settle: (v: { url: string }) => void = () => {}
    mintSpy.mockImplementationOnce(() => new Promise((res) => { settle = res }))
    const button = screen.getByRole('button', { name: /show artifact/i }) as HTMLButtonElement
    button.click()
    await act(async () => { await Promise.resolve() })
    expect(button.disabled).toBe(true)

    // Settle with the SAME url so the notice stays mounted, and assert the
    // re-enable on the node held from before the click — settling with a fresh
    // url would unmount the notice and let a stuck-pending regression pass on
    // a null query.
    await act(async () => { settle({ url: DOC_URL }); await Promise.resolve() })
    expect(button.disabled).toBe(false)
    expect(screen.getByText(/no longer showing/i)).toBeTruthy()
  })
})
