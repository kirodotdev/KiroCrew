import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { renderWithProviders } from './helpers'
import WorkspacePicker from '../components/WorkspacePicker'
import { api } from '../api/client'

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
      await waitFor(async () => expect(await pathInput()).toHaveValue('/home/u'))
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

    it('keeps the previous listing while a drill is pending, not an emptiness claim', async () => {
      // "No subdirectories" asserts a folder IS empty, so it must not stand in for a
      // listing still in flight: on a wedged gateway it would hold for the whole deadline.
      vi.mocked(api.browseDirs)
        .mockResolvedValueOnce(browseResult())
        .mockImplementationOnce(() => new Promise(() => {}))
      renderPicker()
      fireEvent.click(await screen.findByText('beta'))
      await waitFor(() => expect(api.browseDirs).toHaveBeenCalledTimes(2))
      expect(screen.queryByText('No subdirectories')).toBeNull()
      expect(screen.getByText('beta')).toBeInTheDocument()
    })

    it('shows the empty state when a directory has no children', async () => {
      vi.mocked(api.browseDirs).mockResolvedValue(browseResult('/home/u', '/home', []))
      renderPicker()
      expect(await screen.findByText('No subdirectories')).toBeInTheDocument()
    })

  })

  describe('typed filtering', () => {
    it('narrows the list to entries matching the typed segment', async () => {
      renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: '/home/u/al' } })
      expect(screen.getByText('alpha')).toBeInTheDocument()
      expect(screen.queryByText('beta')).not.toBeInTheDocument()
    })

    it('falls back to the empty state when nothing matches', async () => {
      renderPicker()
      const input = await pathInput()
      fireEvent.change(input, { target: { value: 'zzz' } })
      expect(screen.getByText('No subdirectories')).toBeInTheDocument()
    })

    it('keeps the full list when the input still equals the browsed path', async () => {
      renderPicker()
      const input = await pathInput()
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
