/**
 * Mochi chat panel — the paths `MochiChatPanel.coverage.test.tsx` leaves alone.
 *
 * That file covers the panel's headline flows (send, slash commands, approvals,
 * the destructive confirmations). This one takes the parts that only appear once
 * the panel is put under conditions a no-layout test environment does not supply
 * by itself:
 *
 *  - the two lazy-history loaders (the "content does not fill the container"
 *    auto-load, and the sentinel becoming visible again) plus the pre-paint
 *    scroll correction that keeps the reading position steady when messages are
 *    PREPENDED — which needs a scroll geometry, so the metrics are stubbed;
 *  - the floating stop capsule's measured offset, which comes from a
 *    ResizeObserver on the bottom stack;
 *  - the scroll-position pill, which needs a scroller that is not at its end;
 *  - the markdown component table the reply renderer installs (tables, lists,
 *    inline code, remote vs. local images) and the widget branches of both the
 *    streaming and the committed renderer;
 *  - `relativeTime`'s whole ladder, from "now" to an absolute date;
 *  - the timer-driven affordances: the rotating placeholder, the copy
 *    confirmation reverting, and the gateway-start button's 8s settle window.
 *
 * Timers: the panel runs a 10s placeholder interval and several deferred state
 * updates, so every test that needs to jump forward installs fake timers with
 * `shouldAdvanceTime` (so awaited work still progresses) and `afterEach` drops
 * whatever is still queued — an orphan timer firing after teardown would make
 * the run exit non-zero with every test green, and no coverage report written.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

type AnyFn = (...args: never[]) => unknown

/** Captured `on*` subscribers, keyed by the api method that registered them. */
const subscribers = new Map<string, Set<AnyFn>>()

function subscribe(channel: string) {
  return (cb: AnyFn) => {
    const set = subscribers.get(channel) ?? new Set<AnyFn>()
    set.add(cb)
    subscribers.set(channel, set)
    return () => { set.delete(cb) }
  }
}

/** Push a backend frame to whatever the panel registered on `channel`. */
function emit(channel: string, ...args: unknown[]): void {
  for (const cb of Array.from(subscribers.get(channel) ?? [])) {
    ;(cb as (...a: unknown[]) => unknown)(...args)
  }
}

/** Chat history handed back by `getChatHistory`; set per test before render. */
let history: unknown[] = []
/** Initial backend reachability, so the offline banner can be exercised. */
let backendOnline = true

const sendMessage = vi.fn(async () => undefined)
const stopGeneration = vi.fn(async () => undefined)
const retryConnect = vi.fn(async () => ({ ok: true } as { ok: boolean; message?: string }))
const resetMochi = vi.fn(async () => undefined)
const respondApproval = vi.fn(async () => undefined as unknown)
const openLightbox = vi.fn()
const previewFile = vi.fn()
const unpinFile = vi.fn()
const markPinnedSeen = vi.fn()
const openWidgetExternal = vi.fn()
const readLocalImage = vi.fn(async (_path: string): Promise<string | null> => null)

// The reveal action on a file chip exists only inside the Electron shell (it
// delegates to the shell bridge); these tests exercise that surface.
vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  isElectron: true,
}))

vi.mock('../apps/mochi/src/mochiApi', () => ({
  api: {
    getMochiConfig: async () => ({ petName: 'Mochi', theme: 'mocha' }),
    getConfig: async () => ({
      shortcuts: {
        toggleWindow: 'CommandOrControl+Shift+M',
        screenCapture: 'CommandOrControl+Shift+X',
        hideAll: 'CommandOrControl+Shift+H',
      },
    }),
    getPetStateInfo: async () => ({ state: 'idle', mood: 'neutral' }),
    getChatHistory: async () => history,
    getBackendStatus: async () => backendOnline,
    onStateChange: subscribe('onStateChange'),
    onMood: subscribe('onMood'),
    onPeeking: subscribe('onPeeking'),
    onConfigUpdated: subscribe('onConfigUpdated'),
    onChatChunk: subscribe('onChatChunk'),
    onChatDone: subscribe('onChatDone'),
    onChatMessage: subscribe('onChatMessage'),
    onBackendStatus: subscribe('onBackendStatus'),
    onBackendSwitching: subscribe('onBackendSwitching'),
    onSlotsUpdate: subscribe('onSlotsUpdate'),
    onCaptureDone: subscribe('onCaptureDone'),
    onApprovalRequest: subscribe('onApprovalRequest'),
    onApprovalResolvedExternal: subscribe('onApprovalResolvedExternal'),
    onThemeChanged: subscribe('onThemeChanged'),
    onContextUsage: subscribe('onContextUsage'),
    sendMessage,
    stopGeneration,
    retryConnect,
    resetMochi,
    respondApproval,
    openLightbox,
    previewFile,
    revealFile: vi.fn(),
    unpinFile,
    markPinnedSeen,
    openExternal: vi.fn(),
    openWidgetExternal,
    readLocalImage,
    closeChat: vi.fn(),
    openSettings: vi.fn(),
    galleryOpen: vi.fn(),
    openDashboard: vi.fn(),
    newSession: vi.fn(async () => undefined),
    editResend: vi.fn(async () => ({ ok: true })),
    deleteHistory: vi.fn(async () => undefined),
  },
}))

const { ChatPanel, PinnedSidePanel } = await import('../apps/mochi/src/renderer/ChatPanel')

/** Property overrides installed for one test, undone by `afterEach`. */
const restores: (() => void)[] = []

/**
 * Replace a prototype accessor (`scrollHeight`, `offsetHeight`, …) for one test.
 *
 * The environment has no layout, so every metric the panel reads is 0 and the
 * geometry-dependent branches are unreachable. Overriding the accessor is the
 * only way to give the component a scroller that is taller than its viewport.
 */
function stubMetric(proto: object, name: string, get: () => number): void {
  const original = Object.getOwnPropertyDescriptor(proto, name)
  Object.defineProperty(proto, name, { configurable: true, get })
  restores.push(() => {
    if (original) Object.defineProperty(proto, name, original)
    else delete (proto as Record<string, unknown>)[name]
  })
}

/**
 * Record every scroll position assigned to any element, for one test.
 *
 * The panel writes `scrollTop` from several places (the post-load jump to the
 * end, and the pre-paint correction under test), so reading the final value
 * proves nothing about which write happened. The log does.
 */
function recordScrollTops(): number[] {
  const written: number[] = []
  let current = 0
  const original = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop')
  Object.defineProperty(Element.prototype, 'scrollTop', {
    configurable: true,
    get: () => current,
    set: (v: number) => { current = v; written.push(v) },
  })
  restores.push(() => {
    if (original) Object.defineProperty(Element.prototype, 'scrollTop', original)
    else delete (Element.prototype as unknown as Record<string, unknown>).scrollTop
  })
  return written
}

/** Replace a global (IntersectionObserver, ResizeObserver) for one test. */
function stubGlobal(name: string, value: unknown): void {
  const holder = globalThis as unknown as Record<string, unknown>
  const original = holder[name]
  holder[name] = value
  restores.push(() => { holder[name] = original })
}

beforeEach(() => {
  vi.clearAllMocks()
  subscribers.clear()
  history = []
  backendOnline = true
  sendMessage.mockResolvedValue(undefined)
  retryConnect.mockResolvedValue({ ok: true })
  readLocalImage.mockResolvedValue(null)
})

afterEach(() => {
  // Drop deferred work before the DOM goes away: the panel queues 8s and 1.5s
  // updates, and one landing after teardown fails the run on its own.
  vi.clearAllTimers()
  vi.useRealTimers()
  while (restores.length > 0) restores.pop()!()
})

/** Render the panel and wait until the mount-time config reads have settled. */
async function renderPanel(props: Partial<React.ComponentProps<typeof ChatPanel>> = {}) {
  const view = render(<ChatPanel {...props} />)
  await screen.findByText(/Idle/)
  return view
}

/** The panel's composer. */
function composer(): HTMLTextAreaElement {
  return screen.getByPlaceholderText(/./) as HTMLTextAreaElement
}

/**
 * A turn, in the shape both channels use.
 *
 * The id matters for the live channel (`chat:message`) — the panel keys the
 * bubble and its entry animation off it. History entries are re-keyed on load,
 * so the same helper serves both.
 */
function turn(role: string, content: string, timestamp = 1700000000000) {
  return { id: `t-${role}-${timestamp}-${content.length}`, role, content, timestamp }
}

describe('ChatPanel lazy history', () => {
  it('pulls in earlier turns until the transcript fills the scroller', async () => {
    // 25 turns, but only 10 render at first. With nothing filling the viewport
    // the panel keeps asking for more rather than leaving the user with a short
    // transcript and no way to notice there is more.
    history = Array.from({ length: 25 }, (_, i) => turn('user', `turn ${i}`, 1700000000000 + i))
    await renderPanel()
    expect(await screen.findByText('turn 24')).toBeInTheDocument()
    expect(screen.queryByText('turn 0')).not.toBeInTheDocument()
    expect(await screen.findByText('turn 0', {}, { timeout: 5000 })).toBeInTheDocument()
    // Everything is loaded, so the load-earlier affordance is gone.
    expect(screen.queryByText(/Load earlier messages/)).not.toBeInTheDocument()
  })

  it('keeps the reading position steady when the sentinel pulls older turns in', async () => {
    // A scroller that is taller than its viewport, so the auto-loader above
    // stays out of the way and the sentinel path is the one under test. Height
    // grows with the number of rendered rows, which is what makes the
    // pre-paint correction observable.
    stubMetric(Element.prototype, 'clientHeight', () => 0)
    stubMetric(Element.prototype, 'scrollHeight', function (this: Element) {
      return this.childElementCount * 100
    })

    const observers: ((entries: unknown[]) => void)[] = []
    stubGlobal('IntersectionObserver', class {
      constructor(private readonly cb: (entries: unknown[]) => void) { observers.push(cb) }
      observe() { this.cb([{ isIntersecting: true }]) }
      unobserve() {}
      disconnect() {}
      takeRecords() { return [] }
    })

    history = Array.from({ length: 25 }, (_, i) => turn('user', `turn ${i}`, 1700000000000 + i))
    const { container } = await renderPanel()
    await screen.findByText('turn 24')
    const scroller = container.querySelector('.chat-scroll') as HTMLElement
    expect(screen.queryByText('turn 14')).not.toBeInTheDocument()

    const before = scroller.scrollHeight
    const scrollTops = recordScrollTops()
    act(() => { observers.forEach(cb => cb([{ isIntersecting: true }])) })
    expect(await screen.findByText('turn 14')).toBeInTheDocument()
    // The prepended rows made the content taller; the panel pushes the scroll
    // position down by exactly that much so the turn the user was reading does
    // not jump off screen.
    const grew = scroller.scrollHeight - before
    expect(grew).toBeGreaterThan(0)
    expect(scrollTops).toContain(grew)

    // The in-flight guard is released once the prepend has settled, so scrolling
    // back up again keeps walking the history instead of stopping at one page.
    await waitFor(async () => {
      act(() => { observers.forEach(cb => cb([{ isIntersecting: true }])) })
      expect(await screen.findByText('turn 4')).toBeInTheDocument()
    }, { timeout: 5000 })
  })
})

describe('ChatPanel scroll position pill', () => {
  it('offers a way back to the latest turn once the user scrolls away from it', async () => {
    stubMetric(Element.prototype, 'clientHeight', () => 0)
    stubMetric(Element.prototype, 'scrollHeight', () => 1000)
    history = [turn('assistant', 'older answer')]
    const { container } = await renderPanel()
    await screen.findByText('older answer')
    const scroller = container.querySelector('.chat-scroll') as HTMLElement
    const scrollTo = vi.fn()
    scroller.scrollTo = scrollTo as unknown as typeof scroller.scrollTo

    expect(screen.queryByRole('button', { name: 'Scroll to Latest' })).not.toBeInTheDocument()
    // The panel re-pins the scroller to the end on 50/150/300ms timers once the
    // history lands, so a plain `scrollTop` write is not a state this test can
    // rely on: a timer that runs after the write puts the scroller back AT the
    // bottom, the scroll below reads as "still at the latest turn" and the pill
    // never renders. Park the scroller away from the end with an own accessor —
    // the getter pins the reported position mid-scroll and the setter swallows
    // the panel's re-pin writes (a plain no-setter property would make those
    // strict-mode writes throw inside the panel's timers). The element is
    // per-test, so the override dies with it and needs no restore.
    Object.defineProperty(scroller, 'scrollTop', {
      configurable: true,
      get: () => 500,
      set: () => {},
    })
    fireEvent.scroll(scroller)
    // React handles scroll at continuous (non-sync) priority, so the pill's
    // commit is scheduled rather than flushed inside `fireEvent`. On a loaded
    // CI runner that scheduling can outlive the default 1s query budget; give
    // the appearance the same generous ceiling the file's other timing-driven
    // assertions use.
    const pill = await screen.findByRole(
      'button', { name: 'Scroll to Latest' }, { timeout: 5000 },
    )

    // The pill dims under the pointer, which is how it reads as pressable.
    await userEvent.hover(pill)
    expect(pill.style.opacity).toBe('0.85')
    await userEvent.unhover(pill)
    expect(pill.style.opacity).toBe('1')

    await userEvent.click(pill)
    expect(scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: 'smooth' })
  })
})

describe('ChatPanel floating stop capsule', () => {
  it('lifts clear of the bottom stack as the composer grows', async () => {
    let stackHeight = 0
    stubMetric(HTMLElement.prototype, 'offsetHeight', () => stackHeight)
    const observers: (() => void)[] = []
    stubGlobal('ResizeObserver', class {
      constructor(private readonly cb: () => void) { observers.push(() => this.cb()) }
      observe() {}
      unobserve() {}
      disconnect() {}
    })

    await renderPanel()
    emit('onChatMessage', turn('user', 'do a thing'))
    const stop = await screen.findByRole('button', { name: /Stop/ })
    const floating = stop.parentElement as HTMLElement
    // Nothing measured yet: the fallback keeps the capsule off the composer.
    expect(floating.style.bottom).toBe('62px')

    stackHeight = 80
    act(() => { observers.forEach(fire => fire()) })
    // Re-measured, so a grown composer cannot end up underneath the capsule.
    await waitFor(() => expect(floating.style.bottom).toBe('98px'))
  })
})

describe('ChatPanel context menu dismissal', () => {
  it('leaves the text selection alone instead of hijacking the right-click', async () => {
    const { container } = await renderPanel()
    const selection = vi.spyOn(window, 'getSelection').mockReturnValue({
      toString: () => 'some selected words',
    } as unknown as Selection)
    try {
      fireEvent.contextMenu(container.firstChild as HTMLElement, { clientX: 5, clientY: 5 })
      // The browser's own copy menu belongs to a selection; the pet menu would
      // replace it and there would be no way to copy the transcript.
      expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    } finally {
      selection.mockRestore()
    }
  })

  it('closes the menu on the next click anywhere in the panel', async () => {
    const { container } = await renderPanel()
    const shell = container.firstChild as HTMLElement
    fireEvent.contextMenu(shell, { clientX: 5, clientY: 5 })
    await screen.findByRole('menu')
    fireEvent.click(shell)
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
  })
})

describe('ChatPanel reset confirmation', () => {
  it('cancels without resetting anything', async () => {
    history = [turn('user', 'kept turn')]
    const { container } = await renderPanel()
    await screen.findByText('kept turn')
    fireEvent.contextMenu(container.firstChild as HTMLElement, { clientX: 5, clientY: 5 })
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Reset Mochi' }))
    await screen.findByText('Reset \u201cMochi\u201d?')
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByText('Reset \u201cMochi\u201d?')).not.toBeInTheDocument())
    expect(resetMochi).not.toHaveBeenCalled()
    expect(screen.getByText('kept turn')).toBeInTheDocument()
  })
})

describe('ChatPanel gateway start', () => {
  it('reports the start as under way and re-offers it once the window lapses', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    backendOnline = false
    await renderPanel()
    const start = await screen.findByRole('button', { name: 'Start Kiro Crew' }, { timeout: 5000 })
    retryConnect.mockResolvedValueOnce({ ok: true })
    await userEvent.click(start)

    // Two separate surfaces: the button is replaced by a progress label, and the
    // message line explains what is happening.
    expect(await screen.findByText('Connecting...')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start Kiro Crew' })).not.toBeInTheDocument()

    // The socket never connects, so the panel gives up waiting and lets the user
    // try again rather than sitting on "Connecting..." forever.
    await act(async () => { await vi.advanceTimersByTimeAsync(8000) })
    expect(await screen.findByRole('button', { name: 'Start Kiro Crew' })).toBeInTheDocument()
  })
})

describe('ChatPanel screen capture', () => {
  /** Answer the upload route without touching the network. */
  function stubUpload(status: number, body: unknown) {
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response(JSON.stringify(body), { status }),
    )
    restores.push(() => spy.mockRestore())
    return spy
  }

  it('keeps the crop and says why it could not be attached', async () => {
    stubUpload(415, { error: 'Unsupported file type' })
    const { container } = await renderPanel()
    // 'QUJD' is base64 for 'ABC' — cropToFile runs it through atob.
    emit('onCaptureDone', 'QUJD')
    expect(await screen.findByText('Unsupported file type')).toBeInTheDocument()
    // Losing the capture outright would be worse than showing it locally only.
    await waitFor(() =>
      expect(container.querySelector('img[src="data:image/png;base64,QUJD"]')).not.toBeNull(),
    )
  })

  it('opens the kept crop in the lightbox', async () => {
    stubUpload(200, { paths: [] })
    const { container } = await renderPanel()
    emit('onCaptureDone', 'QUJD')
    const preview = await waitFor(() => {
      const found = container.querySelector('img[src="data:image/png;base64,QUJD"]')
      expect(found).not.toBeNull()
      return found as HTMLImageElement
    })
    // The dismiss dot is revealed by hovering the thumbnail.
    const wrapper = preview.parentElement as HTMLElement
    fireEvent.mouseEnter(wrapper)
    expect((wrapper.querySelector('.x-btn') as HTMLElement).style.opacity).toBe('1')
    fireEvent.mouseLeave(wrapper)
    expect((wrapper.querySelector('.x-btn') as HTMLElement).style.opacity).toBe('0')

    await userEvent.click(preview)
    expect(openLightbox).toHaveBeenCalledWith('QUJD')
  })

  it('attaches an image pasted into the composer', async () => {
    stubUpload(200, { paths: ['/home/u/uploads/pasted.png'] })
    await renderPanel()
    const file = new File(['x'], 'pasted.png', { type: 'image/png' })
    fireEvent.paste(composer(), {
      clipboardData: {
        items: [{ kind: 'file', type: file.type, getAsFile: () => file }],
        files: [file],
        types: ['Files'],
      },
    })
    // The strip records what will be sent; the typed text stays clean.
    expect(await screen.findByAltText('pasted.png')).toBeInTheDocument()
    expect(composer()).toHaveValue('')
  })
})

describe('ChatPanel streaming renderer', () => {
  it('paints buffered chunks on the next frame', async () => {
    await renderPanel()
    // No done frame: the chunk has to reach the bubble through the throttle on
    // its own, which is what the user sees mid-answer.
    emit('onChatChunk', 'thinking out loud')
    expect(await screen.findByText('thinking out loud')).toBeInTheDocument()
  })

  it('drops a buffered chunk when the panel goes away mid-answer', async () => {
    const view = await renderPanel()
    emit('onChatChunk', 'half a thought')
    // Unmounting with a frame still pending must not leave it to fire into a
    // dead component.
    view.unmount()
    expect(screen.queryByText('half a thought')).not.toBeInTheDocument()
  })

  it('renders a completed widget and keeps streaming the text after it', async () => {
    await renderPanel()
    emit('onChatChunk', 'Here it is <mcwidget title="Chart">bars</mcwidget> and more to come')
    emit('onChatDone')
    expect(await screen.findByTitle('Chart')).toBeInTheDocument()
    expect(screen.getByText('Here it is')).toBeInTheDocument()
    expect(screen.getByText(/and more to come/)).toBeInTheDocument()
    expect(screen.queryByText(/mcwidget/)).not.toBeInTheDocument()
  })

  it('hands a streamed widget to the OS browser on request', async () => {
    await renderPanel()
    emit('onChatChunk', '<mcwidget title="Chart">bars</mcwidget>')
    emit('onChatDone')
    await screen.findByTitle('Chart')
    await userEvent.click(screen.getByRole('button', { name: 'Open in browser' }))
    expect(openWidgetExternal).toHaveBeenCalledTimes(1)
    expect(openWidgetExternal.mock.calls[0][1]).toBe('Chart')
  })
})

describe('ChatPanel reply markdown', () => {
  it('renders a table with the panel cell metrics', async () => {
    history = [turn('assistant', '| Env | State |\n| --- | --- |\n| beta | green |')]
    await renderPanel()
    const header = await screen.findByText('Env')
    expect(header.tagName).toBe('TH')
    expect(header.style.fontWeight).toBe('600')
    const cell = screen.getByText('beta')
    expect(cell.tagName).toBe('TD')
    expect(cell.style.padding).toBe('3px 6px')
    expect(cell.closest('table')?.style.borderCollapse).toBe('collapse')
  })

  it('renders both list flavours with the panel spacing', async () => {
    history = [turn('assistant', '- first\n- second\n\n1. step one\n2. step two')]
    await renderPanel()
    const bullet = await screen.findByText('first')
    expect(bullet.tagName).toBe('LI')
    expect(bullet.closest('ul')?.style.paddingLeft).toBe('16px')
    expect(screen.getByText('step one').closest('ol')?.style.paddingLeft).toBe('16px')
  })

  it('leaves inline code that is not a file path as code', async () => {
    history = [turn('assistant', 'Run `npm test` and wait.')]
    await renderPanel()
    const code = await screen.findByText('npm test')
    expect(code.tagName).toBe('CODE')
    // A path would have become a chip with preview/reveal buttons instead.
    expect(screen.queryByRole('button', { name: 'Preview' })).not.toBeInTheDocument()
  })

  it('leaves a remote image for the browser to load', async () => {
    // `.svg` is outside the extension list the reply renderer lifts paths out
    // with, so this reference survives into the markdown image component — the
    // one place the local-vs-remote decision is actually made.
    history = [turn('assistant', '![banner](https://example.com/banner.svg)')]
    const { container } = await renderPanel()
    await waitFor(() =>
      expect(container.querySelector('img[src="https://example.com/banner.svg"]')).not.toBeNull(),
    )
    // Nothing local about it, so the app-api reader is not involved.
    expect(readLocalImage).not.toHaveBeenCalled()
  })

  it('reads an on-disk image through the app api even for an unlisted extension', async () => {
    // A bare `<img src="/home/...">` would be resolved against the gateway
    // origin and 404, so any absolute path has to go through the reader.
    readLocalImage.mockResolvedValue('QUJD')
    history = [turn('assistant', '![diagram](/home/u/diagram.svg)')]
    const { container } = await renderPanel()
    await waitFor(() => expect(readLocalImage).toHaveBeenCalledWith('/home/u/diagram.svg'))
    expect(container.querySelector('img[src="/home/u/diagram.svg"]')).toBeNull()
    const img = container.querySelector('img[src^="data:image/png;base64,"]') as HTMLImageElement
    await userEvent.click(img)
    expect(openLightbox).toHaveBeenCalledWith('/home/u/diagram.svg')
  })

  it('renders a widget in a committed reply alongside its images', async () => {
    readLocalImage.mockResolvedValue('QUJD')
    history = [
      turn('assistant', 'Chart below\n<mcwidget title="Usage">bars</mcwidget>\n/home/u/shot.png'),
    ]
    await renderPanel()
    expect(await screen.findByTitle('Usage')).toBeInTheDocument()
    expect(screen.getByText('Chart below')).toBeInTheDocument()
    // The bare path is lifted out of the text and rendered as the image itself.
    await waitFor(() => expect(readLocalImage).toHaveBeenCalledWith('/home/u/shot.png'))
    expect(screen.queryByText(/shot\.png/)).not.toBeInTheDocument()
  })

  it('names the widget generically when the model gave it no title', async () => {
    history = [turn('assistant', '<mcwidget>bars</mcwidget>')]
    await renderPanel()
    expect(await screen.findByTitle('Widget')).toBeInTheDocument()
  })
})

describe('ChatPanel user message content', () => {
  it('renders a markdown image the user typed as the image', async () => {
    readLocalImage.mockResolvedValue('QUJD')
    history = [turn('user', '![my shot](/home/u/typed.png)')]
    await renderPanel()
    await waitFor(() => expect(readLocalImage).toHaveBeenCalledWith('/home/u/typed.png'))
  })

  it('opens a captured screenshot carried on the message', async () => {
    await renderPanel()
    emit('onChatMessage', { ...turn('user', 'what is this'), id: 'm-ss', screenshot: 'QUJD' })
    const shot = await waitFor(() => {
      const found = document.querySelector('img[src="data:image/png;base64,QUJD"]')
      expect(found).not.toBeNull()
      return found as HTMLImageElement
    })
    await userEvent.click(shot)
    expect(openLightbox).toHaveBeenCalledWith('QUJD')
  })
})

describe('ChatPanel timestamps', () => {
  it('ages each turn from "now" up to an absolute date', async () => {
    const now = Date.now()
    const min = 60_000
    history = [
      turn('assistant', 'just landed', now),
      turn('assistant', 'half a minute', now - 30_000),
      turn('assistant', 'five minutes', now - 5 * min),
      turn('assistant', 'three hours', now - 3 * 60 * min),
      turn('assistant', 'two days', now - 2 * 24 * 60 * min),
      turn('assistant', 'last month', now - 40 * 24 * 60 * min),
    ]
    await renderPanel()
    await screen.findByText('just landed')
    expect(screen.getByText('now')).toBeInTheDocument()
    expect(screen.getByText('30 seconds ago')).toBeInTheDocument()
    expect(screen.getByText('5 minutes ago')).toBeInTheDocument()
    expect(screen.getByText('3 hours ago')).toBeInTheDocument()
    expect(screen.getByText('2 days ago')).toBeInTheDocument()
    // Beyond a week "N days ago" stops being useful, so it becomes a date.
    expect(screen.getByText(/^\d{1,2}\/\d{1,2},/)).toBeInTheDocument()
  })
})

describe('ChatPanel copy confirmation', () => {
  it('reverts to the copy action after acknowledging the copy', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    history = [turn('assistant', 'the answer')]
    await renderPanel()
    await user.click(await screen.findByRole('button', { name: 'Copy markdown' }))
    const copied = await screen.findByRole('button', { name: 'Copied' })
    expect(copied.style.opacity).toBe('1')

    // The tick is an acknowledgement, not a new state — it has to go away.
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(await screen.findByRole('button', { name: 'Copy markdown' })).toBeInTheDocument()
  })

  it('brings the copy action forward under the pointer', async () => {
    history = [turn('assistant', 'the answer')]
    await renderPanel()
    const copy = await screen.findByRole('button', { name: 'Copy markdown' })
    fireEvent.mouseEnter(copy)
    expect(copy.style.transform).toBe('scale(1.2)')
    expect(copy.style.color).toBe('var(--text-muted)')
    fireEvent.mouseLeave(copy)
    expect(copy.style.transform).toBe('scale(1)')
    expect(copy.style.color).toBe('var(--text-faint)')
  })

  it('brings the edit action forward under the pointer', async () => {
    history = [turn('user', 'my question')]
    await renderPanel()
    const edit = await screen.findByRole('button', { name: 'Edit & resend' })
    fireEvent.mouseEnter(edit)
    expect(edit.style.transform).toBe('scale(1.2)')
    fireEvent.mouseLeave(edit)
    expect(edit.style.transform).toBe('scale(1)')
  })
})

describe('ChatPanel composer focus and tips', () => {
  it('takes the caret back when the window is refocused', async () => {
    await renderPanel()
    composer().blur()
    expect(document.activeElement).not.toBe(composer())
    fireEvent.focus(window)
    // The panel is a small always-on-top window: refocusing it should leave the
    // user able to type immediately.
    expect(document.activeElement).toBe(composer())
  })

  it('rotates the placeholder tip', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await renderPanel()
    const first = composer().placeholder
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(composer().placeholder).not.toBe(first)
  })
})

describe('ChatPanel stop capsule hover', () => {
  it('dims the stop capsule under the pointer', async () => {
    await renderPanel()
    emit('onChatMessage', turn('user', 'long job'))
    const stop = await screen.findByRole('button', { name: /Stop/ })
    fireEvent.mouseEnter(stop)
    expect(stop.style.opacity).toBe('0.8')
    fireEvent.mouseLeave(stop)
    expect(stop.style.opacity).toBe('1')
  })
})

describe('ChatPanel trust scopes', () => {
  it('grants every tool when the widest scope is chosen deliberately', async () => {
    await renderPanel()
    emit('onApprovalRequest', {
      id: 'req-9', tool: 'execute_bash', toolInput: 'cat x',
      fullCommand: 'cat x', baseCommand: 'cat',
    })
    await userEvent.click(await screen.findByRole('button', { name: 'Trust' }))
    await userEvent.click(screen.getByRole('button', { name: 'Trust all tools' }))
    expect(respondApproval).toHaveBeenCalledWith('req-9', 'trust', undefined)
    expect(await screen.findByText('Trusted')).toBeInTheDocument()
  })
})

describe('ChatPanel file chip', () => {
  it('previews the file from the keyboard as well as the pointer', async () => {
    history = [turn('assistant', 'Check `src/app/notes.md` please.')]
    await renderPanel()
    const chip = await screen.findByTitle('src/app/notes.md')
    expect(chip).toHaveAttribute('role', 'button')
    fireEvent.keyDown(chip, { key: 'Enter' })
    expect(previewFile).toHaveBeenCalledWith('src/app/notes.md')
    fireEvent.keyDown(chip, { key: ' ' })
    expect(previewFile).toHaveBeenCalledTimes(2)
    // An unrelated key must not open anything.
    fireEvent.keyDown(chip, { key: 'a' })
    expect(previewFile).toHaveBeenCalledTimes(2)
  })

  it('highlights the chip actions under the pointer', async () => {
    history = [turn('assistant', 'Check `src/app/notes.md` please.')]
    await renderPanel()
    const preview = await screen.findByRole('button', { name: 'Preview' })
    fireEvent.mouseEnter(preview)
    expect(preview.style.color).toBe('var(--accent)')
    fireEvent.mouseLeave(preview)
    expect(preview.style.color).toBe('var(--text-muted)')
  })
})

describe('ChatPanel pointer feedback', () => {
  it('presses the send paw in and lets it back out', async () => {
    await renderPanel()
    const send = screen.getByRole('button', { name: 'Send' })
    fireEvent.mouseDown(send)
    expect(send.style.transform).toBe('scale(0.88)')
    fireEvent.mouseUp(send)
    expect(send.style.transform).toBe('scale(1)')
    fireEvent.mouseDown(send)
    fireEvent.mouseLeave(send)
    // Releasing outside the button must not leave it stuck pressed.
    expect(send.style.transform).toBe('scale(1)')
  })

  it('fades the edit-cancel action under the pointer', async () => {
    history = [turn('user', 'never mind')]
    await renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: 'Edit & resend' }))
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    fireEvent.mouseEnter(cancel)
    expect(cancel.style.opacity).toBe('0.7')
    fireEvent.mouseLeave(cancel)
    expect(cancel.style.opacity).toBe('1')
  })

  it('underlines a link only while it is hovered', async () => {
    history = [turn('assistant', 'See [the docs](https://example.com/guide).')]
    await renderPanel()
    const link = await screen.findByText('the docs')
    fireEvent.mouseEnter(link)
    expect(link.style.textDecoration).toBe('underline')
    fireEvent.mouseLeave(link)
    expect(link.style.textDecoration).toBe('none')
  })

  it('highlights the reveal action on a file chip', async () => {
    history = [turn('assistant', 'Check `src/app/notes.md` please.')]
    await renderPanel()
    const reveal = await screen.findByRole('button', { name: 'Show in file manager' })
    fireEvent.mouseEnter(reveal)
    expect(reveal.style.color).toBe('var(--accent)')
    fireEvent.mouseLeave(reveal)
    expect(reveal.style.color).toBe('var(--text-muted)')
  })

  /**
   * The reveal label follows the SHELL's platform, not the gateway's: `revealFile`
   * is an IPC send Mochi's Electron main process handles, so that host decides
   * which application opens. With no shell at all — a browser tab — nothing can
   * be revealed and nothing may be named.
   */
  it.each([
    ['darwin', 'Open in Finder'],
    ['win32', 'Open in File Explorer'],
    ['linux', 'Show in file manager'],
  ])('names the reveal action for a %s desktop shell', async (platform, label) => {
    stubGlobal('kirocrew', { isElectron: true, platform })
    history = [turn('assistant', 'Check `src/app/notes.md` please.')]
    await renderPanel()
    expect(await screen.findByRole('button', { name: label })).toBeInTheDocument()
  })
})

describe('PinnedSidePanel chip hover', () => {
  it('withdraws the unpin dot when the pointer leaves the chip', async () => {
    const pin = { path: '/home/u/src/a.ts', label: '', pinnedAt: 1 }
    render(
      <PinnedSidePanel pins={[pin]} updatedPaths={new Set()} deletedPaths={new Set()} visible />,
    )
    await userEvent.hover(screen.getByText('a.ts'))
    expect(screen.getByRole('button', { name: 'Unpin' })).toBeInTheDocument()
    await userEvent.unhover(screen.getByText('a.ts'))
    expect(screen.queryByRole('button', { name: 'Unpin' })).not.toBeInTheDocument()
    expect(unpinFile).not.toHaveBeenCalled()
  })
})
