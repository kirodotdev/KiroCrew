import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import SketchDialog from './SketchDialog'

/** Captured from the mock's props so the persistence test can assert what a
 *  fresh mount was seeded with. */
let lastInitialData: { elements?: unknown[] } | null = null

/** Fake imperative API standing in for Excalidraw's. `elements` is mutable so
 *  individual tests can model an empty vs non-empty canvas. */
const fake = vi.hoisted(() => ({
  elements: [{ id: 'rect-1' }] as unknown[],
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
  return {
    Excalidraw: (props: {
      excalidrawAPI?: (api: unknown) => void
      onChange?: () => void
      renderTopRightUI?: () => React.ReactNode
      initialData?:
        | { elements?: unknown[] }
        | (() => { elements?: unknown[] } | null)
        | null
    }) => {
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
})
