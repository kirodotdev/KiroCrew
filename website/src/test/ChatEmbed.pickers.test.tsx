/**
 * ChatEmbed's `/` command and `@` file pickers: the dashboard composer's own
 * picker atom, reading through the app's scoped client, and offered only when
 * the host app declared the endpoint behind each one.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, fireEvent, waitFor, within } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()
const mockRaw = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost, raw: mockRaw }),
  useAppInfo: () => ({ name: 'host', version: '0', permissions: { api: [], events: [] } }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: () => <div data-testid="chat-message-list" />,
}))

// The dashboard client must never serve the embed's pickers: every read goes
// through the app's scoped client above.
const dashboardApi = vi.hoisted(() => ({
  slashCommands: vi.fn(),
  fileSearch: vi.fn(),
  pathComplete: vi.fn(),
  skills: vi.fn(),
}))
vi.mock('../api/client', async (orig) => ({ ...(await orig<typeof import('../api/client')>()), api: dashboardApi }))

import ChatEmbed from '../app-sdk/ChatEmbed'
import { AppSdkContext, type AppSdkContextValue } from '../app-sdk/scopedApi'
import { EMBED_FILE_SEARCH_PATH, EMBED_SLASH_COMMANDS_PATH, embedPickerSource } from '../app-sdk/embedPickerSource'
import { offers } from '../chat-core/composer/pickerSource'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'
import { stripPickedToken } from '../app-sdk/useEmbedPickers'

let queryClient: QueryClient

function mount(api: string[]) {
  const ctx = {
    api: { get: mockGet, post: mockPost, raw: mockRaw },
    info: { name: 'host', version: '0', permissions: { api, events: [] } },
    subscribe: () => () => {},
    navigate: () => {},
    notify: () => {},
  } as unknown as AppSdkContextValue
  return render(
    <QueryClientProvider client={queryClient}>
      <AppSdkContext.Provider value={ctx}>
        <ChatEmbed slotKey="s1" />
      </AppSdkContext.Provider>
    </QueryClientProvider>,
  )
}

const COMMANDS = [
  { name: '/compact', description: 'Compact conversation to free context' },
  { name: '/side', description: 'Open a side chat' },
]
const ROOT = '/work/proj'
const FILES = { root: ROOT, results: [{ path: `${ROOT}/src/app.ts`, name: 'app.ts', size: 10, mtime: 1750000000, kind: 'file' }] }

const GRANTED = ['/api/chat', '/api/chat/slots', EMBED_SLASH_COMMANDS_PATH, EMBED_FILE_SEARCH_PATH]
const UNGRANTED = ['/api/chat', '/api/chat/slots']

const box = () => screen.getByLabelText('Chat message') as HTMLTextAreaElement
const type = (value: string) => fireEvent.change(box(), { target: { value, selectionStart: value.length } })
const chatPosts = () => mockPost.mock.calls.filter(c => String(c[0]).startsWith('/api/chat?'))
const pickerReads = () => mockGet.mock.calls.map(c => String(c[0])).filter(p => !p.startsWith('/api/chat/'))

beforeEach(() => {
  vi.clearAllMocks()
  resetEmbedRecoveryForTests()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockImplementation(async (path: string) => {
    if (path.startsWith(EMBED_SLASH_COMMANDS_PATH)) return COMMANDS
    if (path.startsWith(EMBED_FILE_SEARCH_PATH)) return FILES
    return { messages: [], running: false, title: '' }
  })
  mockPost.mockResolvedValue({ ok: true, dispatched: true })
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

describe('embedPickerSource', () => {
  it('carries a read only for a declared endpoint, and never path or skill reads', () => {
    const api = { get: mockGet } as never
    const none = embedPickerSource(api, 'host', UNGRANTED)
    expect(offers(none, 'slashCommands')).toBe(false)
    expect(offers(none, 'fileSearch')).toBe(false)
    const all = embedPickerSource(api, 'host', ['/api/*'])
    expect(offers(all, 'slashCommands')).toBe(true)
    expect(offers(all, 'fileSearch')).toBe(true)
    expect(offers(all, 'pathComplete')).toBe(false)
    expect(offers(all, 'skills')).toBe(false)
    expect(all.scope).toBe('app:host')
  })
})

describe('ChatEmbed pickers with the grants', () => {
  it('opens the / menu from the scoped client and leaves out what the embed cannot run', async () => {
    mount(GRANTED)
    await act(async () => { type('/') })
    const list = await screen.findByRole('listbox')
    await waitFor(() => expect(within(list).getByText('/compact')).toBeInTheDocument())
    expect(within(list).queryByText('/side')).toBeNull()
    expect(within(list).queryByText('/onboarding')).toBeNull()
    expect(mockGet).toHaveBeenCalledWith(EMBED_SLASH_COMMANDS_PATH, expect.objectContaining({ signal: expect.anything() }))
    expect(dashboardApi.slashCommands).not.toHaveBeenCalled()
  })

  it('Enter on a command fills the composer instead of sending', async () => {
    mount(GRANTED)
    await act(async () => { type('/comp') })
    const list = await screen.findByRole('listbox')
    await waitFor(() => expect(within(list).getByText('/compact')).toBeInTheDocument())
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    expect(box().value).toBe('/compact ')
    expect(chatPosts()).toHaveLength(0)
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
  })

  it('an accepted @ file writes its token and rides the send as an attachment', async () => {
    mount(GRANTED)
    await act(async () => { type('see @app') })
    const list = await screen.findByRole('listbox')
    await waitFor(() => expect(within(list).getByText('app.ts')).toBeInTheDocument())
    expect(mockGet).toHaveBeenCalledWith(`${EMBED_FILE_SEARCH_PATH}?q=app`, expect.objectContaining({ signal: expect.anything() }))
    expect(dashboardApi.fileSearch).not.toHaveBeenCalled()
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(box().value).toBe('see @src/app.ts '))
    expect(screen.getByRole('group', { name: `${ROOT}/src/app.ts` })).toBeInTheDocument()
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(chatPosts()).toHaveLength(1))
    expect(chatPosts()[0][1].meta).toEqual({ files: [`${ROOT}/src/app.ts`], sendId: expect.any(String) })
  })

  it('same-named @ results each show the folder that tells them apart', async () => {
    const twins = { root: ROOT, results: ['packages/a', 'packages/b'].map(d => ({ path: `${ROOT}/${d}/README.md`, name: 'README.md', size: 1, mtime: 1750000000, kind: 'file' })) }
    mockGet.mockImplementation(async (path: string) => path.startsWith(EMBED_FILE_SEARCH_PATH) ? twins : path.startsWith(EMBED_SLASH_COMMANDS_PATH) ? COMMANDS : { messages: [], running: false, title: '' })
    mount(GRANTED)
    await act(async () => { type('see @readme') })
    const list = await screen.findByRole('listbox')
    await waitFor(() => expect(within(list).getAllByText('README.md')).toHaveLength(2))
    const folders = within(list).getAllByTestId('file-picker-row-folder')
    expect(folders.map(f => f.textContent)).toEqual(['packages/a/', 'packages/b/'])
    // Cut from the start, so the differing end stays in view.
    expect(folders.every(f => f.getAttribute('dir') === 'rtl')).toBe(true)
  })

  it('removing a picked file chip also takes its @token out of the text', async () => {
    mount(GRANTED)
    await act(async () => { type('see @app') })
    const list = await screen.findByRole('listbox')
    await waitFor(() => expect(within(list).getByText('app.ts')).toBeInTheDocument())
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(box().value).toBe('see @src/app.ts '))
    await act(async () => { type('see @src/app.ts now') })
    const chip = screen.getByRole('group', { name: `${ROOT}/src/app.ts` })
    await act(async () => { fireEvent.click(within(chip).getByRole('button', { name: 'Remove' })) })
    expect(screen.queryByRole('group', { name: `${ROOT}/src/app.ts` })).toBeNull()
    expect(box().value).toBe('see now')
  })
})

describe('stripPickedToken', () => {
  it('takes out the removed mention and one space after it', () => {
    expect(stripPickedToken('see @src/app.ts now', '@src/app.ts', [])).toBe('see now')
    expect(stripPickedToken('@a.ts and @a.ts', '@a.ts', [])).toBe('and ')
  })

  it('leaves a still-picked mention that extends the removed one', () => {
    const text = 'compare @report final.pdf with @report '
    expect(stripPickedToken(text, '@report', ['@report final.pdf'])).toBe('compare @report final.pdf with ')
    expect(stripPickedToken(text, '@report final.pdf', ['@report'])).toBe('compare with @report ')
  })
})

describe('ChatEmbed pickers without the grants', () => {
  it('opens no menu, makes no picker read, and Enter sends the text', async () => {
    mount(UNGRANTED)
    await act(async () => { type('/compact') })
    expect(screen.queryByRole('listbox')).toBeNull()
    await act(async () => { type('see @app') })
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(pickerReads()).toEqual([])
    expect(dashboardApi.slashCommands).not.toHaveBeenCalled()
    expect(dashboardApi.fileSearch).not.toHaveBeenCalled()
    await act(async () => { fireEvent.keyDown(box(), { key: 'Enter' }) })
    await waitFor(() => expect(chatPosts()).toHaveLength(1))
    expect(chatPosts()[0][1]).toMatchObject({ message: 'see @app' })
  })

  it('with only the command grant, @ stays text: no file menu and no file search', async () => {
    mount([...UNGRANTED, EMBED_SLASH_COMMANDS_PATH])
    // Positive signal first: the command half opens under the same composer.
    await act(async () => { type('/') })
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
    await act(async () => { type('see @app') })
    // An offered file menu mounts and opens on this same render, before any
    // debounced search, so its absence here is not a race.
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(mockGet.mock.calls.some(c => String(c[0]).startsWith(EMBED_FILE_SEARCH_PATH))).toBe(false)
    expect(dashboardApi.fileSearch).not.toHaveBeenCalled()
  })
})
