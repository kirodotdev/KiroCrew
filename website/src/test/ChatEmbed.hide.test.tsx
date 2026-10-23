/**
 * ChatEmbed's `hide` prop: each key removes exactly one part, at the one place
 * that part mounts, and removes nothing else. Each part is checked both ways
 * (shown by default, gone when hidden) so dropping a gate turns a test red.
 * The REAL ChatMessageList is mounted, so the tool-details gate is exercised
 * through the transcript's own tool rows; only the API boundary is stubbed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import React from 'react'
import path from 'node:path'
import fs from 'node:fs'
import ts from 'typescript'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()
const mockRaw = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost, raw: mockRaw }),
  useAppInfo: () => ({ name: 'host', version: '0', permissions: { api: [], events: [] } }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'
import { AppSdkContext, type AppSdkContextValue } from '../app-sdk/scopedApi'
import { EMBED_UPLOAD_PATH } from '../app-sdk/useEmbedAttachments'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'
import { CHAT_EMBED_HIDE_KEYS, type ChatEmbedHideKey } from '../app-sdk/chatEmbedHide'

/** Upload is granted, so an attach control that is missing was hidden, not refused. */
const GRANTED = ['/api/chat', '/api/chat/slots', EMBED_UPLOAD_PATH]

let queryClient: QueryClient

function tree(hide?: readonly ChatEmbedHideKey[]) {
  const ctx = {
    api: { get: mockGet, post: mockPost, raw: mockRaw },
    info: { name: 'host', version: '0', permissions: { api: GRANTED, events: [] } },
    subscribe: () => () => {},
    navigate: () => {},
    notify: () => {},
  } as unknown as AppSdkContextValue
  return (
    <QueryClientProvider client={queryClient}>
      <AppSdkContext.Provider value={ctx}>
        <ChatEmbed slotKey="s1" hide={hide} />
      </AppSdkContext.Provider>
    </QueryClientProvider>
  )
}

async function mount(hide?: readonly ChatEmbedHideKey[]) {
  let view!: ReturnType<typeof render>
  await act(async () => { view = render(tree(hide)) })
  await act(async () => { vi.advanceTimersByTime(100) })
  return view
}

const TOOL_ROW = {
  role: 'tool_call', content: '🔧 read_file', cls: '', ts: '2026-01-01T00:00:02Z',
  meta: { input: '{"path":"a.txt"}', output: 'file body' },
}

/** Two slot reads that between them show every hideable part. Stop needs a
 *  running turn and follow-up chips need a settled one, so no single read
 *  shows both. */
const slot = (running: boolean) => ({
  running,
  title: 'Embed',
  messages: [
    { role: 'user', content: 'go', cls: '', ts: '2026-01-01T00:00:01Z' },
    TOOL_ROW,
    { role: 'assistant', content: 'Next? [OPTIONS: Run tests | Skip]', cls: '', ts: '2026-01-01T00:00:03Z' },
  ],
  queue: [{ id: 'q1', content: 'please also check the logs' }],
})
const BUSY = slot(true)
const IDLE = slot(false)

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  resetEmbedRecoveryForTests()
  mockRaw.mockReset()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue(BUSY)
  mockPost.mockResolvedValue({})
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

afterEach(() => {
  vi.useRealTimers()
})

/** The parts a read can show: Stop only while running, chips only when not. */
const shownIn = (running: boolean) =>
  CHAT_EMBED_HIDE_KEYS.filter(k => (k === 'stop' ? running : k === 'followUps' ? !running : true))

const parts: Record<ChatEmbedHideKey, () => HTMLElement | null> = {
  header: () => screen.queryByTestId('chat-embed-title'),
  attach: () => screen.queryByLabelText('Attach files'),
  stop: () => screen.queryByTestId('stop-button-armed'),
  queue: () => screen.queryByTestId('chat-embed-queue'),
  toolDetails: () => screen.queryByRole('button', { name: 'read_file' }),
  followUps: () => screen.queryByText('Run tests'),
}

describe('ChatEmbed hide', () => {
  it.each([[true], [false]])('shows every part when hide is omitted (running=%s)', async (running) => {
    mockGet.mockResolvedValue(running ? BUSY : IDLE)
    await mount()
    for (const key of shownIn(running)) expect(parts[key](), key).not.toBeNull()
  })

  it.each(CHAT_EMBED_HIDE_KEYS.flatMap(k => [[k, true], [k, false]] as const))('hiding %s removes only it (running=%s)', async (key, running) => {
    mockGet.mockResolvedValue(running ? BUSY : IDLE)
    await mount([key])
    expect(parts[key]()).toBeNull()
    for (const other of shownIn(running)) {
      if (other !== key) expect(parts[other](), other).not.toBeNull()
    }
    // The transcript and the editor are never hideable.
    expect(screen.getByLabelText('Chat message')).toBeInTheDocument()
  })

  it('keeps Send where Stop would be when stop is hidden', async () => {
    await mount(['stop'])
    // Empty draft: Send is there but cannot send.
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'more' } })
    expect(screen.getByRole('button', { name: 'Queue message' })).toBeEnabled()
  })

  it('refuses a pasted file when attach is hidden, even with the upload grant', async () => {
    await mount(['attach'])
    const box = screen.getByLabelText('Chat message')
    fireEvent.paste(box, { clipboardData: { files: [new File(['x'], 'shot.png', { type: 'image/png' })], types: ['Files'] } })
    await act(async () => { await Promise.resolve() })
    expect(screen.getByTestId('chat-embed-attach-notice')).toHaveTextContent("This app doesn't accept files")
    expect(mockRaw).not.toHaveBeenCalled()
  })

  it('draws a tool row as a static pill with no panel when toolDetails is hidden', async () => {
    await mount(['toolDetails'])
    const pill = screen.getByTestId('tool-pill-static')
    expect(pill).toHaveTextContent('read_file')
    fireEvent.click(pill)
    expect(screen.queryByTestId('tool-pill-details')).toBeNull()
  })

  it('closes an open tool panel when the host starts hiding toolDetails', async () => {
    const view = await mount()
    fireEvent.click(screen.getByRole('button', { name: 'read_file' }))
    expect(screen.getByTestId('tool-pill-details')).toBeInTheDocument()
    await act(async () => { view.rerender(tree(['toolDetails'])) })
    expect(screen.queryByTestId('tool-pill-details')).toBeNull()
  })
})

describe('ChatEmbedHideKey', () => {
  const sdkDir = path.resolve(__dirname, '../app-sdk')
  const hideFile = path.join(sdkDir, 'chatEmbedHide.ts')

  /** Type-check `code` as a module beside chatEmbedHide.ts; returns the errors. */
  function typeErrors(code: string): string[] {
    const probe = path.join(sdkDir, '__hide_probe__.ts')
    const options: ts.CompilerOptions = { strict: true, noEmit: true, target: ts.ScriptTarget.ES2020, lib: ['lib.es2020.d.ts'], types: [] }
    const host = ts.createCompilerHost(options)
    const getSourceFile = host.getSourceFile.bind(host)
    const fileExists = host.fileExists.bind(host)
    host.getSourceFile = (f, lang, ...rest) => (f === probe ? ts.createSourceFile(f, code, lang) : getSourceFile(f, lang, ...rest))
    host.fileExists = f => f === probe || fileExists(f)
    const program = ts.createProgram([probe], options, host)
    return ts.getPreEmitDiagnostics(program).map(d => ts.flattenDiagnosticMessageText(d.messageText, '\n'))
  }

  it('accepts every listed key', () => {
    const keys = CHAT_EMBED_HIDE_KEYS.map(k => `'${k}'`).join(', ')
    expect(typeErrors(`import type { ChatEmbedHideKey } from './chatEmbedHide'\nexport const ok: ChatEmbedHideKey[] = [${keys}]\n`)).toEqual([])
  })

  it('rejects an unknown key', () => {
    const errors = typeErrors(`import type { ChatEmbedHideKey } from './chatEmbedHide'\nexport const bad: ChatEmbedHideKey[] = ['composer']\n`)
    expect(errors.join('\n')).toMatch(/'"composer"' is not assignable/)
  })

  it('is the type of ChatEmbed\'s hide prop and is exported from the SDK', () => {
    expect(fs.existsSync(hideFile)).toBe(true)
    const embed = fs.readFileSync(path.join(sdkDir, 'ChatEmbed.tsx'), 'utf8')
    expect(embed).toMatch(/\bhide\?: readonly ChatEmbedHideKey\[\]/)
    const index = fs.readFileSync(path.join(sdkDir, 'index.ts'), 'utf8')
    expect(index).toMatch(/export type \{ ChatEmbedHideKey \} from '\.\/chatEmbedHide'/)
  })
})
