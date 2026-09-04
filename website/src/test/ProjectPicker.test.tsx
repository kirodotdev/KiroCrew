import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectPicker from '../components/ProjectPicker'
import { api } from '../api/client'
import { useRef } from 'react'

type BrowseDirsResult = Awaited<ReturnType<typeof api.browseDirs>>

const mockBrowseDirs = (path = '/home/u', dirs: { name: string; path: string }[] = []): BrowseDirsResult =>
  ({ path, parent: '/home', dirs })

beforeEach(() => {
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/projA', '/home/u/projB'] })
  vi.spyOn(api, 'browseDirs').mockResolvedValue(mockBrowseDirs())
})

afterEach(() => {
  vi.restoreAllMocks()
})

// Helper: build a DOMRect-shaped object (jsdom doesn't expose DOMRect directly).
const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height,
  bottom: top + height,
  right: left + width,
  x: left, y: top,
  toJSON: () => ({}),
} as DOMRect)

describe('ProjectPicker', () => {
  describe('visibility', () => {
    it('renders nothing when open is false', () => {
      const { container } = renderWithProviders(
        <ProjectPicker open={false} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(container.textContent).toBe('')
      expect(screen.queryByText('Recent')).not.toBeInTheDocument()
    })

    it('renders nothing when open but no anchor (rect or ref) is provided', () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} onSelect={vi.fn()} />
      )
      expect(screen.queryByText('Recent')).not.toBeInTheDocument()
    })

    it('renders tabs and Recent panel when open with anchorRect', async () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('Recent')).toBeInTheDocument()
      expect(screen.getByText('Browse')).toBeInTheDocument()
    })
  })

  describe('anchorRect positioning', () => {
    it('positions below the anchor when in upper viewport half (no flip)', async () => {
      // Anchor near top of a 768-tall viewport; bottom = 124 < 384 (half)
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 200)} onSelect={vi.fn()} />
      )
      const drop = (await screen.findByText('Recent')).closest('div.fixed') as HTMLElement
      expect(drop).toBeTruthy()
      // top = anchorR.bottom (124) + 4 = 128
      expect(drop.style.top).toBe('128px')
      expect(drop.style.bottom).toBe('')
    })

    it('flips upward when anchor is in lower viewport half', async () => {
      // Viewport 768 tall, anchor at top=600 → bottom=624 > 384 → flipUp
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(600, 200)} onSelect={vi.fn()} />
      )
      const drop = (await screen.findByText('Recent')).closest('div.fixed') as HTMLElement
      expect(drop).toBeTruthy()
      // bottom = innerHeight - anchorR.top + 4 = 768 - 600 + 4 = 172
      expect(drop.style.bottom).toBe('172px')
      expect(drop.style.top).toBe('')
    })

    it('clamps left position to keep dropdown inside viewport', async () => {
      // Anchor at right edge: innerWidth=1280, anchorR.right=1278 → left = min(1278-400, 1280-408) = 872
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(50, 1198, 80, 24)} onSelect={vi.fn()} />
      )
      const drop = (await screen.findByText('Recent')).closest('div.fixed') as HTMLElement
      expect(parseInt(drop.style.left)).toBeLessThanOrEqual(872)
      expect(parseInt(drop.style.left)).toBeGreaterThanOrEqual(8)
    })

    it('clamps left position to minimum 8px when anchor is far left', async () => {
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(50, 0, 20)} onSelect={vi.fn()} />
      )
      const drop = (await screen.findByText('Recent')).closest('div.fixed') as HTMLElement
      // anchorR.right = 20 → 20 - 400 = -380 → Math.max(8, ...) = 8
      expect(drop.style.left).toBe('8px')
    })
  })

  describe('anchorRef fallback', () => {
    function PickerWithRef({ onSelect = vi.fn() }: { onSelect?: (p: string) => void }) {
      const ref = useRef<HTMLButtonElement>(null)
      return (
        <>
          <button ref={ref} data-testid="anchor-btn">Anchor</button>
          <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRef={ref} onSelect={onSelect} />
        </>
      )
    }

    it('falls back to anchorRef.getBoundingClientRect when anchorRect is absent', async () => {
      renderWithProviders(<PickerWithRef />)
      // jsdom returns a 0,0,0,0 rect by default but it's still a valid DOMRect → component renders
      expect(await screen.findByText('Recent')).toBeInTheDocument()
    })

    it('prefers live anchorRef.getBoundingClientRect over anchorRect when both are provided', async () => {
      function Both() {
        const ref = useRef<HTMLButtonElement>(null)
        return (
          <>
            <button ref={ref}>Anchor</button>
            <ProjectPicker
              open={true}
              onOpenChange={vi.fn()}
              anchorRef={ref}
              anchorRect={rect(100, 200)}
              onSelect={vi.fn()}
            />
          </>
        )
      }
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      renderWithProviders(<Both />)
      await screen.findByText('Recent')
      // Live ref measurement wins so layout shifts (scroll/resize/keyboard) stay accurate.
      // jsdom returns a 0,0,0,0 rect for the button → bottom=0 → top = 0 + 4 = 4,
      // NOT the captured anchorRect's 124 + 4 = 128. The ref attaches after the
      // first paint, so wait for the post-mount re-render to settle the value.
      await waitFor(() => {
        const drop = screen.getByText('Recent').closest('div.fixed') as HTMLElement
        expect(drop.style.top).toBe('4px')
      })
    })
  })

  describe('outside-click behavior', () => {
    it('closes when mousedown lands outside both dropdown and anchor', async () => {
      const onOpenChange = vi.fn()
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 200)} onSelect={vi.fn()} />
      )
      await screen.findByText('Recent')
      // Tick the timer so the listener is registered
      await act(async () => { await Promise.resolve() })
      // Click well outside (clientX=0, clientY=0 is not inside anchorRect or dropdown)
      const evt = new MouseEvent('mousedown', { clientX: 0, clientY: 0, bubbles: true })
      document.dispatchEvent(evt)
      await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    })

    it('does NOT close when mousedown is inside the anchor rect (rect hit-test)', async () => {
      const onOpenChange = vi.fn()
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 200, 80, 24)} onSelect={vi.fn()} />
      )
      await screen.findByText('Recent')
      await act(async () => { await Promise.resolve() })
      // Click inside anchor rect: x in [200,280], y in [100,124]
      const evt = new MouseEvent('mousedown', { clientX: 240, clientY: 110, bubbles: true })
      document.dispatchEvent(evt)
      // Give it a moment to (not) fire
      await act(async () => { await Promise.resolve() })
      expect(onOpenChange).not.toHaveBeenCalled()
    })

    it('does NOT close when mousedown is inside the dropdown panel itself', async () => {
      const onOpenChange = vi.fn()
      Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 200)} onSelect={vi.fn()} />
      )
      const recentTab = await screen.findByText('Recent')
      await act(async () => { await Promise.resolve() })
      fireEvent.mouseDown(recentTab)
      expect(onOpenChange).not.toHaveBeenCalled()
    })
  })

  describe('selection', () => {
    it('renders recent projects from api.recentProjects', async () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('projA')).toBeInTheDocument()
      expect(screen.getByText('projB')).toBeInTheDocument()
    })

    it('calls onSelect and onOpenChange(false) when clicking a recent entry', async () => {
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      const item = await screen.findByText('projA')
      fireEvent.mouseDown(item)
      expect(onSelect).toHaveBeenCalledWith('/home/u/projA')
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })

    it('shows "No recent projects" when user switches to Recent tab with empty list', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      // Empty list auto-switches to Browse, so click Recent to land on the empty state
      const recentTab = await screen.findByText('Recent')
      fireEvent.mouseDown(recentTab)
      expect(await screen.findByText('No recent projects')).toBeInTheDocument()
    })

    it('switches to Browse tab when no recent projects exist', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', [
        { name: 'workplace', path: '/home/u/workplace' },
      ]))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      // Browse panel shows the directory listing
      expect(await screen.findByText('workplace')).toBeInTheDocument()
    })

    it('selects typed path on Enter in Browse tab', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      const input = await screen.findByPlaceholderText('/path/to/project')
      fireEvent.change(input, { target: { value: '/home/u/typed' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(onSelect).toHaveBeenCalledWith('/home/u/typed')
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })

    it('closes on Escape in Browse tab without calling onSelect', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      const input = await screen.findByPlaceholderText('/path/to/project')
      fireEvent.keyDown(input, { key: 'Escape' })
      expect(onSelect).not.toHaveBeenCalled()
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
  })

  describe('keyboard navigation', () => {
    it('Recent tab: ArrowDown moves the highlight and Enter selects', async () => {
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      await screen.findByText('projA')
      const optA = screen.getByText('projA').closest('[role="option"]') as HTMLElement
      const optB = screen.getByText('projB').closest('[role="option"]') as HTMLElement
      // First option highlighted by default.
      expect(optA).toHaveAttribute('aria-selected', 'true')
      // The Recent tab listens at the document level (no input to focus).
      fireEvent.keyDown(document, { key: 'ArrowDown' })
      await waitFor(() => expect(optB).toHaveAttribute('aria-selected', 'true'))
      fireEvent.keyDown(document, { key: 'Enter' })
      expect(onSelect).toHaveBeenCalledWith('/home/u/projB')
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })

    it('Browse tab: ArrowDown highlights a subdir and Enter drills into it', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      const browseSpy = vi.mocked(api.browseDirs)
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u', [
        { name: 'alpha', path: '/home/u/alpha' },
        { name: 'beta', path: '/home/u/beta' },
      ]))
      const onSelect = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      const input = await screen.findByPlaceholderText('/path/to/project')
      await screen.findByText('beta')
      browseSpy.mockClear()
      fireEvent.keyDown(input, { key: 'ArrowDown' }) // highlight index 1 (beta)
      fireEvent.keyDown(input, { key: 'Enter' })     // Enter drills into the highlighted folder
      await waitFor(() => expect(browseSpy).toHaveBeenCalledWith('/home/u/beta'))
      expect(onSelect).not.toHaveBeenCalled()         // drilling, not committing
    })

    it('Browse tab: Cmd+Enter commits the current directory', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', [
        { name: 'alpha', path: '/home/u/alpha' },
      ]))
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      const input = await screen.findByPlaceholderText('/path/to/project')
      await screen.findByText('alpha')
      fireEvent.keyDown(input, { key: 'Enter', metaKey: true }) // commit current dir, no drill
      expect(onSelect).toHaveBeenCalledWith('/home/u')
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
  })

  describe('Recent tab search', () => {
    it('renders a search box only when there are recent projects', async () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      // Recent projects exist (projA/projB from the default beforeEach mock).
      expect(await screen.findByPlaceholderText('Search recent projects…')).toBeInTheDocument()
    })

    it('does NOT render the search box when there are no recent projects', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      // Empty list lands on Browse; switch to Recent and confirm no search box.
      const recentTab = await screen.findByText('Recent')
      fireEvent.mouseDown(recentTab)
      await screen.findByText('No recent projects')
      expect(screen.queryByPlaceholderText('Search recent projects…')).not.toBeInTheDocument()
    })

    it('filters the recent list by case-insensitive substring on the full path', async () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await screen.findByText('projA')
      const searchBox = screen.getByPlaceholderText('Search recent projects…')
      // 'proja' (lowercase) matches '/home/u/projA' but not '/home/u/projB'.
      fireEvent.change(searchBox, { target: { value: 'proja' } })
      await waitFor(() => expect(screen.queryByText('projB')).not.toBeInTheDocument())
      expect(screen.getByText('projA')).toBeInTheDocument()
    })

    it('shows "No matching projects" when the query matches nothing', async () => {
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await screen.findByText('projA')
      const searchBox = screen.getByPlaceholderText('Search recent projects…')
      fireEvent.change(searchBox, { target: { value: 'zzz-no-match' } })
      expect(await screen.findByText('No matching projects')).toBeInTheDocument()
    })

    it('keyboard nav + Enter selects from the filtered list, not the full list', async () => {
      const onSelect = vi.fn()
      const onOpenChange = vi.fn()
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )
      await screen.findByText('projA')
      const searchBox = screen.getByPlaceholderText('Search recent projects…')
      // Narrow to just projB. The document-level nav hook now sees count=1.
      fireEvent.change(searchBox, { target: { value: 'projb' } })
      await waitFor(() => expect(screen.queryByText('projA')).not.toBeInTheDocument())
      // Index 0 of the filtered list is projB; Enter selects it.
      fireEvent.keyDown(document, { key: 'Enter' })
      expect(onSelect).toHaveBeenCalledWith('/home/u/projB')
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
  })

  describe('Browse tab trailing-slash auto-drill', () => {
    beforeEach(() => {
      vi.useFakeTimers()
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
    })
    afterEach(() => {
      vi.runOnlyPendingTimers()
      vi.useRealTimers()
    })

    it('drills into the typed directory when the input ends with a slash', async () => {
      const browseSpy = vi.mocked(api.browseDirs)
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      // Drain the initial browse() + recentProjects() promises.
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      const input = screen.getByPlaceholderText('/path/to/project')
      browseSpy.mockClear()
      fireEvent.change(input, { target: { value: '/home/u/workplace/' } })
      // Debounce is 250ms; nothing should fire before it elapses.
      expect(browseSpy).not.toHaveBeenCalled()
      await act(async () => { await vi.advanceTimersByTimeAsync(250) })
      // Trailing slash is stripped to the target dir for the API call. The
      // preserveInput flag is internal to browse() and is NOT forwarded to
      // api.browseDirs (a network call that only takes a path), so the spy
      // sees just the path. Slash preservation is asserted in the next test.
      expect(browseSpy).toHaveBeenCalledWith('/home/u/workplace')
    })

    it('preserves the typed trailing slash in the input after the drill resolves', async () => {
      const browseSpy = vi.mocked(api.browseDirs)
      // Initial mount resolves to /home/u so the drill target (/home/u/workplace)
      // differs from browsePath — otherwise the `target === browsePath` guard
      // early-returns and the drill never fires (making the assertion trivial).
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      const input = screen.getByPlaceholderText('/path/to/project') as HTMLInputElement
      // The drill response resolves with a canonical path WITHOUT the trailing slash.
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u/workplace', []))
      fireEvent.change(input, { target: { value: '/home/u/workplace/' } })
      await act(async () => { await vi.advanceTimersByTimeAsync(250) })
      // The drill fired (target differed from browsePath)...
      expect(browseSpy).toHaveBeenCalledWith('/home/u/workplace')
      // ...but preserveInput=true means setInput is NOT called, so the user's
      // text (including the trailing slash they just typed) is retained.
      expect(input.value).toBe('/home/u/workplace/')
    })

    it('does NOT auto-drill for a non-slash-terminated path', async () => {
      const browseSpy = vi.mocked(api.browseDirs)
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      const input = screen.getByPlaceholderText('/path/to/project')
      browseSpy.mockClear()
      fireEvent.change(input, { target: { value: '/home/u/workpla' } })
      await act(async () => { await vi.advanceTimersByTimeAsync(300) })
      expect(browseSpy).not.toHaveBeenCalled()
    })

    it('does NOT re-drill when the slash target equals the already-loaded dir', async () => {
      const browseSpy = vi.mocked(api.browseDirs)
      // browsePath is '/home/u' after the initial load.
      browseSpy.mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      const input = screen.getByPlaceholderText('/path/to/project')
      browseSpy.mockClear()
      // Typing '/home/u/' strips to '/home/u' which equals browsePath → no-op.
      fireEvent.change(input, { target: { value: '/home/u/' } })
      await act(async () => { await vi.advanceTimersByTimeAsync(300) })
      expect(browseSpy).not.toHaveBeenCalled()
    })
  })
})
