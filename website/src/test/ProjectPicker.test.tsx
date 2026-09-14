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

    it('does not claim there are no recent projects while the read is still pending', async () => {
      // `recentDirs` is [] until the query settles, now up to BROWSE_FILES_TIMEOUT_MS, so
      // the empty claim was a statement about data that had not arrived.
      let release: (v: { dirs: string[] }) => void = () => {}
      vi.mocked(api.recentProjects).mockImplementation(
        () => new Promise(resolve => { release = resolve }) as never,
      )
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )

      expect(await screen.findByText('Loading…')).toBeInTheDocument()
      expect(screen.queryByText('No recent projects')).not.toBeInTheDocument()

      // Settle with rows, so the tab stays on Recent (an empty list auto-switches to
      // Browse, which the sibling test above already covers).
      release({ dirs: ['/home/u/projA'] })
      expect(await screen.findByText('/home/u/projA')).toBeInTheDocument()
      expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
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

    it('ignores a superseded browse rejection that lands after a newer success', async () => {
      // Without the generation guard the first drill's late rejection set listError
      // after the second drill had already rendered valid rows.
      let rejectFirst: (e: Error) => void = () => {}
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs)
        .mockImplementationOnce(() => new Promise((_res, rej) => { rejectFirst = rej }))
        .mockResolvedValue(mockBrowseDirs('/home/u', [{ name: 'beta', path: '/home/u/beta' }]))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      const input = await screen.findByLabelText('Project directory path')
      fireEvent.change(input, { target: { value: '/home/u/' } })
      expect(await screen.findByText('beta')).toBeInTheDocument()
      await act(async () => { rejectFirst(new Error('stale')) })
      expect(screen.queryByRole('alert')).toBeNull()
      expect(screen.getByText('beta')).toBeInTheDocument()
    })

    it('keeps the previous listing while a drill is pending, not an emptiness claim', async () => {
      // "No subdirectories" asserts a folder IS empty, so a listing still in flight must
      // not render it -- on a wedged gateway that claim would hold for the whole deadline.
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(mockBrowseDirs('/home/u', [{ name: 'beta', path: '/home/u/beta' }]))
        .mockImplementationOnce(() => new Promise(() => {}))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      const input = await screen.findByLabelText('Project directory path')
      expect(await screen.findByText('beta')).toBeInTheDocument()
      fireEvent.keyDown(input, { key: 'ArrowDown' })
      fireEvent.keyDown(input, { key: 'Enter' })
      await waitFor(() => expect(api.browseDirs).toHaveBeenCalledTimes(2))
      expect(screen.queryByText('No subdirectories')).toBeNull()
      expect(screen.getByText('beta')).toBeInTheDocument()
    })

    it('keeps a path typed after a failed listing when Retry succeeds', async () => {
      // The retry reuses the failed target, so without flipping its flag the delivered read
      // reclaims the input and silently discards the correction the user typed to recover.
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs).mockRejectedValueOnce(new Error('nope'))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await screen.findByRole('alert')
      const input = await screen.findByLabelText('Project directory path')
      // A partial segment, as a user mid-correction would leave it -- and one the row filter
      // still matches, so the delivered row below is observable rather than filtered away.
      fireEvent.change(input, { target: { value: '/home/u/al' } })

      vi.mocked(api.browseDirs).mockResolvedValue(
        mockBrowseDirs('/home/u', [{ name: 'alpha', path: '/home/u/alpha' }]))
      fireEvent.click(screen.getByRole('button', { name: /^Retry: / }))

      // Waiting on the delivered row is what makes the assertion below meaningful: it proves
      // onData ran, so an unchanged input is a decision rather than a read that never landed.
      expect(await screen.findByText('alpha')).toBeInTheDocument()
      expect(input).toHaveValue('/home/u/al')
    })

    it('names a browse timeout apart from a browse failure', async () => {
      // A wedged gateway and a refusal are different remedies, so the picker must not
      // render one copy for both -- the folder panel's search already distinguishes them.
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs).mockRejectedValue(
        Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }),
      )
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByRole('alert')).toHaveTextContent(/Folder listing timed out/)
      expect(screen.queryByText(/Unable to list folder/)).toBeNull()
    })

    it('drops the recents notice off a Browse tab the user chose deliberately', async () => {
      vi.mocked(api.recentProjects).mockRejectedValue(
        Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }),
      )
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('Recent projects unavailable')).toBeInTheDocument()

      fireEvent.mouseDown(await screen.findByText('Browse'))
      expect(screen.queryByText('Recent projects unavailable')).not.toBeInTheDocument()

      fireEvent.mouseDown(await screen.findByText('Recent'))
      expect(screen.queryByText('Recent projects unavailable')).toBeInTheDocument()
    })

    it('surfaces a recent-projects failure instead of silently landing on Browse', async () => {
      // Flipping to Browse is the fallback, but on its own it reads as "you have no recent
      // projects" -- a claim about the user's data, not about a request that never answered.
      vi.mocked(api.recentProjects).mockRejectedValue(
        Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }),
      )
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('Recent projects unavailable')).toBeInTheDocument()
      // And says it ONCE. The list is empty because the request failed, not because the
      // account has no projects, so the empty-state copy would contradict the notice.
      const recentTab = await screen.findByText('Recent')
      fireEvent.mouseDown(recentTab)
      expect(screen.queryByText('No recent projects')).not.toBeInTheDocument()
    })

    it('keeps focus in the picker when the recents Retry recovers into an EMPTY list', async () => {
      // The hand-off targeted the recents search field, which mounts only when rows exist, and
      // `inputRef` only on Browse -- so an empty recovery dropped a keyboard user onto <body>.
      const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
      vi.mocked(api.recentProjects).mockRejectedValue(timeout)
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('Recent projects unavailable')).toBeInTheDocument()

      fireEvent.mouseDown(await screen.findByText('Recent'))
      const retry = await screen.findByRole('button', { name: /^Retry: Recent projects/ })
      // Recovers, but with NO rows: the search field never mounts.
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      fireEvent.click(retry)

      await waitFor(() => expect(screen.queryByText('Recent projects unavailable')).not.toBeInTheDocument())
      expect(screen.queryByPlaceholderText('Search recent projects…')).not.toBeInTheDocument()
      await waitFor(() => expect(document.activeElement).not.toBe(document.body))
    })

    it('freezes tab landing once, so a late recents arrival cannot move the user off her tab', async () => {
      // Without the freeze, recents resolving after the user picks a tab yanks her to the
      // other one mid-interaction.
      let release: (v: { dirs: string[] }) => void = () => {}
      vi.mocked(api.recentProjects).mockReturnValue(
        new Promise<{ dirs: string[] }>(r => { release = r }) as never)
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      // The user deliberately goes to Browse while recents are still in flight.
      fireEvent.mouseDown(await screen.findByText('Browse'))
      expect(await screen.findByPlaceholderText('/path/to/project')).toBeInTheDocument()

      // Recents land late, and WITH rows — the case that would auto-select the Recent tab.
      release({ dirs: ['/home/u/projA', '/home/u/projB'] })
      await waitFor(() => expect(api.recentProjects).toHaveBeenCalled())
      await new Promise(r => setTimeout(r, 50))
      expect(screen.getByPlaceholderText('/path/to/project')).toBeInTheDocument()
      expect(screen.queryByPlaceholderText('Search recent projects…')).not.toBeInTheDocument()
    })

    it('clears the previous directory\u2019s rows when a drill fails, not leaving them under the notice', async () => {
      // The rows in state describe the directory we drilled OUT of, so leaving them
      // beneath "listing timed out" reads as the new folder's contents.
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(mockBrowseDirs('/home/u', [{ name: 'child', path: '/home/u/child' }]))
        .mockRejectedValue(Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      fireEvent.click(await screen.findByText('child'))                    // drill in; this one fails
      expect(await screen.findByText('Folder listing timed out')).toBeInTheDocument()
      expect(screen.queryByText('child')).not.toBeInTheDocument()
    })

    it('persists the RETRIED directory, not the stale parent still shown in the field', async () => {
      const onSelect = vi.fn()
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(mockBrowseDirs('/home/u', [{ name: 'child', path: '/home/u/child' }]))
        .mockRejectedValueOnce(Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }))
        .mockResolvedValue(mockBrowseDirs('/home/u/child', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={onSelect} />
      )

      fireEvent.click(await screen.findByText('child'))
      await screen.findByText('Folder listing timed out')
      // The field is the parent at this point, which is exactly the hazard.
      expect(screen.getByPlaceholderText('/path/to/project')).toHaveValue('/home/u/')

      fireEvent.click(screen.getByRole('button', { name: /^Retry/ }))
      await waitFor(() =>
        expect(screen.getByPlaceholderText('/path/to/project')).toHaveValue('/home/u/child/'))

      // The control commits on mousedown, so a click never reaches it.
      fireEvent.mouseDown(screen.getByRole('button', { name: 'Select' }))
      expect(onSelect).toHaveBeenCalledTimes(1)
      expect(onSelect.mock.calls[0][0]).toMatch(/^\/home\/u\/child\/?$/)
    })

    it('keeps what the user typed after a failure, rather than overwriting it on recovery', async () => {
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(mockBrowseDirs('/home/u', [{ name: 'child', path: '/home/u/child' }]))
        .mockRejectedValueOnce(Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }))
        .mockResolvedValue(mockBrowseDirs('/home/u/child', []))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )

      fireEvent.click(await screen.findByText('child'))
      await screen.findByText('Folder listing timed out')
      const field = screen.getByPlaceholderText('/path/to/project')
      fireEvent.change(field, { target: { value: '/somewhere/else' } })

      fireEvent.click(screen.getByRole('button', { name: /^Retry/ }))
      await waitFor(() => expect(api.browseDirs).toHaveBeenCalledTimes(3))
      expect(field).toHaveValue('/somewhere/else')
    })

    it('clears a stale recent-projects error on reopen, not only when the next fetch lands', async () => {
      // This picker lives on an always-mounted page, so without a synchronous reset the
      // previous failure is still on screen while the new request is in flight.
      vi.mocked(api.recentProjects)
        .mockRejectedValueOnce(new Error('nope'))
        .mockImplementation(() => new Promise(() => {}))     // reopen: never settles
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', []))
      const { rerender } = renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('Recent projects unavailable')).toBeInTheDocument()

      rerender(<ProjectPicker open={false} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />)
      rerender(<ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />)
      await waitFor(() =>
        expect(screen.queryByText('Recent projects unavailable')).not.toBeInTheDocument())
    })

    it('recovers both failed reads by reopening, and offers a retry beside each notice', async () => {
      // Reopening is still a recovery path, so if the open effect stopped re-reading these
      // failure states would regress; the Retry controls make one of them discoverable.
      vi.mocked(api.recentProjects).mockRejectedValue(new Error('nope'))
      vi.mocked(api.browseDirs).mockRejectedValue(new Error('nope'))
      const { rerender } = renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByText('Recent projects unavailable')).toBeInTheDocument()
      await screen.findByText('Unable to list folder')
      expect(screen.getAllByRole('button', { name: /^Retry: / })).toHaveLength(2)

      const recentBefore = vi.mocked(api.recentProjects).mock.calls.length
      const browseBefore = vi.mocked(api.browseDirs).mock.calls.length
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: ['/home/u/projA'] })
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', [
        { name: 'beta', path: '/home/u/beta' },
      ]))
      rerender(
        <ProjectPicker open={false} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      rerender(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      await waitFor(() =>
        expect(vi.mocked(api.recentProjects).mock.calls.length).toBeGreaterThan(recentBefore))
      await waitFor(() =>
        expect(vi.mocked(api.browseDirs).mock.calls.length).toBeGreaterThan(browseBefore))
      await waitFor(() =>
        expect(screen.queryByText('Recent projects unavailable')).not.toBeInTheDocument())
    })

    it('surfaces a browse failure instead of showing it as an empty directory', async () => {
      // The listing is deadline-bound, so a wedged gateway rejects rather than hanging.
      // "No subdirectories" would report a folder as empty on a listing that never arrived.
      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      vi.mocked(api.browseDirs).mockRejectedValue(new Error('nope'))
      renderWithProviders(
        <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
      )
      expect(await screen.findByRole('alert')).toHaveTextContent(/Unable to list folder/)
      expect(screen.queryByText('No subdirectories')).toBeNull()
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
      await waitFor(() => expect(browseSpy).toHaveBeenCalledWith('/home/u/beta', expect.any(AbortSignal)))
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
      expect(browseSpy).toHaveBeenCalledWith('/home/u/workplace', expect.any(AbortSignal))
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
      expect(browseSpy).toHaveBeenCalledWith('/home/u/workplace', expect.any(AbortSignal))
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

  describe('a background refetch does not rewrite what the user typed', () => {
    /**
     * The shipped client leaves `refetchOnWindowFocus` true so that a finite `staleTime`
     * opts a query INTO focus refetching, and this hook wants `staleTime: 0` because the
     * filesystem moves under it. Those two together used to mean an alt-tab re-read the
     * listing and fed it back through `onData`, which calls `setInput`. The test client
     * shares both properties, so it can see the same thing.
     */
    it('keeps a typed path across a window focus whose listing changed on disk', async () => {
      vi.spyOn(api, 'browseDirs').mockResolvedValue(
        mockBrowseDirs('/home/u', [{ name: 'alpha', path: '/home/u/alpha' }]),
      )
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      fireEvent.mouseDown(screen.getByText('Browse'))
      const box = await screen.findByPlaceholderText('/path/to/project')
      await waitFor(() => expect(api.browseDirs).toHaveBeenCalled())

      fireEvent.change(box, { target: { value: '/home/u/my-half-typed-pa' } })
      expect((box as HTMLInputElement).value).toBe('/home/u/my-half-typed-pa')

      // The directory changes on disk, so a refetch would yield a NEW data reference and
      // structural sharing cannot hand back the old one.
      vi.mocked(api.browseDirs).mockResolvedValue(
        mockBrowseDirs('/home/u', [
          { name: 'alpha', path: '/home/u/alpha' },
          { name: 'beta', path: '/home/u/beta' },
        ]),
      )
      // focusManager binds `visibilitychange` on WINDOW, and `waitFor` on an UNCHANGED
      // value returns on its first check -- so target and settle both decide discrimination.
      await act(async () => {
        window.dispatchEvent(new Event('visibilitychange'))
        await new Promise(r => setTimeout(r, 80))
      })

      expect((box as HTMLInputElement).value).toBe('/home/u/my-half-typed-pa')
    })

    it('reads nothing more once closed', async () => {
      const { rerender } = renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      await waitFor(() => expect(api.browseDirs).toHaveBeenCalled())

      rerender(
        <ProjectPicker open={false} onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      const after = vi.mocked(api.browseDirs).mock.calls.length
      // A settle window: an assertion that something did NOT happen returns on its first check
      // otherwise, which would pass against a picker that reads on every render.
      await new Promise(r => setTimeout(r, 50))
      expect(vi.mocked(api.browseDirs).mock.calls.length).toBe(after)
    })

    it('switches to Recent and lands focus there when a recents retry recovers', async () => {
      vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('deadline exceeded'))
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      // The failure itself lands the user on Browse, where the recents search input never mounts.
      await screen.findByText('Recent projects unavailable')
      const browseInput = screen.getByRole('combobox')

      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: ['/home/u/projA'] })
      const retry = screen.getByRole('button', { name: 'Retry: Recent projects unavailable' })
      // A keyboard user reaches the button, so focus is ON it when the recovery unmounts it.
      retry.focus()
      expect(document.activeElement).toBe(retry)
      fireEvent.click(retry)

      await waitFor(() =>
        expect(screen.queryByText('Recent projects unavailable')).not.toBeInTheDocument())
      // The rows the click recovered have to be ON SCREEN: leaving an un-picked user on Browse
      // would hide them behind a tab she never chose, so the click delivered nothing visible.
      const recentSearch = await screen.findByLabelText('Search recent projects')
      expect(browseInput).not.toBeInTheDocument()
      expect(document.activeElement).toBe(recentSearch)
      expect(document.activeElement).not.toBe(document.body)
    })

    it('stays on Browse when a recents retry recovers with NO rows', async () => {
      // Control for the switch above: it mirrors the landing rule rather than switching blind,
      // and empty recents belong on Browse — landing there would report "no recent projects".
      vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('deadline exceeded'))
      vi.mocked(api.browseDirs).mockResolvedValue(mockBrowseDirs('/home/u', []))
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      await screen.findByText('Recent projects unavailable')
      const browseInput = screen.getByRole('combobox')

      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: [] })
      fireEvent.click(screen.getByRole('button', { name: 'Retry: Recent projects unavailable' }))

      await waitFor(() =>
        expect(screen.queryByText('Recent projects unavailable')).not.toBeInTheDocument())
      expect(browseInput).toBeInTheDocument()
      expect(screen.queryByLabelText('Search recent projects')).not.toBeInTheDocument()
    })

    it('offers a retry on the recents notice that re-runs the read', async () => {
      vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('deadline exceeded'))
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      await screen.findByText('Recent projects unavailable')
      const before = vi.mocked(api.recentProjects).mock.calls.length

      vi.mocked(api.recentProjects).mockResolvedValue({ dirs: ['/home/u/projA'] })
      fireEvent.click(screen.getByRole('button', { name: 'Retry: Recent projects unavailable' }))

      await waitFor(() => {
        expect(vi.mocked(api.recentProjects).mock.calls.length).toBeGreaterThan(before)
      })
    })

    /**
     * Pressing Retry disables it, which blurs it to <body>. A recovery unmounts the button so
     * focus is handed elsewhere deliberately, but a REPEATED FAILURE leaves it mounted and
     * re-enabled with focus still on <body> -- so a keyboard user has to blind-Tab back for
     * every attempt. Both notices are asserted because they carry separate buttons.
     */
    it('hands focus back to the recents Retry when the re-read fails again', async () => {
      vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('deadline exceeded'))
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      await screen.findByText('Recent projects unavailable')
      const retry = screen.getByRole('button', {
        name: 'Retry: Recent projects unavailable',
      })
      retry.focus()
      expect(retry).toHaveFocus()

      // Held in flight so the blur lands while the button is disabled. Rejecting at once lets
      // the settle win the race, and the blur then undoes the focus restore under test.
      let failRetry!: (e: Error) => void
      vi.mocked(api.recentProjects).mockImplementationOnce(
        () => new Promise((_res, rej) => { failRetry = rej }),
      )
      fireEvent.click(retry)
      // happy-dom neither blurs a disabling button nor honours `.blur()` on a disabled one, so
      // focus is moved to a real sibling: what matters is that it is NOT on the button.
      act(() => { (screen.getByText('Browse') as HTMLButtonElement).focus() })
      expect(retry).not.toHaveFocus()

      await act(async () => { failRetry(new Error('deadline exceeded')) })
      // Re-queried rather than compared to `retry`: the notice remounts, so React hands back a
      // NEW node carrying the same accessible name, and identity would fail on a working fix.
      expect(screen.getByRole('button', {
        name: 'Retry: Recent projects unavailable',
      })).toHaveFocus()
    })

    it('hands focus back to the listing Retry when the re-read fails again', async () => {
      vi.mocked(api.browseDirs).mockRejectedValue(new Error('deadline exceeded'))
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      fireEvent.mouseDown(screen.getByText('Browse'))
      await screen.findByText('Unable to list folder')
      const retry = screen.getByRole('button', {
        name: 'Retry: Unable to list folder',
      })
      retry.focus()

      fireEvent.click(retry)
      await waitFor(() => expect(retry).not.toBeDisabled())
      expect(document.activeElement).not.toBe(document.body)
      expect(screen.getByRole('button', {
        name: 'Retry: Unable to list folder',
      })).toHaveFocus()
    })
    /**
     * FilePickerMenu withholds Retry from a 403/404 because re-asking returns the same answer.
     * The listings classified every failure as recoverable, so they offered a button that could
     * only spin and fail -- opposite rules for the same refusal inside one PR.
     */
    it('withholds the listing Retry on a refusal but offers it on a transient failure', async () => {
      const { ApiError } = await import('../api/apiError')
      vi.mocked(api.browseDirs).mockRejectedValue(
        new ApiError(403, 'no', JSON.stringify({ error: 'Access denied', code: 'access_denied' })),
      )
      const { unmount } = renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      fireEvent.mouseDown(screen.getByText('Browse'))
      await screen.findByText('Unable to list folder')
      expect(screen.queryByRole('button', { name: /^Retry: / })).not.toBeInTheDocument()
      unmount()

      // Positive control: the same surface, a codeless 500, still carries the button -- so the
      // assertion above is about the CAUSE and not about the button having gone missing.
      vi.mocked(api.browseDirs).mockRejectedValue(new ApiError(500, 'boom', '{}'))
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      fireEvent.mouseDown(screen.getByText('Browse'))
      await screen.findByText('Unable to list folder')
      expect(screen.getByRole('button', { name: 'Retry: Unable to list folder' })).toBeInTheDocument()
    })
    /**
     * `refetch` on a key that never succeeded takes react-query's `fetch` reducer, which clears
     * `error` -- so gating the listing notice on the error alone unmounted it, its Retry and the
     * row-clearing for the whole multi-second read, dropping focus to <body> and flashing stale
     * rows back. The sibling recents notice already guarded against exactly this.
     */
    it('holds the listing notice and its Retry mounted for the whole in-flight retry', async () => {
      vi.mocked(api.browseDirs).mockRejectedValue(new Error('deadline exceeded'))
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      fireEvent.mouseDown(screen.getByText('Browse'))
      await screen.findByText('Unable to list folder')

      // Held unsettled so the assertions land DURING the read, which is the whole window the
      // finding is about; a mock that rejects at once never enters that state.
      let settle!: () => void
      vi.mocked(api.browseDirs).mockImplementationOnce(
        () => new Promise((_res, rej) => { settle = () => rej(new Error('deadline exceeded')) }),
      )
      fireEvent.click(screen.getByRole('button', { name: 'Retry: Unable to list folder' }))

      const busy = screen.getByRole('button', { name: 'Retry: Unable to list folder' })
      expect(busy).toHaveAttribute('aria-busy', 'true')
      expect(screen.getByText('Unable to list folder')).toBeInTheDocument()
      expect(screen.queryByText('No subdirectories')).not.toBeInTheDocument()

      await act(async () => { settle() })
      expect(screen.getByRole('button', { name: 'Retry: Unable to list folder' })).toBeInTheDocument()
    })
  })

  describe('a reopened drill re-reads the directory instead of serving cached rows', () => {
    /**
     * Reopening after a subdir drill changes the query key back to the root, which react-query
     * serves from cache before its refetch starts. A guard that spent the drill on that cached
     * value suppressed the refetch that followed, so a directory changed on disk kept offering
     * rows that no longer existed.
     */
    it('shows rows created since the last visit when the picker is reopened', async () => {
      vi.mocked(api.browseDirs).mockImplementation(async (path?: string) =>
        path === '/home/u/alpha'
          ? mockBrowseDirs('/home/u/alpha', [])
          : mockBrowseDirs('/home/u', [{ name: 'alpha', path: '/home/u/alpha' }]))

      const { rerender } = renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      fireEvent.mouseDown(screen.getByText('Browse'))
      const alpha = await screen.findByText('alpha')
      fireEvent.click(alpha)
      await waitFor(() => expect(api.browseDirs).toHaveBeenCalledWith('/home/u/alpha', expect.any(AbortSignal)))

      rerender(
        <ProjectPicker open={false} onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      // The root gains a directory while the picker is shut.
      vi.mocked(api.browseDirs).mockImplementation(async () =>
        mockBrowseDirs('/home/u', [
          { name: 'alpha', path: '/home/u/alpha' },
          { name: 'brand-new', path: '/home/u/brand-new' },
        ]))
      rerender(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      fireEvent.mouseDown(screen.getByText('Browse'))

      expect(await screen.findByText('brand-new')).toBeInTheDocument()
    })

    it('retries the failed read on the first click, with nothing yet succeeded', async () => {
      // The old call passed the last SUCCESSFUL path, which is '' before anything succeeds --
      // and '' and undefined share one query key, so the click fetched nothing at all.
      vi.mocked(api.browseDirs).mockRejectedValue(new Error('nope'))
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      fireEvent.mouseDown(screen.getByText('Browse'))
      await screen.findByText('Unable to list folder')
      const before = vi.mocked(api.browseDirs).mock.calls.length

      vi.mocked(api.browseDirs).mockResolvedValue(
        mockBrowseDirs('/home/u', [{ name: 'recovered', path: '/home/u/recovered' }]),
      )
      const listingRetry = screen.getAllByRole('button', { name: /^Retry: / })
      fireEvent.click(listingRetry[listingRetry.length - 1])

      await waitFor(() =>
        expect(vi.mocked(api.browseDirs).mock.calls.length).toBeGreaterThan(before))
      expect(await screen.findByText('recovered')).toBeInTheDocument()
    })


    it('reports the Retry control busy while its re-read is outstanding', async () => {
      // First read fails so the notice appears; the second is held open -- without an
      // acknowledgement that window is pixel-identical to the state before the click.
      let release: (v: { dirs: string[] }) => void = () => {}
      const held = new Promise<{ dirs: string[] }>(r => { release = r })
      let calls = 0
      vi.mocked(api.recentProjects).mockImplementation(() => {
        calls += 1
        return calls === 1 ? Promise.reject(new Error('nope')) : held
      })
      vi.mocked(api.browseDirs).mockRejectedValue(new Error('nope'))
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      await screen.findByText('Recent projects unavailable')
      const btn = screen.getByRole('button', { name: 'Retry: Recent projects unavailable' })
      expect(btn).not.toBeDisabled()

      fireEvent.click(btn)
      await waitFor(() => expect(btn).toBeDisabled())
      expect(btn).toHaveAttribute('aria-busy', 'true')

      // On success the notice is gone, so the button goes with it -- asserting "not disabled"
      // would probe a detached node, which keeps its last rendered attributes forever.
      await act(async () => { release({ dirs: ['/home/u/projA'] }); await Promise.resolve() })
      await waitFor(() =>
        expect(screen.queryByText('Recent projects unavailable')).not.toBeInTheDocument())
    })

    it('gives the two Retry controls distinct accessible names', async () => {
      // Both buttons show the word "Retry", so a screen reader hears it twice and the
      // adjacency that says which is which is purely visual.
      vi.mocked(api.recentProjects).mockRejectedValue(new Error('nope'))
      vi.mocked(api.browseDirs).mockRejectedValue(new Error('nope'))
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      await screen.findByText('Recent projects unavailable')
      await screen.findByText('Unable to list folder')

      expect(screen.getByRole('button', { name: 'Retry: Recent projects unavailable' }))
        .toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Retry: Unable to list folder' }))
        .toBeInTheDocument()
      expect(screen.queryAllByRole('button', { name: 'Retry' })).toHaveLength(0)
    })

    it('gives every Retry a hover affordance that names a defined colour', async () => {
      // `fg` is not a colour and the colour named `bg-hover` needs the `bg-` utility prefix, so
      // `hover:text-fg` / `hover:bg-hover` compile to nothing and the button never reacts.
      vi.mocked(api.recentProjects).mockRejectedValue(new Error('nope'))
      vi.mocked(api.browseDirs).mockRejectedValue(new Error('nope'))
      renderWithProviders(
        <ProjectPicker open onOpenChange={() => {}} anchorRect={rect(10, 10)} onSelect={() => {}} />,
      )
      await screen.findByText('Recent projects unavailable')
      const buttons = screen.getAllByRole('button', { name: /^Retry: / })
      expect(buttons.length).toBeGreaterThan(0)
      for (const b of buttons) {
        expect(b.className).toContain('hover:bg-bg-hover')
        expect(b.className).toContain('hover:border-border-strong')
        expect(b.className).not.toContain('hover:text-fg')
        expect(b.className).not.toMatch(/hover:bg-hover(\s|$)/)
      }
    })
  })
})
