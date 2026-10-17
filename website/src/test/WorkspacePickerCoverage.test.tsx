import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { renderWithProviders } from './helpers'
import WorkspacePicker from '../components/WorkspacePicker'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'

type BrowseDirsResult = Awaited<ReturnType<typeof api.browseDirs>>
type PickerProps = ComponentProps<typeof WorkspacePicker>

const DIRS = [
  { name: 'alpha', path: '/home/u/alpha' },
  { name: 'beta', path: '/home/u/beta' },
]

const browseResult = (
  path = '/home/u',
  parent = '/home',
  dirs: { name: string; path: string }[] = DIRS,
): BrowseDirsResult => ({ path, parent, dirs })

/** happy-dom does not expose a DOMRect constructor; build the shape by hand. */
const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height,
  bottom: top + height,
  right: left + width,
  x: left, y: top,
  toJSON: () => ({}),
} as DOMRect)

/**
 * The picker bails out unless `anchorRef.current` is a live element, and its
 * click-outside guard calls `anchor.contains(target)`. A detached node would
 * make every outside-click assertion vacuous, so the anchor is a real button
 * mounted in the document for the duration of each test.
 */
let anchor: HTMLButtonElement
let anchorRef: { current: HTMLElement | null }

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  anchor = document.createElement('button')
  anchor.textContent = 'anchor'
  anchor.setAttribute('data-testid', 'anchor-btn')
  document.body.appendChild(anchor)
  anchorRef = { current: anchor }
  Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
  vi.spyOn(api, 'browseDirs').mockResolvedValue(browseResult())
  vi.spyOn(api, 'createWorkspace').mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  anchor.remove()
  vi.restoreAllMocks()
})

function renderPicker(overrides: Partial<PickerProps> = {}) {
  const onOpenChange = vi.fn()
  const onCreated = vi.fn()
  const utils = renderWithProviders(
    <WorkspacePicker
      open={true}
      onOpenChange={onOpenChange}
      anchorRef={anchorRef}
      onCreated={onCreated}
      {...overrides}
    />,
  )
  return { onOpenChange, onCreated, ...utils }
}

/** Wait for the initial browse() response to land. */
const pathInput = () => screen.findByLabelText('Project directory path')
const nameInput = () => screen.findByLabelText('Workspace name')

/** Let the deferred click-outside listener attach (component uses setTimeout 0). */
async function attachOutsideListener() {
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
}

/** Render, then drive the browse view into the create view for `dir`. */
async function enterCreateView(dir = '/home/u/alpha') {
  const handles = renderPicker()
  const input = await pathInput()
  fireEvent.change(input, { target: { value: dir } })
  fireEvent.click(screen.getByRole('button', { name: 'Select' }))
  await nameInput()
  return handles
}

describe('WorkspacePicker', () => {
  describe('visibility', () => {
    it('renders nothing when closed', async () => {
      renderPicker({ open: false })
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(screen.queryByRole('button', { name: 'Select' })).not.toBeInTheDocument()
      expect(api.browseDirs).not.toHaveBeenCalled()
    })

    it('renders nothing when the anchor element is not mounted yet', async () => {
      renderPicker({ anchorRef: { current: null } })
      await waitFor(() => expect(api.browseDirs).toHaveBeenCalled())
      expect(screen.queryByRole('button', { name: 'Select' })).not.toBeInTheDocument()
    })

    it('renders the browse view when open with a live anchor', async () => {
      renderPicker()
      expect(await pathInput()).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Select' })).toBeInTheDocument()
    })
  })

  describe('directory browsing', () => {
    it('loads the default directory and seeds the path input with it', async () => {
      renderPicker()
      // The input is on screen from mount; the value is what proves the opening read landed.
      await waitFor(() => expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u'))
      expect(api.browseDirs).toHaveBeenCalledWith(undefined)
    })

    it('lists the returned subdirectories', async () => {
      renderPicker()
      expect(await screen.findByText('alpha')).toBeInTheDocument()
      expect(screen.getByText('beta')).toBeInTheDocument()
      expect(screen.queryByText('No subdirectories')).not.toBeInTheDocument()
    })

    it('descends into a subdirectory when its row is clicked', async () => {
      renderPicker()
      const row = (await screen.findByText('beta')).closest('button') as HTMLElement
      vi.mocked(api.browseDirs).mockResolvedValue(
        browseResult('/home/u/beta', '/home/u', [{ name: 'nested', path: '/home/u/beta/nested' }]),
      )
      fireEvent.click(row)
      expect(await screen.findByText('nested')).toBeInTheDocument()
      expect(api.browseDirs).toHaveBeenLastCalledWith('/home/u/beta')
    })

    it('offers a parent button that browses upward', async () => {
      renderPicker()
      const up = await screen.findByLabelText('Back')
      vi.mocked(api.browseDirs).mockResolvedValue(
        browseResult('/home', '/', [{ name: 'u', path: '/home/u' }]),
      )
      fireEvent.click(up)
      await waitFor(() => expect(api.browseDirs).toHaveBeenLastCalledWith('/home'))
    })

    it('hides the parent button at the filesystem root', async () => {
      vi.mocked(api.browseDirs).mockResolvedValue(browseResult('/', '/', []))
      renderPicker()
      await waitFor(() => expect(screen.getByLabelText('Project directory path')).toHaveValue('/'))
      expect(screen.queryByLabelText('Back')).not.toBeInTheDocument()
    })

    it('shows the empty state when a directory has no children', async () => {
      vi.mocked(api.browseDirs).mockResolvedValue(browseResult('/home/u', '/home', []))
      renderPicker()
      expect(await screen.findByText('No subdirectories')).toBeInTheDocument()
    })

    it('ignores an older browse failure after a newer browse has succeeded', async () => {
      renderPicker()
      await screen.findByText('alpha')

      let rejectOlder!: (reason?: unknown) => void
      const older = new Promise<BrowseDirsResult>((_resolve, reject) => { rejectOlder = reject })
      let resolveNewer!: (value: BrowseDirsResult) => void
      const newer = new Promise<BrowseDirsResult>(resolve => { resolveNewer = resolve })
      vi.mocked(api.browseDirs)
        .mockReturnValueOnce(older)
        .mockReturnValueOnce(newer)

      fireEvent.click(screen.getByText('alpha'))
      fireEvent.click(screen.getByText('beta'))
      await act(async () => {
        resolveNewer(browseResult('/home/u/beta', '/home/u', [
          { name: 'newest', path: '/home/u/beta/newest' },
        ]))
      })
      expect(await screen.findByText('newest')).toBeInTheDocument()

      await act(async () => { rejectOlder(new Error('older listing failed')) })
      expect(screen.getByText('newest')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    })

    it('surfaces a browse failure without claiming the directory is empty', async () => {
      vi.mocked(api.browseDirs).mockRejectedValue(new Error('nope'))
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Unable to list folder')
      expect(screen.queryByText('No subdirectories')).not.toBeInTheDocument()
      expect(screen.getByLabelText('Project directory path')).toHaveValue('')
    })

    it('offers Retry beside the failure, which re-asks for the same listing and clears the notice when it lands', async () => {
      // The notice named the cause but no remedy, and this surface has no Refresh, so a
      // wedged gateway that recovered left the user closing and reopening the picker.
      vi.mocked(api.browseDirs)
        .mockRejectedValueOnce(Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }))
        .mockResolvedValue(browseResult())
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')
      expect(api.browseDirs).toHaveBeenCalledTimes(1)

      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      // The first open asked for the default listing; with no browse position yet, so does Retry.
      expect(api.browseDirs).toHaveBeenCalledTimes(2)
      expect(api.browseDirs).toHaveBeenLastCalledWith(undefined)
      expect(await screen.findByText('alpha')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()

      // The recovered listing drills as any other.
      fireEvent.click(screen.getByText('alpha'))
      expect(api.browseDirs).toHaveBeenLastCalledWith('/home/u/alpha')
    })

    it('Retry re-lists the child whose drill failed, and the notice names that child', async () => {
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(browseResult('/home/u', '/home', [
          { name: 'x', path: '/home/u/x' },
        ]))
        .mockRejectedValueOnce(Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }))
        .mockResolvedValueOnce(browseResult('/home/u/x', '/home/u', []))
      renderPicker()
      fireEvent.click(await screen.findByText('x'))
      // The input and the rows still show /home/u, so the notice names the path the FAILED read
      // asked for -- the one Retry re-asks -- not the shared pathless copy.
      expect(await screen.findByRole('alert')).toHaveTextContent('Opening /home/u/x timed out. Retry, or pick another folder.')
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u')

      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      expect(api.browseDirs).toHaveBeenLastCalledWith('/home/u/x')
      await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    })

    it('Retry after a NON-timeout failure re-asks for the same listing too', async () => {
      // The `failed` arm offers Retry exactly as the timeout arm does, so it has to re-ask as well.
      vi.mocked(api.browseDirs)
        .mockRejectedValueOnce(new Error('Failed to fetch'))
        .mockResolvedValue(browseResult())
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Unable to list folder')

      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      expect(api.browseDirs).toHaveBeenCalledTimes(2)
      expect(api.browseDirs).toHaveBeenLastCalledWith(undefined)
      expect(await screen.findByText('alpha')).toBeInTheDocument()
    })

    it('does not offer Retry for an access-denied listing refusal', async () => {
      vi.mocked(api.browseDirs).mockRejectedValue(
        new ApiError(403, 'Access denied', JSON.stringify({ error: 'Access denied', code: 'access_denied' })),
      )
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('No access to this folder')
      expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    })

    it('an access-denied DRILL names the folder that refused and the next step, still without Retry', async () => {
      // The input and the rows keep the listing the drill left from, so a notice reading
      // "No access to this folder" pointed at the wrong folder: the one still on screen.
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(browseResult())
        .mockRejectedValueOnce(
          new ApiError(403, 'Access denied', JSON.stringify({ error: 'Access denied', code: 'access_denied' })),
        )
      renderPicker()
      fireEvent.click(await screen.findByText('alpha'))

      expect(await screen.findByRole('alert')).toHaveTextContent('No access to /home/u/alpha. Type another path above.')
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u')
      expect(screen.getByText('beta')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    })

    it('a not-found DRILL names the folder that refused and the next step, still without Retry', async () => {
      // `not_a_directory` is the refusal `/api/browse-dirs` actually emits for a row that
      // stopped being a directory; it classifies as `root_missing`, the other permanent arm.
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(browseResult())
        .mockRejectedValueOnce(
          new ApiError(400, 'Not a directory', JSON.stringify({ error: 'Not a directory', code: 'not_a_directory', path: '/home/u/beta' })),
        )
      renderPicker()
      fireEvent.click(await screen.findByText('beta'))

      expect(await screen.findByRole('alert')).toHaveTextContent('/home/u/beta was not found. Type another path above.')
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u')
      expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    })

    it('shows the retry in progress and makes the control inert until the re-asked listing settles', async () => {
      // A listing can honestly take most of its bound. With no in-flight state, the timeout
      // notice and an enabled Retry sat unchanged for that whole wait, so a second press was
      // the natural read -- and each press took a fresh ticket and restarted the wait.
      const timeout = Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
      vi.mocked(api.browseDirs).mockRejectedValueOnce(timeout)
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')

      let settleRetry!: (value: BrowseDirsResult) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>(resolve => { settleRetry = resolve }),
      )
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      expect(api.browseDirs).toHaveBeenCalledTimes(2)

      // In flight: the control says so and takes no second press, so the ticket stands.
      const inFlight = screen.getByRole('button', { name: 'Retrying…' })
      expect(inFlight).toHaveAttribute('aria-disabled', 'true')
      expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
      fireEvent.click(inFlight)
      expect(api.browseDirs).toHaveBeenCalledTimes(2)

      await act(async () => { settleRetry(browseResult()) })
      expect(await screen.findByText('alpha')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Retrying…' })).not.toBeInTheDocument()
    })

    it('a keyboard-activated Retry keeps focus for the whole re-ask, inert but still in the tab order', async () => {
      // Inertness is `aria-disabled` plus the in-handler guard, NOT `disabled` -- the FolderPanel
      // precedent. A `disabled` button leaves the tab order, so a real browser's focus fixup
      // dropped `document.activeElement` to <body> the moment a keyboard press started the
      // re-ask, and the user had to Tab in from the top of the popover to press it again once
      // it failed. happy-dom runs no focus fixup, so the tab-order half of the contract is the
      // absent `disabled` attribute; the focus half holds in this DOM and in a browser alike.
      const timeout = () => Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
      vi.mocked(api.browseDirs).mockRejectedValueOnce(timeout())
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')

      let failRetry!: (reason: unknown) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>((_resolve, reject) => { failRetry = reject }),
      )
      const retry = screen.getByRole('button', { name: 'Retry' })
      retry.focus()
      expect(retry).toHaveFocus()
      // A keyboard activation of a button arrives as a click.
      fireEvent.click(retry)
      expect(api.browseDirs).toHaveBeenCalledTimes(2)

      // In flight: the same element, relabelled, still focusable, still focused, and inert.
      const inFlight = screen.getByRole('button', { name: 'Retrying…' })
      expect(inFlight).toBe(retry)
      expect(inFlight).toBeEnabled()
      expect(inFlight).toHaveAttribute('aria-disabled', 'true')
      expect(inFlight).toHaveFocus()
      expect(inFlight.closest('[aria-busy]')).toHaveAttribute('aria-busy', 'true')
      fireEvent.click(inFlight)
      expect(api.browseDirs).toHaveBeenCalledTimes(2)

      // The re-ask fails: Retry is live again under the user's focus, one press away.
      await act(async () => { failRetry(timeout()) })
      const again = await screen.findByRole('button', { name: 'Retry' })
      expect(again).toBe(retry)
      expect(again).not.toHaveAttribute('aria-disabled')
      expect(again).toHaveFocus()
      fireEvent.click(again)
      expect(api.browseDirs).toHaveBeenCalledTimes(3)
    })

    it('re-enables Retry when the re-asked listing fails again', async () => {
      const timeout = () => Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })
      vi.mocked(api.browseDirs).mockRejectedValueOnce(timeout())
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')

      let failRetry!: (reason: unknown) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>((_resolve, reject) => { failRetry = reject }),
      )
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      expect(screen.getByRole('button', { name: 'Retrying…' })).toHaveAttribute('aria-disabled', 'true')

      await act(async () => { failRetry(timeout()) })
      const retry = await screen.findByRole('button', { name: 'Retry' })
      expect(retry).toBeEnabled()
      expect(retry).not.toHaveAttribute('aria-disabled')
      expect(screen.getByRole('alert')).toHaveTextContent('Folder listing timed out')
    })

    it('a STALE settlement neither re-enables Retry nor replaces the notice; only the ticketed one does', async () => {
      renderPicker()
      await screen.findByText('alpha')

      // Two drills in a row: the first is superseded before either settles.
      let settleStale!: (value: BrowseDirsResult) => void
      let failCurrent!: (reason: unknown) => void
      vi.mocked(api.browseDirs)
        .mockReturnValueOnce(new Promise<BrowseDirsResult>(resolve => { settleStale = resolve }))
        .mockReturnValueOnce(new Promise<BrowseDirsResult>((_resolve, reject) => { failCurrent = reject }))
      fireEvent.click(screen.getByText('alpha'))
      fireEvent.click(screen.getByText('beta'))
      await act(async () => {
        failCurrent(Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' }))
      })
      expect(await screen.findByRole('alert')).toHaveTextContent('Opening /home/u/beta timed out')

      let settleRetry!: (value: BrowseDirsResult) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>(resolve => { settleRetry = resolve }),
      )
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      expect(api.browseDirs).toHaveBeenLastCalledWith('/home/u/beta')
      expect(screen.getByRole('button', { name: 'Retrying…' })).toHaveAttribute('aria-disabled', 'true')

      // The superseded drill lands now: it must not read as the retry having settled.
      await act(async () => {
        settleStale(browseResult('/home/u/alpha', '/home/u', [{ name: 'stale', path: '/home/u/alpha/stale' }]))
      })
      expect(screen.getByRole('button', { name: 'Retrying…' })).toHaveAttribute('aria-disabled', 'true')
      expect(screen.getByRole('alert')).toHaveTextContent('Opening /home/u/beta timed out')
      expect(screen.queryByText('stale')).not.toBeInTheDocument()

      await act(async () => {
        settleRetry(browseResult('/home/u/beta', '/home/u', [{ name: 'fresh', path: '/home/u/beta/fresh' }]))
      })
      expect(await screen.findByText('fresh')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Retrying…' })).not.toBeInTheDocument()
    })
  })

  describe('editing the path after a listing failure', () => {
    // Retry re-asks the path that FAILED (`failedBrowsePath`), never the input's value. With the
    // input's onChange leaving the failure alone, a user who corrected the path and pressed the
    // adjacent Retry re-asked the old broken path under a notice about it, and a refusal kept
    // naming a path they had already replaced. ProjectPicker clears its failure on every
    // keystroke: typing is the recovery.
    const timeout = () => Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })

    it('a keystroke in the path input clears the failure notice and its Retry without asking for anything', async () => {
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(browseResult())
        .mockRejectedValueOnce(timeout())
      renderPicker()
      fireEvent.click(await screen.findByText('alpha'))
      expect(await screen.findByRole('alert')).toHaveTextContent('Opening /home/u/alpha timed out')
      expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
      expect(api.browseDirs).toHaveBeenCalledTimes(2)

      fireEvent.change(screen.getByLabelText('Project directory path'), { target: { value: '/home/u/beta' } })
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
      // Typing asks for nothing: the typed path stands and the rows still filter against it.
      expect(api.browseDirs).toHaveBeenCalledTimes(2)
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/beta')
      expect(screen.getByText('beta')).toBeInTheDocument()
    })

    it('a keystroke clears a refusal that named the replaced path', async () => {
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(browseResult())
        .mockRejectedValueOnce(
          new ApiError(403, 'Access denied', JSON.stringify({ error: 'Access denied', code: 'access_denied' })),
        )
      renderPicker()
      fireEvent.click(await screen.findByText('alpha'))
      expect(await screen.findByRole('alert')).toHaveTextContent('No access to /home/u/alpha.')

      fireEvent.change(screen.getByLabelText('Project directory path'), { target: { value: '/home/u/beta' } })
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/beta')
    })

    it('a keystroke retires the retry in flight: its late failure does not bring the notice back', async () => {
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(browseResult())
        .mockRejectedValueOnce(timeout())
      renderPicker()
      fireEvent.click(await screen.findByText('alpha'))
      expect(await screen.findByRole('alert')).toHaveTextContent('Opening /home/u/alpha timed out')

      let failRetry!: (reason: unknown) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>((_resolve, reject) => { failRetry = reject }),
      )
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      expect(api.browseDirs).toHaveBeenLastCalledWith('/home/u/alpha')
      expect(screen.getByRole('button', { name: 'Retrying…' })).toHaveAttribute('aria-disabled', 'true')

      fireEvent.change(screen.getByLabelText('Project directory path'), { target: { value: '/home/u/beta' } })
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Retrying…' })).not.toBeInTheDocument()

      // The request the user typed over lands now, as a failure: it describes a path they replaced.
      await act(async () => { failRetry(timeout()) })
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Retrying…' })).not.toBeInTheDocument()
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/beta')
      expect(api.browseDirs).toHaveBeenCalledTimes(3)
    })

    it('a keystroke during a drill claims the field, not the listing: the drill lands its rows and leaves the typed path alone', async () => {
      // The success arm runs `setInput(d.path)`, so a drill landing after the edit would erase what
      // was just typed (the ProjectPicker precedent). The rows are not the user's to keep: the
      // click asked for that listing, and it lands, filtered by the typed text. Dropping it
      // instead left `browsing` stuck and nothing in flight.
      renderPicker()
      await screen.findByText('alpha')
      let settleDrill!: (value: BrowseDirsResult) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>(resolve => { settleDrill = resolve }),
      )
      fireEvent.click(screen.getByText('alpha'))
      fireEvent.change(screen.getByLabelText('Project directory path'), { target: { value: '/home/u/alpha/l' } })

      await act(async () => {
        settleDrill(browseResult('/home/u/alpha', '/home/u', [
          { name: 'late', path: '/home/u/alpha/late' },
          { name: 'other', path: '/home/u/alpha/other' },
        ]))
      })
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/alpha/l')
      expect(screen.getByText('late')).toBeInTheDocument()
      expect(screen.queryByText('other')).not.toBeInTheDocument()
      expect(screen.queryByText('beta')).not.toBeInTheDocument()
      expect(api.browseDirs).toHaveBeenCalledTimes(2)
    })
  })

  describe('typing while the opening read is in flight', () => {
    // The field is `autoFocus`ed and the opening read can take most of its bound on a slow
    // gateway, so typing inside that round trip is ordinary. A keystroke claims the FIELD: a
    // listing that started before it lands its rows but never runs `setInput(d.path)` over what
    // was typed. It retires nothing -- retiring the only read in flight left the pane with no
    // rows, no empty-state, no notice, no Back and nothing that could start another listing
    // until the picker was closed and reopened.
    const timeout = () => Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })

    it('the read lands its rows, filtered by the typed text, and the typed path is preserved', async () => {
      let settleOpening!: (value: BrowseDirsResult) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>(resolve => { settleOpening = resolve }),
      )
      renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/al' } })

      await act(async () => { settleOpening(browseResult()) })
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/al')
      expect(screen.getByText('alpha')).toBeInTheDocument()
      expect(screen.queryByText('beta')).not.toBeInTheDocument()
      expect(screen.getByLabelText('Back')).toBeInTheDocument()
      expect(api.browseDirs).toHaveBeenCalledTimes(1)

      // The landed listing browses as any other: a row drills, and that drill -- asked for
      // after the keystroke -- seeds the field with its path as a drill always has.
      fireEvent.click(screen.getByText('alpha'))
      expect(api.browseDirs).toHaveBeenLastCalledWith('/home/u/alpha')
      await waitFor(() => expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u'))
    })

    it('an empty read lands its empty-state and Back, and the typed path is preserved', async () => {
      // "No subdirectories" describes a listing that WAS read: this one was, so it shows; only
      // the field is the user's.
      let settleOpening!: (value: BrowseDirsResult) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>(resolve => { settleOpening = resolve }),
      )
      renderPicker()
      const input = await pathInput()
      expect(screen.queryByText('No subdirectories')).not.toBeInTheDocument()
      fireEvent.change(input, { target: { value: '/home/u/typed' } })
      // Nothing has listed yet, so nothing is empty yet.
      expect(screen.queryByText('No subdirectories')).not.toBeInTheDocument()

      await act(async () => { settleOpening(browseResult('/home/u/alpha', '/home/u', [])) })
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/typed')
      expect(screen.getByText('No subdirectories')).toBeInTheDocument()
      expect(screen.getByLabelText('Back')).toBeInTheDocument()
      expect(api.browseDirs).toHaveBeenCalledTimes(1)
    })

    it('a failed read lands its notice and Retry: the pane is not dead, and the typed path is preserved', async () => {
      // The opening read names no path, so its notice is not about a path the user is replacing,
      // and with nothing listed it is the only remedy the pane has.
      let failOpening!: (reason: unknown) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>((_resolve, reject) => { failOpening = reject }),
      )
      renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/typed' } })

      await act(async () => { failOpening(timeout()) })
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')
      expect(screen.getByRole('button', { name: 'Retry' })).not.toHaveAttribute('aria-disabled')
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/typed')
      expect(screen.queryByText('No subdirectories')).not.toBeInTheDocument()

      // Retry re-asks the default listing and, asked for after the keystroke, seeds the field
      // with what it listed, as every user-initiated listing does.
      vi.mocked(api.browseDirs).mockResolvedValueOnce(browseResult())
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      expect(api.browseDirs).toHaveBeenLastCalledWith(undefined)
      expect(await screen.findByText('alpha')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u')
    })

    it('a keystroke leaves the pathless notice of a failed opening read in place: it names no path being replaced', async () => {
      // Typing clears a notice that names a path the user is replacing (a failed drill or Back).
      // The opening read's notice names none, and with nothing listed, clearing it would leave
      // the pane with no rows, no empty-state, no notice and nothing in flight.
      vi.mocked(api.browseDirs).mockRejectedValueOnce(timeout())
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')

      fireEvent.change(screen.getByLabelText('Project directory path'), { target: { value: '/home/u/typed' } })
      expect(screen.getByRole('alert')).toHaveTextContent('Folder listing timed out')
      expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/typed')
      expect(api.browseDirs).toHaveBeenCalledTimes(1)
    })

    it('a keystroke during the Retry of the opening read: its late failure still lands, its late success keeps the typed path', async () => {
      vi.mocked(api.browseDirs).mockRejectedValueOnce(timeout())
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')

      let failRetry!: (reason: unknown) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>((_resolve, reject) => { failRetry = reject }),
      )
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      expect(screen.getByRole('button', { name: 'Retrying…' })).toHaveAttribute('aria-disabled', 'true')
      fireEvent.change(screen.getByLabelText('Project directory path'), { target: { value: '/home/u/typed' } })
      // The re-ask is still in flight and still shown as such.
      expect(screen.getByRole('button', { name: 'Retrying…' })).toHaveAttribute('aria-disabled', 'true')

      await act(async () => { failRetry(timeout()) })
      expect(screen.getByRole('alert')).toHaveTextContent('Folder listing timed out')
      expect(screen.getByRole('button', { name: 'Retry' })).not.toHaveAttribute('aria-disabled')
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/typed')

      let settleRetry!: (value: BrowseDirsResult) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>(resolve => { settleRetry = resolve }),
      )
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      fireEvent.change(screen.getByLabelText('Project directory path'), { target: { value: '/home/u/typed/more' } })
      await act(async () => { settleRetry(browseResult()) })
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/typed/more')
      // The listing landed; the typed text just matches none of it.
      expect(screen.getByText('No subdirectories')).toBeInTheDocument()
      expect(screen.getByLabelText('Back')).toBeInTheDocument()
    })

    it('Select retires the read in flight outright: a late success seeds nothing on the create form', async () => {
      // The create form owns the field and the directory it shows; the browse pane's landing is
      // not for it. Back to the browse view re-runs the read (nothing had listed), and only that
      // re-run's rows land: the retired read left nothing behind. Both listings carry a row the
      // typed text matches, so neither could be hidden by the filter.
      let settleOpening!: (value: BrowseDirsResult) => void
      vi.mocked(api.browseDirs)
        .mockReturnValueOnce(new Promise<BrowseDirsResult>(resolve => { settleOpening = resolve }))
        .mockResolvedValueOnce(browseResult('/home/u', '/home', [{ name: 'typed-fresh', path: '/home/u/typed-fresh' }]))
      renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/typed' } })
      fireEvent.click(screen.getByRole('button', { name: 'Select' }))
      expect(await nameInput()).toHaveValue('typed')

      await act(async () => {
        settleOpening(browseResult('/home/u', '/home', [{ name: 'typed-stale', path: '/home/u/typed-stale' }]))
      })
      expect(screen.getByLabelText('Workspace name')).toHaveValue('typed')
      expect(screen.getByText('/home/u/typed')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Back' }))
      expect(await screen.findByText('typed-fresh')).toBeInTheDocument()
      expect(screen.queryByText('typed-stale')).not.toBeInTheDocument()
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/typed')
      expect(api.browseDirs).toHaveBeenCalledTimes(2)
    })

    it('Select retires the read in flight outright: a late failure paints no listing notice onto the create form', async () => {
      let failOpening!: (reason: unknown) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>((_resolve, reject) => { failOpening = reject }),
      )
      renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/typed' } })
      fireEvent.click(screen.getByRole('button', { name: 'Select' }))
      await nameInput()

      await act(async () => { failOpening(timeout()) })
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.getByLabelText('Workspace name')).toHaveValue('typed')
    })

    it('Escape retires the read in flight outright: a closing picker takes no late landing', async () => {
      // The parent owns `open`; here it is a mock, so the pane stays mounted and shows what a
      // late landing would have done to it.
      let settleOpening!: (value: BrowseDirsResult) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>(resolve => { settleOpening = resolve }),
      )
      const { onOpenChange } = renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/typed' } })
      fireEvent.keyDown(input, { key: 'Escape' })
      expect(onOpenChange).toHaveBeenCalledWith(false)

      await act(async () => { settleOpening(browseResult()) })
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/typed')
      expect(screen.queryByText('alpha')).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Back')).not.toBeInTheDocument()
    })

    it('a click outside retires the read in flight outright: its late failure raises no notice', async () => {
      let failOpening!: (reason: unknown) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>((_resolve, reject) => { failOpening = reject }),
      )
      const { onOpenChange } = renderPicker()
      const input = await pathInput()
      await attachOutsideListener()
      fireEvent.change(input, { target: { value: '/home/u/typed' } })
      const outside = document.createElement('div')
      document.body.appendChild(outside)
      fireEvent.mouseDown(outside)
      outside.remove()
      expect(onOpenChange).toHaveBeenCalledWith(false)

      await act(async () => { failOpening(timeout()) })
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/typed')
    })
  })

  describe('leaving the create form, and a Select with nothing to select', () => {
    // The same dead pane reached through Select instead of a keystroke. Select retires the
    // listing ticket and clears the notice, so returning to the browse pane with no listing
    // ever landed (`browsePath` '') left it with no rows, no empty-state, no notice and nothing
    // in flight; and a Select with nothing to select did the retiring and clearing without
    // even leaving the pane.
    const timeout = () => Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })

    it('Back from the create form after a failed opening read re-runs it: rows land and the typed path is kept', async () => {
      vi.mocked(api.browseDirs)
        .mockRejectedValueOnce(timeout())
        .mockResolvedValueOnce(browseResult())
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')
      const input = screen.getByLabelText('Project directory path')
      fireEvent.change(input, { target: { value: '/home/u/al' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(await nameInput()).toHaveValue('al')

      fireEvent.click(screen.getByRole('button', { name: 'Back' }))
      expect(api.browseDirs).toHaveBeenCalledTimes(2)
      expect(api.browseDirs).toHaveBeenLastCalledWith(undefined)
      expect(await screen.findByText('alpha')).toBeInTheDocument()
      // The field still holds what the user typed before Select, so the re-run does not seed it;
      // the typed text filters the landed rows as it would have had the first read landed.
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/al')
      expect(screen.queryByText('beta')).not.toBeInTheDocument()
      expect(screen.getByLabelText('Back')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it('Back from the create form whose re-run fails again shows the pathless notice and Retry', async () => {
      vi.mocked(api.browseDirs)
        .mockRejectedValueOnce(timeout())
        .mockRejectedValueOnce(timeout())
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')
      const input = screen.getByLabelText('Project directory path')
      fireEvent.change(input, { target: { value: '/home/u/typed' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      await nameInput()
      // Entering the create form cleared the listing notice: it is not a create failure.
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Back' }))
      expect(api.browseDirs).toHaveBeenCalledTimes(2)
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')
      expect(screen.getByRole('button', { name: 'Retry' })).not.toHaveAttribute('aria-disabled')
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/typed')
      expect(screen.queryByText('No subdirectories')).not.toBeInTheDocument()
    })

    it('Escape in the name input after Select retired the opening read re-runs it too', async () => {
      let settleOpening!: (value: BrowseDirsResult) => void
      vi.mocked(api.browseDirs)
        .mockReturnValueOnce(new Promise<BrowseDirsResult>(resolve => { settleOpening = resolve }))
        .mockResolvedValueOnce(browseResult('/home/u', '/home', []))
      renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/typed' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      await nameInput()
      // Select retired the opening read: its late landing is dropped, as before.
      await act(async () => { settleOpening(browseResult()) })

      fireEvent.keyDown(screen.getByLabelText('Workspace name'), { key: 'Escape' })
      expect(api.browseDirs).toHaveBeenCalledTimes(2)
      expect(await screen.findByText('No subdirectories')).toBeInTheDocument()
      expect(screen.getByLabelText('Back')).toBeInTheDocument()
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/typed')
    })

    it('the re-run supersedes a reopen\'s opening read still in flight on the create form: one listing lands', async () => {
      // A parent can close and reopen the popover while the create form is up (only a click
      // outside and a finished create reset it), and the reopen runs the opening read with the
      // create form still shown. Back then takes a new ticket for the same listing; the older
      // read's landing is ignored, and `browsing` follows the one that holds the ticket.
      let settleReopen!: (value: BrowseDirsResult) => void
      let settleRerun!: (value: BrowseDirsResult) => void
      vi.mocked(api.browseDirs)
        .mockRejectedValueOnce(timeout())
        .mockReturnValueOnce(new Promise<BrowseDirsResult>(resolve => { settleReopen = resolve }))
        .mockReturnValueOnce(new Promise<BrowseDirsResult>(resolve => { settleRerun = resolve }))
      const { rerender, onOpenChange, onCreated } = renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')
      const input = screen.getByLabelText('Project directory path')
      fireEvent.change(input, { target: { value: '/home/u/typed' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      await nameInput()

      rerender(<WorkspacePicker open={false} onOpenChange={onOpenChange} anchorRef={anchorRef} onCreated={onCreated} />)
      rerender(<WorkspacePicker open={true} onOpenChange={onOpenChange} anchorRef={anchorRef} onCreated={onCreated} />)
      expect(await nameInput()).toHaveValue('typed')
      expect(api.browseDirs).toHaveBeenCalledTimes(2)

      fireEvent.click(screen.getByRole('button', { name: 'Back' }))
      expect(api.browseDirs).toHaveBeenCalledTimes(3)
      await act(async () => {
        settleReopen(browseResult('/home/u', '/home', [{ name: 'typed-stale', path: '/home/u/typed-stale' }]))
      })
      expect(screen.queryByText('typed-stale')).not.toBeInTheDocument()

      await act(async () => {
        settleRerun(browseResult('/home/u', '/home', [{ name: 'typed-fresh', path: '/home/u/typed-fresh' }]))
      })
      expect(screen.getByText('typed-fresh')).toBeInTheDocument()
      expect(screen.queryByText('typed-stale')).not.toBeInTheDocument()
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/typed')
    })

    it('Back from the create form after a listing landed asks for nothing: the rows are still there', async () => {
      renderPicker()
      await screen.findByText('alpha')
      expect(api.browseDirs).toHaveBeenCalledTimes(1)
      fireEvent.change(screen.getByLabelText('Project directory path'), { target: { value: '/home/u/beta' } })
      fireEvent.click(screen.getByRole('button', { name: 'Select' }))
      await nameInput()

      fireEvent.click(screen.getByRole('button', { name: 'Back' }))
      expect(await pathInput()).toHaveValue('/home/u/beta')
      expect(screen.getByText('beta')).toBeInTheDocument()
      expect(api.browseDirs).toHaveBeenCalledTimes(1)
    })

    it('a click outside from the create form closes without re-running the read', async () => {
      vi.mocked(api.browseDirs).mockRejectedValueOnce(timeout())
      const { onOpenChange } = renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')
      await attachOutsideListener()
      const input = screen.getByLabelText('Project directory path')
      fireEvent.change(input, { target: { value: '/home/u/typed' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      await nameInput()

      const outside = document.createElement('div')
      document.body.appendChild(outside)
      fireEvent.mouseDown(outside)
      outside.remove()
      expect(onOpenChange).toHaveBeenCalledWith(false)
      expect(api.browseDirs).toHaveBeenCalledTimes(1)
    })

    it('Select with an empty field before the opening read lands does nothing: the read still lands its rows', async () => {
      let settleOpening!: (value: BrowseDirsResult) => void
      vi.mocked(api.browseDirs).mockReturnValueOnce(
        new Promise<BrowseDirsResult>(resolve => { settleOpening = resolve }),
      )
      renderPicker()
      await pathInput()
      fireEvent.click(screen.getByRole('button', { name: 'Select' }))
      expect(screen.queryByLabelText('Workspace name')).not.toBeInTheDocument()

      await act(async () => { settleOpening(browseResult()) })
      expect(screen.getByText('alpha')).toBeInTheDocument()
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u')
      expect(api.browseDirs).toHaveBeenCalledTimes(1)
    })

    it('Select with a blank field after a failed opening read does nothing: the notice and Retry stay', async () => {
      vi.mocked(api.browseDirs).mockRejectedValueOnce(timeout())
      renderPicker()
      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')
      fireEvent.change(screen.getByLabelText('Project directory path'), { target: { value: '   ' } })
      fireEvent.click(screen.getByRole('button', { name: 'Select' }))

      expect(screen.queryByLabelText('Workspace name')).not.toBeInTheDocument()
      expect(screen.getByRole('alert')).toHaveTextContent('Folder listing timed out')
      expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
      expect(api.browseDirs).toHaveBeenCalledTimes(1)
    })
  })

  describe('which failure hides the empty-state', () => {
    // The gate is the failure of the listing ON SCREEN, not any listing failure. A directory
    // that listed successfully empty keeps "No subdirectories" when a read of a DIFFERENT path
    // then fails: the rows and the path input still show that directory, and a blank list
    // region under the notice said nothing at all. This picker has no typed drill (Enter
    // selects), so the different path a user can reach from an empty listing is its parent,
    // through Back. The opening read (`browse()` with no path) keeps hiding it.
    const timeout = () => Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })

    it('a failed Back from an empty listing keeps that listing\'s empty-state beside the notice', async () => {
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(browseResult('/home/u/alpha', '/home/u', []))
        .mockRejectedValueOnce(timeout())
      renderPicker()
      // Back is gated on the landed parent, so it proves /home/u/alpha is the listing on screen
      // before Back is pressed.
      expect(await screen.findByLabelText('Back')).toBeInTheDocument()
      expect(screen.getByText('No subdirectories')).toBeInTheDocument()

      fireEvent.click(screen.getByLabelText('Back'))

      // Back asked for /home/u, so the notice names /home/u -- not the empty listing still shown.
      expect(await screen.findByRole('alert')).toHaveTextContent('Opening /home/u timed out')
      expect(api.browseDirs).toHaveBeenLastCalledWith('/home/u')
      // The listing on screen is still /home/u/alpha, read successfully and empty.
      expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/alpha')
      expect(screen.getByText('No subdirectories')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    })

    it('a reopen whose opening read fails hides the empty-state of the last open\'s empty rows', async () => {
      // The opposite failure mode, guarded: the opening read records no path, so it does not
      // name the shown listing by string -- but it IS that listing's re-read.
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(browseResult('/home/u/alpha', '/home/u', []))
        .mockRejectedValueOnce(timeout())
      const { rerender, onOpenChange, onCreated } = renderPicker()
      // Wait for the first read to land (the field names it) before closing, or the reopen's
      // read would be the one the mock rejects while the first is still in flight.
      await waitFor(() => expect(screen.getByLabelText('Project directory path')).toHaveValue('/home/u/alpha'))
      expect(screen.getByText('No subdirectories')).toBeInTheDocument()

      rerender(<WorkspacePicker open={false} onOpenChange={onOpenChange} anchorRef={anchorRef} onCreated={onCreated} />)
      rerender(<WorkspacePicker open={true} onOpenChange={onOpenChange} anchorRef={anchorRef} onCreated={onCreated} />)

      expect(await screen.findByRole('alert')).toHaveTextContent('Folder listing timed out')
      expect(api.browseDirs).toHaveBeenLastCalledWith(undefined)
      expect(screen.queryByText('No subdirectories')).not.toBeInTheDocument()
    })
  })

  describe('typed filtering', () => {
    it('narrows the list to entries matching the typed segment', async () => {
      renderPicker()
      // Land the listing before filtering it, so the filter is asserted against known rows.
      await screen.findByText('alpha')
      const input = screen.getByLabelText('Project directory path')
      fireEvent.change(input, { target: { value: '/home/u/al' } })
      expect(screen.getByText('alpha')).toBeInTheDocument()
      expect(screen.queryByText('beta')).not.toBeInTheDocument()
    })

    it('falls back to the empty state when nothing matches', async () => {
      renderPicker()
      // The empty-state describes a listing that was read, so land one before filtering it: the
      // path input is on screen at mount, and until the read lands nothing has been listed.
      await screen.findByText('alpha')
      const input = screen.getByLabelText('Project directory path')
      fireEvent.change(input, { target: { value: 'zzz' } })
      expect(screen.getByText('No subdirectories')).toBeInTheDocument()
    })

    it('keeps the full list when the input still equals the browsed path', async () => {
      renderPicker()
      // Land the listing before filtering it, so the filter is asserted against known rows.
      await screen.findByText('alpha')
      const input = screen.getByLabelText('Project directory path')
      fireEvent.change(input, { target: { value: '/HOME/U' } })
      expect(screen.getByText('alpha')).toBeInTheDocument()
      expect(screen.getByText('beta')).toBeInTheDocument()
    })
  })

  describe('positioning', () => {
    it('anchors below the trigger and clamps the left edge to the viewport', async () => {
      anchor.getBoundingClientRect = () => rect(100, 200)
      renderPicker()
      const drop = (await pathInput()).closest('div.fixed') as HTMLElement
      expect(drop.style.top).toBe('128px')
      expect(drop.style.left).toBe('8px')
      expect(drop.style.maxHeight).toBe('636px')
    })

    it('keeps the right-aligned offset when there is room', async () => {
      anchor.getBoundingClientRect = () => rect(40, 900)
      renderPicker()
      const drop = (await pathInput()).closest('div.fixed') as HTMLElement
      expect(drop.style.left).toBe('580px')
    })

    it('floors the max height at 200px in a short viewport', async () => {
      Object.defineProperty(window, 'innerHeight', { value: 300, configurable: true })
      anchor.getBoundingClientRect = () => rect(100, 200)
      renderPicker()
      const drop = (await pathInput()).closest('div.fixed') as HTMLElement
      expect(drop.style.maxHeight).toBe('200px')
    })
  })

  describe('selecting a directory', () => {
    it('derives the workspace name from the last path segment', async () => {
      await enterCreateView('/home/u/alpha')
      expect(await nameInput()).toHaveValue('alpha')
      expect(screen.getByText('Create Workspace')).toBeInTheDocument()
      expect(screen.getByText('/home/u/alpha')).toBeInTheDocument()
    })

    it('ignores trailing slashes when deriving the name', async () => {
      await enterCreateView('/home/u/alpha//')
      expect(await nameInput()).toHaveValue('alpha')
    })

    it('falls back to the browsed path when the input is empty', async () => {
      renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '   ' } })
      fireEvent.click(screen.getByRole('button', { name: 'Select' }))
      expect(await nameInput()).toHaveValue('u')
      expect(screen.getByText('/home/u')).toBeInTheDocument()
    })

    it('selects on Enter in the path input', async () => {
      renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '  /home/u/beta  ' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(await nameInput()).toHaveValue('beta')
    })

    it('ignores Enter on a blank path input', async () => {
      renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '  ' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(screen.getByRole('button', { name: 'Select' })).toBeInTheDocument()
      expect(screen.queryByLabelText('Workspace name')).not.toBeInTheDocument()
    })

    it('closes on Escape in the path input', async () => {
      const { onOpenChange } = renderPicker()
      const input = await pathInput()
      fireEvent.keyDown(input, { key: 'Escape' })
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
  })

  describe('creating a workspace', () => {
    it('refuses an empty name without calling the API', async () => {
      await enterCreateView()
      fireEvent.change(await nameInput(), { target: { value: '   ' } })
      fireEvent.click(screen.getByRole('button', { name: 'Create' }))
      expect(await screen.findByText('Name required')).toBeInTheDocument()
      expect(api.createWorkspace).not.toHaveBeenCalled()
    })

    it('clears a visible error as soon as the name is edited', async () => {
      await enterCreateView()
      fireEvent.change(await nameInput(), { target: { value: '' } })
      fireEvent.click(screen.getByRole('button', { name: 'Create' }))
      expect(await screen.findByText('Name required')).toBeInTheDocument()
      fireEvent.change(await nameInput(), { target: { value: 'ok' } })
      expect(screen.queryByText('Name required')).not.toBeInTheDocument()
    })

    it('slugifies the name and reports the created workspace', async () => {
      const { onCreated, onOpenChange } = renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/alpha' } })
      fireEvent.click(screen.getByRole('button', { name: 'Select' }))
      fireEvent.change(await nameInput(), { target: { value: ' My Proj!ect ' } })
      fireEvent.click(screen.getByRole('button', { name: 'Create' }))
      await waitFor(() => expect(onCreated).toHaveBeenCalledWith('my-proj-ect'))
      expect(api.createWorkspace).toHaveBeenCalledWith({ name: 'my-proj-ect', dir: '/home/u/alpha' })
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })

    it('disables the button and shows progress while the request is in flight', async () => {
      let settle: ((value: { ok: boolean }) => void) | undefined
      vi.mocked(api.createWorkspace).mockReturnValue(
        new Promise(resolve => { settle = resolve }),
      )
      const { onCreated } = renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/alpha' } })
      fireEvent.click(screen.getByRole('button', { name: 'Select' }))
      fireEvent.click(screen.getByRole('button', { name: 'Create' }))
      const busy = await screen.findByRole('button', { name: 'Creating…' })
      expect(busy).toBeDisabled()
      await act(async () => { settle?.({ ok: true }) })
      await waitFor(() => expect(onCreated).toHaveBeenCalledWith('alpha'))
    })

    it('surfaces a server-reported error and re-enables the button', async () => {
      vi.mocked(api.createWorkspace).mockResolvedValue({ error: 'name already taken' })
      const { onCreated, onOpenChange } = renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/alpha' } })
      fireEvent.click(screen.getByRole('button', { name: 'Select' }))
      fireEvent.click(screen.getByRole('button', { name: 'Create' }))
      expect(await screen.findByText('name already taken', undefined, { timeout: 5_000 })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Create' })).not.toBeDisabled()
      expect(onCreated).not.toHaveBeenCalled()
      expect(onOpenChange).not.toHaveBeenCalled()
    })

    it('surfaces a thrown request failure', async () => {
      vi.mocked(api.createWorkspace).mockRejectedValue(new Error('offline'))
      renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/alpha' } })
      fireEvent.click(screen.getByRole('button', { name: 'Select' }))
      fireEvent.click(screen.getByRole('button', { name: 'Create' }))
      expect(
        await screen.findByText('Failed to create workspace', undefined, { timeout: 5_000 }),
      ).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Create' })).not.toBeDisabled()
    })

    it('creates on Enter in the name input', async () => {
      const { onCreated } = renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/beta' } })
      fireEvent.click(screen.getByRole('button', { name: 'Select' }))
      fireEvent.keyDown(await nameInput(), { key: 'Enter' })
      await waitFor(() => expect(onCreated).toHaveBeenCalledWith('beta'))
    })

    it('returns to the browse view on Escape in the name input', async () => {
      await enterCreateView()
      fireEvent.keyDown(await nameInput(), { key: 'Escape' })
      expect(await pathInput()).toBeInTheDocument()
      expect(screen.queryByText('Create Workspace')).not.toBeInTheDocument()
    })

    it('returns to the browse view via the Back button', async () => {
      await enterCreateView()
      fireEvent.click(screen.getByRole('button', { name: 'Back' }))
      expect(await pathInput()).toBeInTheDocument()
      expect(screen.queryByLabelText('Workspace name')).not.toBeInTheDocument()
    })
  })

  describe('click-outside dismissal', () => {
    it('closes on a mousedown outside the dropdown and the anchor', async () => {
      const { onOpenChange } = renderPicker()
      await pathInput()
      await attachOutsideListener()
      const outside = document.createElement('div')
      document.body.appendChild(outside)
      fireEvent.mouseDown(outside)
      expect(onOpenChange).toHaveBeenCalledWith(false)
      outside.remove()
    })

    it('stays open for a mousedown inside the dropdown', async () => {
      const { onOpenChange } = renderPicker()
      const input = await pathInput()
      await attachOutsideListener()
      fireEvent.mouseDown(input)
      expect(onOpenChange).not.toHaveBeenCalled()
    })

    it('stays open for a mousedown on the anchor itself', async () => {
      const { onOpenChange } = renderPicker()
      await pathInput()
      await attachOutsideListener()
      fireEvent.mouseDown(anchor)
      expect(onOpenChange).not.toHaveBeenCalled()
    })

    it('detaches the listener on unmount', async () => {
      const { onOpenChange, unmount } = renderPicker()
      await pathInput()
      await attachOutsideListener()
      unmount()
      fireEvent.mouseDown(document.body)
      expect(onOpenChange).not.toHaveBeenCalled()
    })

    it('cancels the pending listener timer when unmounted before it fires', async () => {
      const { onOpenChange, unmount } = renderPicker()
      await pathInput()
      unmount()
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      fireEvent.mouseDown(document.body)
      expect(onOpenChange).not.toHaveBeenCalled()
    })
  })
})
