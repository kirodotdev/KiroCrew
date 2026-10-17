import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/* ── `./` path completion in ChatInput: the composer's shell-style trigger.
 *    api is mocked so the mounted picker's /api/path-complete fetch is
 *    deterministic; `project` is what the endpoint resolves the token against,
 *    so every test that expects a menu passes one. ── */
const mockApi = vi.hoisted(() => ({
  pathComplete: vi.fn(),
  fileSearch: vi.fn(),
  skills: vi.fn(),
  skillTrust: vi.fn(),
  grantSkillTrust: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ChatInput from '../components/ChatInput'

const ROOT = '/work/proj'
const ROWS = [
  { path: `${ROOT}/src`, name: 'src', kind: 'dir' as const, size: 0, mtime: 1750000000 },
  { path: `${ROOT}/readme.md`, name: 'readme.md', kind: 'file' as const, size: 12, mtime: 1750000000 },
]

beforeEach(() => {
  vi.restoreAllMocks()
  // vitest 4's restoreAllMocks no longer clears standalone vi.fn() call history,
  // so clear it explicitly or calls leak across tests.
  vi.clearAllMocks()
  localStorage.clear()
  mockApi.pathComplete.mockResolvedValue({ results: ROWS, root: ROOT })
  // One level per prefix, so a request for `./` and a request for `./src/` can
  // be told apart — what the stale-window test below turns on.
  mockApi.pathComplete.mockImplementation(async (_project: string, dir: string) =>
    dir === './src/'
      ? { results: [{ path: `${ROOT}/src/app.ts`, name: 'app.ts', kind: 'file' as const, size: 4, mtime: 1750000000 }], root: ROOT }
      : { results: ROWS, root: ROOT },
  )
  mockApi.fileSearch.mockResolvedValue({ results: [] })
  mockApi.skills.mockResolvedValue([])
})

/** Host that keeps the draft, so an inserted completion is visible to the next
 *  keystroke exactly as it is in the real composer. */
function Host({ onChange }: { onChange?: (v: string) => void }) {
  const [val, setVal] = useState('')
  return (
    <ChatInput
      value={val}
      onChange={(v) => { onChange?.(v); setVal(v) }}
      onSend={vi.fn()}
      project={ROOT}
    />
  )
}

function typeInto(value: string) {
  const ta = screen.getByLabelText('Message input')
  fireEvent.change(ta, { target: { value } })
  return ta
}

describe('ChatInput — ./ path completion', () => {
  it('opens the picker on a bare ./ and asks the endpoint for that directory', async () => {
    renderWithProviders(<Host />)
    typeInto('read ./')
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
    await waitFor(() => expect(mockApi.pathComplete).toHaveBeenCalledWith(ROOT, './', '', expect.anything()))
    // The directory row renders with the trailing slash the accepted token gets.
    expect(await screen.findByText('src/')).toBeInTheDocument()
  })

  it('narrows as the partial name is typed', async () => {
    renderWithProviders(<Host />)
    typeInto('read ./read')
    await screen.findByRole('listbox')
    await waitFor(() => expect(mockApi.pathComplete).toHaveBeenCalledWith(ROOT, './', 'read', expect.anything()))
  })

  it('scopes to the sub-directory named by the token', async () => {
    renderWithProviders(<Host />)
    typeInto('read ../../src/comp')
    await screen.findByRole('listbox')
    await waitFor(() => expect(mockApi.pathComplete).toHaveBeenCalledWith(ROOT, '../../src/', 'comp', expect.anything()))
  })

  it('does not open without a project dir to resolve the token against', async () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    typeInto('read ./')
    await waitFor(() => expect(mockApi.pathComplete).not.toHaveBeenCalled())
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('does not open for an abbreviation or a sentence-final full stop', async () => {
    renderWithProviders(<Host />)
    typeInto('e.g. done.')
    await waitFor(() => expect(mockApi.pathComplete).not.toHaveBeenCalled())
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('inserts a plain relative path for a file, not an @-mention chip', async () => {
    const onChange = vi.fn()
    renderWithProviders(<Host onChange={onChange} />)
    typeInto('read ./rea')
    fireEvent.mouseDown(await screen.findByText('readme.md'))
    expect(onChange).toHaveBeenLastCalledWith('read ./readme.md ')
  })

  it('Tab accepts the highlighted row', async () => {
    const onChange = vi.fn()
    renderWithProviders(<Host onChange={onChange} />)
    const ta = typeInto('read ./')
    // The listbox mounts with its empty state, so wait for the ROW: the hook
    // releases Enter/Tab while the list is still empty.
    await screen.findByText('src/')
    fireEvent.keyDown(ta, { key: 'Tab' })
    // First row is the directory, so the accepted token keeps its trailing slash.
    expect(onChange).toHaveBeenLastCalledWith('read ./src/')
  })

  it('Enter accepts the highlighted row instead of sending', async () => {
    const onChange = vi.fn()
    const onSend = vi.fn()
    function EnterHost() {
      const [val, setVal] = useState('')
      return (
        <ChatInput
          value={val}
          onChange={(v) => { onChange(v); setVal(v) }}
          onSend={onSend}
          project={ROOT}
        />
      )
    }
    renderWithProviders(<EnterHost />)
    const ta = typeInto('read ./')
    await screen.findByText('src/')
    fireEvent.keyDown(ta, { key: 'Enter' })
    expect(onChange).toHaveBeenLastCalledWith('read ./src/')
    expect(onSend).not.toHaveBeenCalled()
  })

  it('accepting a directory keeps the menu open on the new level', async () => {
    renderWithProviders(<Host />)
    typeInto('read ./')
    fireEvent.mouseDown(await screen.findByText('src/'))
    await waitFor(() => expect(mockApi.pathComplete).toHaveBeenCalledWith(ROOT, './src/', '', expect.anything()))
    expect(screen.getByRole('listbox')).toBeInTheDocument()
  })

  it('accepting inside the debounce window uses the prefix the rows came from', async () => {
    // Regression: the accepted token was rebuilt on the LIVE prefix while the
    // rows still came from the previous one, so accepting in the ~200ms after a
    // `/` was typed inserted a doubled segment (`./src/src/`).
    const onChange = vi.fn()
    renderWithProviders(<Host onChange={onChange} />)
    const ta = typeInto('read ./sr')
    await screen.findByText('src/')
    // No await: the rows on screen still belong to `./`.
    fireEvent.change(ta, { target: { value: 'read ./src/' } })
    fireEvent.keyDown(ta, { key: 'Tab' })
    expect(onChange).toHaveBeenLastCalledWith('read ./src/')
  })

  it('does not ask for a directory before the token is one', async () => {
    renderWithProviders(<Host />)
    typeInto('read ./')
    await screen.findByText('src/')
    // Every request names a real directory prefix: the tick where the debounced
    // token is still '' must not be sent as a listing of the project root.
    for (const call of mockApi.pathComplete.mock.calls) expect(call[1]).not.toBe('')
  })

  it('does not pair stale rows with a deeper prefix while the next level is in flight', async () => {
    // Regression: React Query's `placeholderData` keeps the PREVIOUS payload on
    // screen across a query-key change, so a prefix read from the debounced token
    // (which has already moved) paired the `./` rows with `./src/` → `./src/src/`.
    const onChange = vi.fn()
    mockApi.pathComplete.mockImplementation(async (_project: string, dir: string) =>
      dir === './src/'
        // Never settles: the window where the key has moved but the rows have not.
        ? new Promise(() => {})
        : { results: ROWS, root: ROOT },
    )
    renderWithProviders(<Host onChange={onChange} />)
    const ta = typeInto('read ./sr')
    await screen.findByText('src/')
    fireEvent.change(ta, { target: { value: 'read ./src/' } })
    await waitFor(() => expect(mockApi.pathComplete).toHaveBeenCalledWith(ROOT, './src/', '', expect.anything()))
    // The stale rows are still on screen, and still choosable.
    expect(screen.getByText('src/')).toBeInTheDocument()
    fireEvent.keyDown(ta, { key: 'Tab' })
    expect(onChange).toHaveBeenLastCalledWith('read ./src/')
  })

  it('says the token left the project instead of claiming no matches', async () => {
    // The verdict is the endpoint's, carried in the payload beside the rows.
    mockApi.pathComplete.mockResolvedValue({ results: [], root: '', outside: true })
    renderWithProviders(<Host />)
    typeInto('read ../')
    expect(await screen.findByText(/ends up outside the project/)).toBeInTheDocument()
    expect(screen.queryByText(/No matching files/)).not.toBeInTheDocument()
  })

  it('still says no matches for an empty directory inside the project', async () => {
    mockApi.pathComplete.mockResolvedValue({ results: [], root: ROOT })
    renderWithProviders(<Host />)
    typeInto('read ./')
    expect(await screen.findByText(/No matching files/)).toBeInTheDocument()
  })

  it('Escape dismisses the picker', async () => {
    renderWithProviders(<Host />)
    const ta = typeInto('read ./')
    await screen.findByRole('listbox')
    fireEvent.keyDown(ta, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('leaves ~/ to the shell — home is not a completion root', async () => {
    renderWithProviders(<Host />)
    typeInto('read ~/')
    await waitFor(() => expect(mockApi.pathComplete).not.toHaveBeenCalled())
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('an @ mention still goes to the file search, not path completion', async () => {
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} project={ROOT} onFileSelect={vi.fn()} />,
    )
    typeInto('see @src')
    await waitFor(() => expect(mockApi.fileSearch).toHaveBeenCalled())
    expect(mockApi.pathComplete).not.toHaveBeenCalled()
  })
})
