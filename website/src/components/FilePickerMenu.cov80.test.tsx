import { useRef } from 'react'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import FilePickerMenu, { makeRelative, resultKind, selectionFor } from './FilePickerMenu'
import { api } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, fileSearch: vi.fn() } }
})

const fileSearch = vi.mocked(api.fileSearch)
const NOW = 1_700_000_000

function result(over: Record<string, unknown> = {}) {
  return { path: '/root/zzq/a.ts', name: 'a.ts', size: 2048, mtime: NOW - 60, ...over }
}

/** The picker positions against a live anchor, so give it a real element. */
function Host(props: Omit<React.ComponentProps<typeof FilePickerMenu>, 'anchorRef'>) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <>
      <div ref={ref} data-testid="zzq-anchor" />
      <FilePickerMenu {...props} anchorRef={ref} />
    </>
  )
}

function mount(props: Partial<React.ComponentProps<typeof FilePickerMenu>> = {}) {
  const onSelect = vi.fn()
  const onClose = vi.fn()
  const view = renderWithProviders(
    <Host query="zz" open onSelect={onSelect} onClose={onClose} {...props} />,
  )
  return { onSelect, onClose, ...view }
}

describe('FilePickerMenu helpers', () => {
  it('makeRelative strips a posix root, with or without a trailing slash', () => {
    expect(makeRelative('/root/a.ts', '/root')).toBe('a.ts')
    expect(makeRelative('/root/a.ts', '/root/')).toBe('a.ts')
  })

  it('makeRelative strips a windows root and leaves a non-match alone', () => {
    expect(makeRelative('C:\\proj\\a.ts', 'C:\\proj')).toBe('a.ts')
    expect(makeRelative('/other/a.ts', '/root')).toBe('/other/a.ts')
    expect(makeRelative('/root/a.ts', '')).toBe('/root/a.ts')
  })

  it('resultKind treats an absent kind as a file', () => {
    expect(resultKind({})).toBe('file')
    expect(resultKind({ kind: 'dir' })).toBe('dir')
  })

  it('selectionFor gives directories exactly one trailing slash', () => {
    expect(selectionFor(result({ kind: 'dir' }) as never, '/root')).toEqual({
      path: '/root/zzq/a.ts',
      relativePath: 'zzq/a.ts/',
      kind: 'dir',
    })
    expect(
      selectionFor(result({ kind: 'dir', path: '/root/zzq/' }) as never, '/root').relativePath,
    ).toBe('zzq/')
    expect(selectionFor(result() as never, '/root').relativePath).toBe('zzq/a.ts')
  })
})

describe('FilePickerMenu', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    fileSearch.mockReset()
    fileSearch.mockResolvedValue({ results: [result()], root: '/root' } as never)
  })
  afterEach(() => vi.useRealTimers())

  it('renders nothing while closed', () => {
    const { container } = mount({ open: false })
    expect(container.querySelector('[role="listbox"]')).toBeNull()
  })

  it('prompts for more characters below the 2-char threshold and never searches', async () => {
    // The picker measures a live anchor, so it renders null on the very first
    // pass (the ref is not attached yet) and needs one more render to appear.
    const { rerender } = mount({ query: 'z' })
    rerender(<Host query="z" open onSelect={vi.fn()} onClose={vi.fn()} />)

    expect(
      await screen.findByText(/Type 2\+ chars to search files and folders/),
    ).toBeInTheDocument()
    await waitFor(() => expect(fileSearch).not.toHaveBeenCalled())
  })

  it('passes the query and project through to the search, with an abort signal', async () => {
    mount({ query: 'zz', project: 'zzq-proj' })
    await waitFor(() =>
      expect(fileSearch).toHaveBeenCalledWith('zz', 'zzq-proj', expect.anything()))
  })

  it('debounces a CHANGED query by 200ms before re-searching', async () => {
    const { rerender } = mount({ query: 'zz' })
    await waitFor(() => expect(fileSearch).toHaveBeenCalledTimes(1))

    rerender(
      <Host query="zzq" open onSelect={vi.fn()} onClose={vi.fn()} />,
    )
    expect(fileSearch).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(200)
    await waitFor(() => expect(fileSearch).toHaveBeenCalledTimes(2))
    expect(fileSearch.mock.calls[1][0]).toBe('zzq')
  })

  it('shows the empty state once a search settles with no hits, announcing that Enter sends', async () => {
    fileSearch.mockResolvedValue({ results: [], root: '/root' } as never)
    mount()
    expect(await screen.findByText(/No matching files/)).toBeInTheDocument()
  })

  it('renders a file row with size and relative age', async () => {
    mount()
    expect(await screen.findByText('a.ts')).toBeInTheDocument()
    const row = screen.getByRole('option')
    expect(row.getAttribute('data-kind')).toBe('file')
    expect(row.textContent).toContain('2')
  })

  it('renders a directory row with a trailing slash and no size', async () => {
    fileSearch.mockResolvedValue({
      results: [result({ kind: 'dir', name: 'zzq-dir', path: '/root/zzq-dir' })],
      root: '/root',
    } as never)
    mount()
    expect(await screen.findByText('zzq-dir/')).toBeInTheDocument()
    expect(screen.getByRole('option').getAttribute('data-kind')).toBe('dir')
    expect(screen.getByLabelText('Folder')).toBeInTheDocument()
  })

  it('formats an old mtime as a calendar date instead of an elapsed age', async () => {
    const old = Math.floor(Date.now() / 1000) - 86400 * 400
    fileSearch.mockResolvedValue({ results: [result({ mtime: old })], root: '/root' } as never)
    mount()
    await screen.findByText('a.ts')
    // A relative rendering would say "ago"; a calendar one never does.
    expect(screen.getByRole('option').textContent).not.toMatch(/ago/)
  })

  it('mousedown on a row selects it with the relative path', async () => {
    const { onSelect } = mount()
    fireEvent.mouseDown(await screen.findByRole('option'))
    expect(onSelect).toHaveBeenCalledWith({
      path: '/root/zzq/a.ts',
      relativePath: 'zzq/a.ts',
      kind: 'file',
    })
  })

  it('hovering a row moves the highlight', async () => {
    fileSearch.mockResolvedValue({
      results: [result(), result({ path: '/root/zzq/b.ts', name: 'b.ts' })],
      root: '/root',
    } as never)
    mount()
    const rows = await screen.findAllByRole('option')
    fireEvent.mouseEnter(rows[1])
    await waitFor(() => expect(rows[1].getAttribute('aria-selected')).toBe('true'))
    expect(rows[0].getAttribute('aria-selected')).toBe('false')
  })

  it('Enter inserts the @-mention for the highlighted row', async () => {
    const { onSelect } = mount()
    await screen.findByRole('option')
    fireEvent.keyDown(document, { key: 'Enter' })
    await waitFor(() => expect(onSelect).toHaveBeenCalledTimes(1))
    expect(onSelect.mock.calls[0][0].relativePath).toBe('zzq/a.ts')
  })

  it('the eye button opens the viewer and closes the picker without inserting', async () => {
    const onFileOpen = vi.fn()
    const { onSelect, onClose } = mount({ onFileOpen })
    fireEvent.mouseDown(await screen.findByLabelText('Open in viewer'))
    expect(onFileOpen).toHaveBeenCalledWith('/root/zzq/a.ts')
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('a directory offers no viewer button', async () => {
    fileSearch.mockResolvedValue({
      results: [result({ kind: 'dir', name: 'zzq-dir' })],
      root: '/root',
    } as never)
    mount({ onFileOpen: vi.fn() })
    await screen.findByText('zzq-dir/')
    expect(screen.queryByLabelText('Open in viewer')).not.toBeInTheDocument()
  })

  it('Alt+Enter previews a file, and falls through to insert for a directory', async () => {
    const onFileOpen = vi.fn()
    const { onSelect, onClose, unmount } = mount({ onFileOpen })
    await screen.findByRole('option')
    fireEvent.keyDown(document, { key: 'Enter', altKey: true })
    await waitFor(() => expect(onFileOpen).toHaveBeenCalledWith('/root/zzq/a.ts'))
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
    unmount()

    fileSearch.mockResolvedValue({
      results: [result({ kind: 'dir', name: 'zzq-dir', path: '/root/zzq-dir' })],
      root: '/root',
    } as never)
    const second = mount({ onFileOpen })
    await screen.findByText('zzq-dir/')
    fireEvent.keyDown(document, { key: 'Enter' })
    await waitFor(() => expect(second.onSelect).toHaveBeenCalledTimes(1))
    expect(second.onSelect.mock.calls[0][0].kind).toBe('dir')
  })

  it('Escape closes the picker', async () => {
    const { onClose } = mount()
    await screen.findByRole('option')
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  // Regression for #5029: a prompt mention (e.g. "@agent-sop:name") matches no
  // file, and the empty menu used to swallow Enter — the message could not be
  // sent until a trailing space closed the menu.
  it('with zero matches, Enter passes through un-prevented and closes the menu', async () => {
    fileSearch.mockResolvedValue({ results: [], root: '/root' } as never)
    const { onSelect, onClose } = mount()
    // The settled-empty state announces the mode flip (UX: Enter now sends).
    await screen.findByText(/No matching files/)
    // fireEvent returns false when preventDefault was called; the composer's
    // own Enter-to-send only fires when the keystroke is NOT prevented.
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true)
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('with zero matches, Tab passes through un-prevented and closes the menu', async () => {
    fileSearch.mockResolvedValue({ results: [], root: '/root' } as never)
    const { onSelect, onClose } = mount()
    await screen.findByText(/No matching files/)
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(true)
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('with matches, Enter is still consumed by the menu (not released)', async () => {
    const { onSelect } = mount()
    await screen.findByRole('option')
    // The inverse of the zero-match release: a populated menu keeps its claim.
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    await waitFor(() => expect(onSelect).toHaveBeenCalledTimes(1))
  })

  it('while the search is still in flight, Enter stays swallowed (no premature send)', async () => {
    // A never-settling fetch models the debounce/in-flight window: results are
    // transiently [], but releasing Enter here would send a draft whose
    // mention the user was still completing.
    fileSearch.mockImplementation(() => new Promise(() => {}))
    const onSelect = vi.fn()
    const onClose = vi.fn()
    const { rerender } = mount({ onSelect, onClose })
    rerender(<Host query="zz" open onSelect={onSelect} onClose={onClose} />)
    expect(await screen.findByText(/Searching/)).toBeInTheDocument()
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('below the 2-char threshold, Enter stays swallowed', async () => {
    const { onClose, rerender } = mount({ query: 'z' })
    rerender(<Host query="z" open onSelect={vi.fn()} onClose={onClose} />)
    await screen.findByText(/Type 2\+ chars to search files and folders/)
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('after a settled-empty search, editing the query re-swallows Enter until the debounce settles', async () => {
    // Settled empty for "zz", then the user broadens the mention: during the
    // 200ms debounce lag the stale empty result set must NOT release Enter --
    // the new query may well have matches.
    fileSearch.mockResolvedValue({ results: [], root: '/root' } as never)
    const onSelect = vi.fn()
    const onClose = vi.fn()
    const { rerender } = mount({ onSelect, onClose })
    await screen.findByText(/No matching files/)

    rerender(<Host query="zq" open onSelect={onSelect} onClose={onClose} />)
    // debounced still holds "zz" here -- the stale window under test. The copy
    // drops back to the plain empty state while the release gate is off.
    await screen.findByText('No matches')
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    expect(onClose).not.toHaveBeenCalled()

    // Once the debounce settles (and the fetch resolves empty), Enter releases.
    await vi.advanceTimersByTimeAsync(250)
    await screen.findByText(/No matching files/)
    await waitFor(() => expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true))
    expect(onClose).toHaveBeenCalled()
  })

  it('after the search settles in an ERROR, Enter is released too (trap must not survive the error path)', async () => {
    // A failed search renders its own ErrorNotice, never the "No matches" copy:
    // saying the file is absent when the request never completed is a lie.
    fileSearch.mockRejectedValue(new Error('boom'))
    const { onSelect, onClose } = mount()
    expect(await screen.findByTestId('file-picker-search-error')).toHaveAttribute('role', 'alert')
    expect(screen.getByText(/File search failed/)).toBeInTheDocument()
    expect(screen.queryByText(/No matching files/)).not.toBeInTheDocument()
    await waitFor(() => expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true))
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('in ctrl-enter send mode, the settled-empty copy names Ctrl+Enter (bare Enter is a newline there)', async () => {
    fileSearch.mockResolvedValue({ results: [], root: '/root' } as never)
    mount({ sendOnEnter: 'ctrl-enter' })
    expect(await screen.findByText(/Ctrl\+Enter sends the message/)).toBeInTheDocument()
    expect(screen.queryByText(/— Enter sends the message/)).not.toBeInTheDocument()
  })
})
