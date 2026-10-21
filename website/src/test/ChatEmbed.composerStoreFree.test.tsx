/**
 * ChatEmbed mounts the chat-core composer (the `Composer` root plus its Editor
 * and Send atoms) and stays store-free while doing so:
 *
 * - statically: nothing the composer modules import reaches `src/store/` or a
 *   Redux package, so an app that has no `Provider` can mount it;
 * - at runtime: the embed renders, queues and sends with NO Redux `Provider`
 *   and NO router, and its one send goes through the app's scoped api (the
 *   `useAppApi` the host app's `allowedApiPaths` gate), not the dashboard
 *   client. The root's Voice atom is off for the embed: it would read STT
 *   config through the dashboard client and needs the dashboard router.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { readFileSync, existsSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))
vi.mock('../app-sdk/ChatMessageList', () => ({
  default: () => <div data-testid="chat-message-list" />,
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

// vitest's root is website/, so this resolves the same in every runner.
const SRC = resolve(__dirname, '..')

/** Every source file reachable from `entry` through runtime imports (type-only
 *  imports erase at build time and are skipped), plus the bare packages they
 *  name. Parent links let a failure print the chain that reached the store. */
function importGraph(entry: string) {
  const parent = new Map<string, string | null>()
  const bare = new Map<string, string>()
  const spec = /(?:^|\n)\s*(?:import|export)\s+(?!type\b)(?:[^'"]*?\bfrom\s*)?['"]([^'"]+)['"]|\bimport\(\s*['"]([^'"]+)['"]\s*\)/g
  const resolveFile = (from: string, s: string): string | null => {
    const base = resolve(dirname(from), s)
    for (const c of [base, `${base}.ts`, `${base}.tsx`, join(base, 'index.ts'), join(base, 'index.tsx')]) {
      if (existsSync(c) && statSync(c).isFile()) return c
    }
    return null
  }
  const walk = (file: string, from: string | null) => {
    if (parent.has(file)) return
    parent.set(file, from)
    const text = readFileSync(file, 'utf8')
    for (const m of text.matchAll(spec)) {
      const s = m[1] ?? m[2]
      if (s.startsWith('.')) {
        const next = resolveFile(file, s)
        if (next) walk(next, file)
      } else if (!bare.has(s)) {
        bare.set(s, file)
      }
    }
  }
  walk(entry, null)
  const chain = (file: string) => {
    const out: string[] = []
    for (let f: string | null | undefined = file; f; f = parent.get(f)) out.unshift(relative(SRC, f))
    return out.join(' -> ')
  }
  return { files: [...parent.keys()], bare, chain }
}

const REDUX = /^(react-redux|@reduxjs\/toolkit|redux)(\/|$)/

describe('ChatEmbed composer is store-free (static)', () => {
  it('the embed mounts the chat-core composer root and its atoms', () => {
    const src = readFileSync(join(SRC, 'app-sdk', 'ChatEmbed.tsx'), 'utf8')
    expect(src).toMatch(/from '\.\.\/chat-core\/composer\/Composer'/)
    expect(src).toMatch(/from '\.\.\/chat-core\/composer\/atoms'/)
    expect(src).toMatch(/<ComposerEditor\b/)
    expect(src).toMatch(/<ComposerSend\b/)
    // No hand-rolled composer beside the atoms (a JSX element, not prose).
    expect(src).not.toMatch(/<textarea[\s/]/)
  })

  it('the embed stages attachments through the chat-core Attach hook, not its own', () => {
    const src = readFileSync(join(SRC, 'app-sdk', 'useEmbedAttachments.ts'), 'utf8')
    expect(src).toMatch(/from '\.\.\/chat-core\/composer\/useComposerAttachments'/)
    expect(src).toMatch(/useComposerAttachments\(/)
    // The adapter holds no staging state of its own: upload, staging and
    // send-time recovery are the shared hook's.
    expect(src).not.toMatch(/\buseState\b/)
    expect(src).not.toMatch(/\buploadComposerFiles\b/)
  })

  it('the embed collapses long pastes through the chat-core Paste atom, not its own', () => {
    const src = readFileSync(join(SRC, 'app-sdk', 'ChatEmbed.tsx'), 'utf8')
    expect(src).toMatch(/from '\.\.\/chat-core\/composer\/pasteEditor'/)
    expect(src).toMatch(/from '\.\.\/chat-core\/composer\/composerPastes'/)
    expect(src).toMatch(/usePasteTokens\(/)
    expect(src).toMatch(/useComposerPastes\(/)
    expect(src).toMatch(/<Composer\b[^>]*\bpastes=\{pastes\}/)
    // The dashboard composer binds the same hook: one implementation.
    const input = readFileSync(join(SRC, 'components', 'ChatInput.tsx'), 'utf8')
    expect(input).toMatch(/import \{ usePasteTokens \} from '\.\.\/chat-core\/composer\/pasteEditor'/)
    expect(existsSync(join(SRC, 'components', 'chat-input', 'paste.ts'))).toBe(false)
    // Undo/redo, which restores a deleted chip's block, is the same hook too.
    expect(src).toMatch(/from '\.\.\/chat-core\/composer\/undoHistory'/)
    expect(src).toMatch(/useUndoHistory\(/)
    expect(input).toMatch(/import \{ useUndoHistory \} from '\.\.\/chat-core\/composer\/undoHistory'/)
  })

  it.each([
    'chat-core/composer/Composer.tsx',
    'chat-core/composer/atoms.tsx',
    'chat-core/composer/useComposerAttachments.ts',
    'chat-core/composer/pickers.ts',
    'chat-core/composer/PickerMenus.tsx',
    'app-sdk/useEmbedPickers.ts',
    'chat-core/composer/pasteEditor.ts',
    'chat-core/composer/undoHistory.ts',
    'chat-core/composer/composerPastes.ts',
    'components/PasteHighlightLayer.tsx',
    'components/PasteHoverLayer.tsx',
  ])('nothing %s imports reaches the Redux store', (entry) => {
    const g = importGraph(join(SRC, ...entry.split('/')))
    const storeFiles = g.files.filter(f => relative(SRC, f).startsWith('store/') || relative(SRC, f) === 'store.ts')
    expect(storeFiles.map(g.chain)).toEqual([])
    const reduxPkgs = [...g.bare].filter(([pkg]) => REDUX.test(pkg)).map(([pkg, by]) => `${pkg} <- ${relative(SRC, by)}`)
    expect(reduxPkgs).toEqual([])
  })
})

describe('ChatEmbed composer is store-free (runtime)', () => {
  let queryClient: QueryClient
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers()
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mockPost.mockResolvedValue({ ok: true, dispatched: true })
  })
  afterEach(() => { vi.useRealTimers() })

  // QueryClientProvider only: no Redux Provider, no router.
  async function mount(running: boolean) {
    mockGet.mockResolvedValue({ messages: [], running, title: '' })
    await act(async () => {
      render(<QueryClientProvider client={queryClient}><ChatEmbed slotKey="slot-1" /></QueryClientProvider>)
    })
    await act(async () => { vi.advanceTimersByTime(100) })
  }
  const editor = () => screen.getByRole('textbox', { name: /chat message/i })
  const sendBtn = () => document.querySelector<HTMLButtonElement>('[data-composer-send]')!

  it('draws the Editor and Send atoms and offers no microphone', async () => {
    await mount(false)
    expect(editor()).toHaveAttribute('data-composer-editor')
    expect(sendBtn().dataset.composerSend).toBe('idle')
    expect(sendBtn()).toBeDisabled()
    expect(screen.queryByRole('button', { name: /voice input/i })).toBeNull()
  })

  it('sends through the app scoped api', async () => {
    await mount(false)
    fireEvent.change(editor(), { target: { value: 'hello' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /send message/i })) })
    expect(mockPost).toHaveBeenCalledTimes(1)
    expect(String(mockPost.mock.calls[0][0])).toMatch(/^\/api\/chat(\?|$)/)
    expect(mockPost.mock.calls[0][1]).toMatchObject({ message: 'hello', slot: 'slot-1' })
  })

  it('a running turn turns Send into Queue once there is text', async () => {
    await mount(true)
    // An empty draft mid-turn holds the Stop control in Send's place.
    expect(sendBtn()).toBeNull()
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
    fireEvent.change(editor(), { target: { value: 'next' } })
    expect(sendBtn().dataset.composerSend).toBe('queue')
    expect(screen.getByRole('button', { name: /queue message/i })).toBe(sendBtn())
  })
})
