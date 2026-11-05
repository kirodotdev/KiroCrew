// #26892: Add Source took files and folders behind a File / Folder switch, and
// printed the supported-format list twice. These pin one view for both, and
// one format list.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import { api as clientApi } from '../api/client'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))
vi.mock('../api/client', () => ({ api: { dashboardConfig: vi.fn() } }))

const FORMATS = '.md, .pdf, .zip'

function renderList() {
  const onIngest = vi.fn()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <SourcesList
        onIngest={onIngest}
        uploadNamespace="default"
        setUploadNamespace={() => {}}
        namespaces={[]}
        ingestionJobs={[]}
        supportedFormatsDisplay={FORMATS}
        acceptsNoExtension
      />
    </QueryClientProvider>,
  )
  return { onIngest }
}

async function openAdd() {
  const utils = renderList()
  fireEvent.click(await screen.findByText('+ Add Source'))
  return utils
}

const dropZone = () =>
  screen.getByText('Drop files or a folder here, or click to choose files').closest('[role="button"]') as HTMLElement

/** A drop the way Chromium delivers it: one item per dropped thing, each
 *  saying through its entry whether it is a folder. */
function dataTransfer(entries: { file: File; dir?: boolean }[]) {
  return {
    files: entries.map(e => e.file),
    items: entries.map(e => ({
      kind: 'file',
      getAsFile: () => e.file,
      webkitGetAsEntry: () => ({ isDirectory: !!e.dir, name: e.file.name }),
    })),
  }
}

const folderItem = (name: string) => ({ file: new File([''], name), dir: true })

beforeEach(() => {
  vi.mocked(api.knowledgeApi).mockReset()
  vi.mocked(api.knowledgeApi).mockImplementation(((path: string, init?: RequestInit) => {
    if (path === '/config') return Promise.resolve({ enabled: true, supported_formats: [], folder_picker: false })
    if (path === '/sources' && init?.method === 'POST') return Promise.resolve({ id: 's1', status: 'pending_confirmation', file_count: 3 })
    if (path === '/sources') return Promise.resolve([])
    return Promise.resolve({ ok: true })
  }) as never)
  vi.mocked(clientApi.dashboardConfig).mockReset()
  vi.mocked(clientApi.dashboardConfig).mockResolvedValue({ knowledge_upload_max_mb: 100 })
})

afterEach(() => {
  Reflect.deleteProperty(window, 'kirocrew')
  Reflect.deleteProperty(window, 'folderPickAPI')
})

/** The desktop shell's bridges: dropped-file paths, and whether this window's
 *  gateway runs on this machine. */
function desktopShell(pathFor: (f: File) => string, sameHost: boolean) {
  Object.assign(window, {
    kirocrew: { getPathForFile: pathFor },
    folderPickAPI: { available: () => Promise.resolve(sameHost), pick: () => Promise.resolve(null) },
  })
}

describe('Add Source — one format list (#26892)', () => {
  it('shows the supported formats once, next to the size limit and the no-extension note', async () => {
    await openAdd()
    await screen.findByText(/Max 100 MB per file\./)
    const withFormats = screen.getAllByText((_, el) => !!el && el.children.length === 0 && (el.textContent ?? '').includes(FORMATS))
    expect(withFormats).toHaveLength(1)
    expect(withFormats[0].textContent).toMatch(/Max 100 MB per file\./)
    expect(withFormats[0].textContent).toMatch(/no extension/)
  })
})

describe('Add Source — files and folders without a switch (#26892)', () => {
  it('Choose folder opens the native picker at once when the gateway can show one', async () => {
    vi.mocked(api.knowledgeApi).mockImplementation(((path: string) => {
      if (path === '/config') return Promise.resolve({ enabled: true, supported_formats: [], folder_picker: true })
      if (path === '/pick-folder') return Promise.resolve({ path: '/home/u/picked' })
      if (path === '/sources') return Promise.resolve([])
      return Promise.resolve({ ok: true })
    }) as never)
    await openAdd()
    fireEvent.click(await screen.findByRole('button', { name: /Choose folder/ }))
    await waitFor(() => expect(screen.getByLabelText('Folder path')).toHaveProperty('value', '/home/u/picked'))
  })

  it('offers Choose files and a folder button straight away, with no File / Folder switch', async () => {
    await openAdd()
    expect(screen.getByRole('button', { name: /Choose files/ })).toBeTruthy()
    expect(await screen.findByRole('button', { name: /Add folder by path/ })).toBeTruthy()
    expect(screen.queryByText('Local File')).toBeNull()
    expect(screen.queryByText('Local Folder')).toBeNull()
    expect(dropZone()).toBeTruthy()
  })

  it('Choose files opens the file picker', async () => {
    await openAdd()
    const input = screen.getByLabelText('Upload files') as HTMLInputElement
    const click = vi.spyOn(input, 'click').mockImplementation(() => {})
    fireEvent.click(screen.getByRole('button', { name: /Choose files/ }))
    expect(click).toHaveBeenCalledTimes(1)
  })

  it('ingests one dropped file', async () => {
    const { onIngest } = await openAdd()
    const f = new File(['# a'], 'a.md')
    fireEvent.drop(dropZone(), { dataTransfer: dataTransfer([{ file: f }]) })
    expect(onIngest).toHaveBeenCalledWith([f])
  })

  it('ingests several dropped files in one go', async () => {
    const { onIngest } = await openAdd()
    const files = [new File(['a'], 'a.md'), new File(['b'], 'b.pdf'), new File(['c'], 'c.txt')]
    fireEvent.drop(dropZone(), { dataTransfer: dataTransfer(files.map(file => ({ file }))) })
    expect(onIngest).toHaveBeenCalledTimes(1)
    expect(onIngest.mock.calls[0][0]).toEqual(files)
  })

  it('passes an extensionless file such as README on to ingestion', async () => {
    const { onIngest } = await openAdd()
    const readme = new File(['hello'], 'README')
    fireEvent.drop(dropZone(), { dataTransfer: dataTransfer([{ file: readme }]) })
    expect(onIngest).toHaveBeenCalledWith([readme])
  })

  it('Add folder by path opens the watched-folder form, and adding it still sends a local_folder source', async () => {
    await openAdd()
    expect(screen.queryByLabelText('Folder path')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Add folder by path/ }))
    fireEvent.change(screen.getByLabelText('Folder path'), { target: { value: '/home/u/notes' } })
    fireEvent.change(screen.getByLabelText('Ignore patterns'), { target: { value: '.trash/\n' } })
    fireEvent.click(screen.getByText('Add Folder'))
    await waitFor(() => {
      const post = vi.mocked(api.knowledgeApi).mock.calls.find(([p, i]) => p === '/sources' && i?.method === 'POST')
      expect(post).toBeTruthy()
      const body = JSON.parse(String(post![1]!.body))
      expect(body).toMatchObject({ source_type: 'local_folder', uri: '/home/u/notes', properties: { recursive: true, ignore_patterns: ['.trash/'] } })
    })
    // The large-scan confirmation still follows a folder add.
    expect(await screen.findByText(/3 supported files found/)).toBeTruthy()
  })

  it('shows a failed folder picker through the shared error notice', async () => {
    vi.mocked(api.knowledgeApi).mockImplementation(((path: string) => {
      if (path === '/config') return Promise.resolve({ enabled: true, supported_formats: [], folder_picker: true })
      if (path === '/pick-folder') return Promise.reject(new Error('Picker unavailable'))
      if (path === '/sources') return Promise.resolve([])
      return Promise.resolve({ ok: true })
    }) as never)
    await openAdd()
    fireEvent.click(await screen.findByRole('button', { name: /Choose folder/ }))
    expect(await screen.findByTestId('sources-folder-picker-error')).toHaveTextContent('Picker unavailable')
    expect(screen.getByLabelText('Folder path')).toHaveProperty('value', '')
  })

  it('a folder dropped in the desktop app fills its path into the folder form', async () => {
    desktopShell(() => '/home/u/notes', true)
    const { onIngest } = await openAdd()
    fireEvent.drop(dropZone(), { dataTransfer: dataTransfer([folderItem('notes')]) })
    expect(onIngest).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByLabelText('Folder path')).toHaveProperty('value', '/home/u/notes'))
    expect(screen.getByLabelText('Source name (optional)')).toHaveProperty('value', 'notes')
    expect(screen.queryByText(/can't be read from a drop/)).toBeNull()
  })

  it('a same-host answer that arrives after a drop never overwrites the path field', async () => {
    let answer: (v: boolean) => void = () => {}
    Object.assign(window, {
      kirocrew: { getPathForFile: () => '/home/u/notes' },
      folderPickAPI: { available: () => new Promise<boolean>(res => { answer = res }), pick: () => Promise.resolve(null) },
    })
    await openAdd()
    fireEvent.drop(dropZone(), { dataTransfer: dataTransfer([folderItem('notes')]) })
    fireEvent.change(screen.getByLabelText('Folder path'), { target: { value: '/home/u/typed' } })
    answer(true)
    await new Promise(r => setTimeout(r, 0))
    expect(screen.getByLabelText('Folder path')).toHaveProperty('value', '/home/u/typed')
  })

  it('a folder dropped in a desktop window on a remote gateway does not send the local path', async () => {
    desktopShell(() => '/home/u/notes', false)
    await openAdd()
    fireEvent.drop(dropZone(), { dataTransfer: dataTransfer([folderItem('notes')]) })
    expect(await screen.findByText(/can't be read from a drop/)).toBeTruthy()
    await new Promise(r => setTimeout(r, 0))
    expect(screen.getByLabelText('Folder path')).toHaveProperty('value', '')
  })

  it('a browser folder drop clears a path typed for an earlier folder', async () => {
    await openAdd()
    fireEvent.click(screen.getByRole('button', { name: /Add folder by path/ }))
    fireEvent.change(screen.getByLabelText('Folder path'), { target: { value: '/home/u/old' } })
    fireEvent.click(screen.getByText('Cancel'))
    fireEvent.click(screen.getByText('+ Add Source'))
    fireEvent.drop(dropZone(), { dataTransfer: dataTransfer([folderItem('notes')]) })
    expect(screen.getByLabelText('Folder path')).toHaveProperty('value', '')
    expect(screen.getByText('Add Folder').closest('button')).toBeDisabled()
  })

  it('shows a failed folder add through the shared error notice', async () => {
    vi.mocked(api.knowledgeApi).mockImplementation(((path: string, init?: RequestInit) => {
      if (path === '/sources' && init?.method === 'POST') return Promise.reject(new Error('Folder not found'))
      if (path === '/sources') return Promise.resolve([])
      return Promise.resolve({ enabled: true, supported_formats: [], folder_picker: false })
    }) as never)
    await openAdd()
    fireEvent.click(screen.getByRole('button', { name: /Add folder by path/ }))
    fireEvent.change(screen.getByLabelText('Folder path'), { target: { value: '/nope' } })
    fireEvent.click(screen.getByText('Add Folder'))
    expect(await screen.findByTestId('sources-folder-error')).toHaveTextContent('Folder not found')
  })

  it('a folder dropped in a browser opens the form and asks for its path instead of uploading it', async () => {
    const { onIngest } = await openAdd()
    fireEvent.drop(dropZone(), { dataTransfer: dataTransfer([folderItem('notes')]) })
    expect(onIngest).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Folder path')).toHaveProperty('value', '')
    expect(screen.getByText(/can't be read from a drop/)).toBeTruthy()
  })

  it('a mixed drop uploads the files and opens the folder form for the folder', async () => {
    desktopShell((f: File) => (f.name === 'notes' ? '/home/u/notes' : ''), true)
    const { onIngest } = await openAdd()
    const f = new File(['a'], 'a.md')
    fireEvent.drop(dropZone(), { dataTransfer: dataTransfer([{ file: f }, folderItem('notes')]) })
    expect(onIngest).toHaveBeenCalledWith([f])
    await waitFor(() => expect(screen.getByLabelText('Folder path')).toHaveProperty('value', '/home/u/notes'))
  })
})
