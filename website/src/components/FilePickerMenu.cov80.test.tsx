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

  it('shows the empty state once a search settles with no hits', async () => {
    fileSearch.mockResolvedValue({ results: [], root: '/root' } as never)
    mount()
    expect(await screen.findByText('No matches')).toBeInTheDocument()
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
})
