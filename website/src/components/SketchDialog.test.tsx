import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import SketchDialog from './SketchDialog'

/** Captured from the mock's props so the persistence test can assert what a
 *  fresh mount was seeded with. */
let lastInitialData: { elements?: unknown[] } | null = null

/** Fake imperative API standing in for Excalidraw's. `elements` is mutable so
 *  individual tests can model an empty vs non-empty canvas. */
const fake = vi.hoisted(() => ({
  elements: [{ id: 'rect-1' }] as unknown[],
  /** Set to make the pad throw during render, so the ErrorBoundary fallback —
   *  the real-world shape of an uncached chunk that will not load — renders. */
  throwOnRender: false,
  /** Set to make the lazy import itself REJECT — the offline-first-open shape —
   *  as opposed to `throwOnRender`, which fails inside an import that succeeded. */
  failLoad: false,
  api: {
    getSceneElements: () => fake.elements,
    getAppState: () => ({ viewBackgroundColor: '#ffffff' }),
    getFiles: () => ({}),
    resetScene: vi.fn(() => { fake.elements = [] }),
  },
  exportToBlob: vi.fn(async () => new Blob(['png-bytes'], { type: 'image/png' })),
  serializeAsJSON: vi.fn(() =>
    JSON.stringify({ type: 'excalidraw', elements: fake.elements, appState: {}, files: {} })),
  restore: vi.fn((data: { elements?: unknown[]; appState?: object; files?: object }) => ({
    elements: data.elements ?? [],
    appState: { ...(data.appState ?? {}), normalized: true },
    files: data.files ?? {},
  })),
}))

vi.mock('@excalidraw/excalidraw', async () => {
  const React = await import('react')
  const FakeExcalidraw = (props: {
      excalidrawAPI?: (api: unknown) => void
      onChange?: () => void
      renderTopRightUI?: () => React.ReactNode
      initialData?:
        | { elements?: unknown[] }
        | (() => { elements?: unknown[] } | null)
        | null
    }) => {
      if (fake.throwOnRender) throw new Error('chunk load failed')
      lastInitialData =
        typeof props.initialData === 'function' ? props.initialData() : props.initialData ?? null
      React.useEffect(() => {
        props.excalidrawAPI?.(fake.api)
        props.onChange?.()
        // Registration + first change fire once per mount, mirroring the real
        // component's startup sequence.
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [])
      return React.createElement('div', { 'data-testid': 'fake-excalidraw' },
        props.renderTopRightUI ? props.renderTopRightUI() : null)
  }
  return {
    // A getter, so the lazy factory's `mod.Excalidraw` read can be made to
    // throw per attempt: that rejects the lazy payload exactly the way a chunk
    // that failed to fetch does, which is the state React caches.
    get Excalidraw() {
      if (fake.failLoad) throw new Error('chunk load failed')
      return FakeExcalidraw
    },
    exportToBlob: fake.exportToBlob,
    serializeAsJSON: fake.serializeAsJSON,
    restore: fake.restore,
  }
})
vi.mock('@excalidraw/excalidraw/index.css', () => ({}))

describe('SketchDialog', () => {
  beforeEach(() => {
    fake.elements = [{ id: 'rect-1' }]
    fake.throwOnRender = false
    fake.failLoad = false
    fake.exportToBlob.mockClear()
    fake.serializeAsJSON.mockClear()
  })

  it('renders the whiteboard and enables Insert once the scene has elements', async () => {
    render(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
    await screen.findByTestId('fake-excalidraw')
    const insert = screen.getByRole('button', { name: 'Attach to message' })
    await waitFor(() => expect(insert).not.toBeDisabled())
  })

  it('keeps Insert disabled while the canvas is empty', async () => {
    fake.elements = []
    render(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
    await screen.findByTestId('fake-excalidraw')
    expect(screen.getByRole('button', { name: 'Attach to message' })).toBeDisabled()
  })

  it('exports PNG + .excalidraw.json sidecar and closes on Insert', async () => {
    const onInsert = vi.fn()
    const onOpenChange = vi.fn()
    render(<SketchDialog open onOpenChange={onOpenChange} onInsert={onInsert} />)
    await screen.findByTestId('fake-excalidraw')
    const insert = screen.getByRole('button', { name: 'Attach to message' })
    await waitFor(() => expect(insert).not.toBeDisabled())

    fireEvent.click(insert)

    await waitFor(() => expect(onInsert).toHaveBeenCalledTimes(1))
    const files = onInsert.mock.calls[0][0] as File[]
    expect(files).toHaveLength(2)
    expect(files[0].name).toMatch(/^sketch-.+\.png$/)
    expect(files[0].type).toBe('image/png')
    expect(files[1].name).toMatch(/^sketch-.+\.excalidraw$/)
    expect(files[1].type).toBe('application/json')
    // Both artifacts stamp the SAME moment so they pair up in the attachment list.
    expect(files[1].name.replace(/\.excalidraw$/, '')).toBe(files[0].name.replace(/\.png$/, ''))
    expect(fake.exportToBlob).toHaveBeenCalledWith(
      expect.objectContaining({ mimeType: 'image/png', appState: expect.objectContaining({ exportBackground: true }) }),
    )
    expect(onOpenChange).toHaveBeenCalledWith(false)
    // The scene deliberately survives insert: onInsert returns before the
    // upload is accepted, so clearing here would strand a failed upload with
    // no copy to retry from. "New sketch" is the explicit clear path.
  })

  it('surfaces a failure line and stays open when export rejects', async () => {
    fake.exportToBlob.mockRejectedValueOnce(new Error('boom'))
    const onInsert = vi.fn()
    const onOpenChange = vi.fn()
    render(<SketchDialog open onOpenChange={onOpenChange} onInsert={onInsert} />)
    await screen.findByTestId('fake-excalidraw')
    const insert = screen.getByRole('button', { name: 'Attach to message' })
    await waitFor(() => expect(insert).not.toBeDisabled())

    fireEvent.click(insert)

    await screen.findByRole('alert')
    expect(onInsert).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalled()
    // Insert stays usable for the retry.
    expect(insert).not.toBeDisabled()
  })

  it('persists the scene to localStorage and seeds a fresh mount from it', async () => {
    vi.useFakeTimers()
    try {
      localStorage.removeItem('mc-sketch-scene')
      const { unmount } = render(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
      // findByTestId under fake timers: the lazy mock resolves on microtasks,
      // so flush them explicitly instead of waiting on real time.
      await vi.waitFor(() => expect(screen.queryByTestId('fake-excalidraw')).not.toBeNull())
      // The mock fires one onChange on mount; the debounced write lands 500ms later.
      vi.advanceTimersByTime(600)
      const stored = JSON.parse(localStorage.getItem('mc-sketch-scene') ?? 'null')
      expect(stored?.elements).toHaveLength(1)
      unmount()

      // A fresh mount (fresh sceneRef — simulating a reload) seeds from storage.
      render(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
      await vi.waitFor(() => expect(screen.queryByTestId('fake-excalidraw')).not.toBeNull())
      expect(lastInitialData?.elements).toHaveLength(1)
    } finally {
      vi.useRealTimers()
      localStorage.removeItem('mc-sketch-scene')
    }
  })

  it('New sketch resets the canvas and drops the stored draft', async () => {
    localStorage.setItem('mc-sketch-scene', '{"type":"excalidraw","elements":[{"id":"old"}]}')
    render(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
    await screen.findByTestId('fake-excalidraw')
    const reset = screen.getByRole('button', { name: 'New sketch' })
    await waitFor(() => expect(reset).not.toBeDisabled())

    // Two-step confirm: first click arms, second click executes.
    fireEvent.click(reset)
    expect(fake.api.resetScene).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Discard drawing' }))

    expect(fake.api.resetScene).toHaveBeenCalled()
    expect(localStorage.getItem('mc-sketch-scene')).toBeNull()
    // Attach disables again on the now-empty canvas.
    expect(screen.getByRole('button', { name: 'Attach to message' })).toBeDisabled()
  })

  it('does not mount Excalidraw while closed (lazy chunk stays unloaded)', () => {
    render(<SketchDialog open={false} onOpenChange={() => {}} onInsert={() => {}} />)
    expect(screen.queryByTestId('fake-excalidraw')).toBeNull()
  })

  /** `errors-use-error-notice` (blocking): a load failure is a dead end the user
   *  cannot clear, so it renders through ErrorNotice WITH the agent hand-off —
   *  not the hand-written muted line this replaced. */
  it('offers the agent hand-off when the pad fails to load', async () => {
    fake.throwOnRender = true
    const onOpenChange = vi.fn()
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      render(<SketchDialog open onOpenChange={onOpenChange} onInsert={() => {}} />)
      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent("Couldn't load the sketch pad")
      // The hand-off, and the dialog closing before it navigates — a hand-off
      // under a modal that stays open reads as a dead button.
      fireEvent.click(screen.getByRole('button', { name: /ask the agent/i }))
      expect(onOpenChange).toHaveBeenCalledWith(false)
    } finally {
      consoleError.mockRestore()
    }
  })

  /** React stores a lazy factory's REJECTION on the lazy object itself, so a
   *  module-level `lazy(...)` would replay an offline first open forever no
   *  matter how the boundary around it is reset. Reopening must be a real
   *  retry: a fresh lazy per open, calling the loader again. */
  it('retries a failed load when the dialog is reopened', async () => {
    fake.failLoad = true
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      const { rerender } = render(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
      await screen.findByRole('alert')

      // The network is back; close and reopen.
      fake.failLoad = false
      rerender(<SketchDialog open={false} onOpenChange={() => {}} onInsert={() => {}} />)
      rerender(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
      await screen.findByTestId('fake-excalidraw')
      expect(screen.queryByRole('alert')).toBeNull()
    } finally {
      consoleError.mockRestore()
    }
  })

  /** A header that says "Draw something first" beside an error saying the pad
   *  never loaded is a hint nobody can satisfy. While the load-failure fallback
   *  is showing — whatever put it there — both the header hint and the disabled
   *  Attach button's matching tooltip go quiet; a reopen that loads brings them
   *  back. */
  describe('failed-load header', () => {
    const expectHintSuppressed = async () => {
      await screen.findByRole('alert')
      await waitFor(() => expect(screen.queryByText('Draw something first')).toBeNull())
      const attach = screen.getByRole('button', { name: 'Attach to message' })
      expect(attach).toBeDisabled()
      expect(attach).not.toHaveAttribute('title')
    }

    it('drops the hint and tooltip when the chunk import rejects', async () => {
      fake.failLoad = true
      fake.elements = []
      const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
      try {
        const { rerender } = render(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
        await expectHintSuppressed()

        // Reopen with the chunk available: the empty-canvas hint is legitimate again.
        fake.failLoad = false
        rerender(<SketchDialog open={false} onOpenChange={() => {}} onInsert={() => {}} />)
        rerender(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
        await screen.findByTestId('fake-excalidraw')
        expect(screen.getByText('Draw something first')).toBeInTheDocument()
        expect(screen.getByRole('button', { name: 'Attach to message' }))
          .toHaveAttribute('title', 'Draw something first')
      } finally {
        consoleError.mockRestore()
      }
    })

    it('drops the hint and tooltip when the pad throws during render', async () => {
      fake.throwOnRender = true
      fake.elements = []
      const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
      try {
        render(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
        await expectHintSuppressed()
      } finally {
        consoleError.mockRestore()
      }
    })
  })

  /** Excalidraw measures its container's viewport rect ONCE on mount and
   *  refreshes it only on window resize, scroll, or a ResizeObserver on its own
   *  container — none of which a CSS transform animation fires, because the
   *  layout box never moves. Mounting the pad while the dialog is still zooming
   *  in therefore froze a mid-flight origin, which put every drawn shape and
   *  every resize handle ~7px from the cursor for the dialog's whole life.
   *
   *  jsdom implements no animations, so these two tests drive
   *  `getAnimations()` directly: that is the exact signal the component keys on,
   *  and the browser-level proof lives in
   *  scripts/capture-sketch-cursor-offset.mjs. */
  describe('placement gate', () => {
    const withAnimations = (anims: Animation[]) => {
      const proto = Element.prototype as unknown as { getAnimations?: () => Animation[] }
      const had = Object.prototype.hasOwnProperty.call(proto, 'getAnimations')
      const previous = proto.getAnimations
      proto.getAnimations = () => anims
      return () => {
        if (had) proto.getAnimations = previous
        else delete proto.getAnimations
      }
    }

    it('withholds the pad until the dialog stops animating', async () => {
      let finish: () => void = () => {}
      const pending = new Promise<void>(resolve => { finish = resolve })
      const restore = withAnimations([{ finished: pending } as unknown as Animation])
      try {
        render(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
        // The loading placeholder stands in, so the wait reads as one continuous
        // load rather than an empty pane.
        await screen.findByText('Loading sketch pad…')
        expect(screen.queryByTestId('fake-excalidraw')).toBeNull()

        finish()
        await screen.findByTestId('fake-excalidraw')
      } finally {
        restore()
      }
    })

    it('mounts the pad anyway when the entrance animation is cancelled', async () => {
      // `Animation.finished` REJECTS on cancellation. A cancelled entrance still
      // means the dialog has come to rest, so it must not strand the pad behind
      // the placeholder.
      const rejected = Promise.reject(new Error('cancelled'))
      const restore = withAnimations([{ finished: rejected } as unknown as Animation])
      try {
        render(<SketchDialog open onOpenChange={() => {}} onInsert={() => {}} />)
        await screen.findByTestId('fake-excalidraw')
      } finally {
        restore()
      }
    })
  })
})
