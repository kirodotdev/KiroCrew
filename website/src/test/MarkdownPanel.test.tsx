import { describe, it, expect, vi, beforeEach } from 'vitest'
import { join } from 'node:path'
import { readSource } from './readSource'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { OverflowMenu, breadcrumbSegments } from '../components/MarkdownPanel'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn(),
    artifact: vi.fn(),
    createArtifact: vi.fn(),
    revealPath: vi.fn(),
  },
}))

const writeText = vi.fn()
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  </MemoryRouter>
)

beforeEach(() => {
  writeText.mockReset()
  queryClient.clear()
  // happy-dom's navigator.clipboard is getter-only; defineProperty replaces it.
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  // Default: no existing artifact for any path. Tests can override.
  vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
  vi.mocked(api).createArtifact = vi.fn().mockResolvedValue({ slug: 'test-doc-md', version: 1 })
  // Desktop present by default: the backend acted, nothing to copy back.
  vi.mocked(api).revealPath = vi.fn().mockResolvedValue({ ok: true })
  vi.spyOn(window, 'alert').mockImplementation(() => {})
})

function openMenu() {
  render(<OverflowMenu filePath="/tmp/hello.txt" content={'line one\nline two\n'} />, { wrapper })
  fireEvent.click(screen.getAllByRole('button')[0])
}

describe('MarkdownPanel OverflowMenu', () => {
  it('exposes both Copy path and Copy content entries', () => {
    openMenu()
    expect(screen.getByText('Copy path')).toBeInTheDocument()
    expect(screen.getByText('Copy content')).toBeInTheDocument()
  })

  it('Copy path writes the filePath to the clipboard', () => {
    openMenu()
    fireEvent.click(screen.getByText('Copy path'))
    expect(writeText).toHaveBeenCalledExactlyOnceWith('/tmp/hello.txt')
  })

  it('Copy content writes the raw file content to the clipboard', () => {
    openMenu()
    fireEvent.click(screen.getByText('Copy content'))
    expect(writeText).toHaveBeenCalledExactlyOnceWith('line one\nline two\n')
  })

  it('closes the overflow menu after Copy content is clicked', () => {
    openMenu()
    expect(screen.getByText('Copy content')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Copy content'))
    expect(screen.queryByText('Copy content')).not.toBeInTheDocument()
  })

  it('Copy content copies an empty string for an empty file without throwing', () => {
    render(<OverflowMenu filePath="/tmp/empty.txt" content="" />, { wrapper })
    fireEvent.click(screen.getAllByRole('button')[0])
    fireEvent.click(screen.getByText('Copy content'))
    expect(writeText).toHaveBeenCalledExactlyOnceWith('')
  })

  // The two desktop hand-off entries were dropped by the side-panel/artifacts
  // reconciliation (79a448b6, PR #1083) while the backend endpoint stayed
  // live, so the panel had no way to leave the browser. These lock the pair
  // back in — including the ACTION each one sends, which is the only thing
  // distinguishing them at the API.
  it('exposes both desktop hand-off entries', () => {
    openMenu()
    expect(screen.getByText('Open with default app')).toBeInTheDocument()
    expect(screen.getByText('Show in file manager')).toBeInTheDocument()
  })

  it('Open with default app asks the backend for the open action', () => {
    openMenu()
    fireEvent.click(screen.getByText('Open with default app'))
    expect(api.revealPath).toHaveBeenCalledExactlyOnceWith('/tmp/hello.txt', 'open')
  })

  it('Show in file manager asks the backend for the reveal action', () => {
    openMenu()
    fireEvent.click(screen.getByText('Show in file manager'))
    expect(api.revealPath).toHaveBeenCalledExactlyOnceWith('/tmp/hello.txt', 'reveal')
  })

  it('closes the overflow menu after a desktop hand-off is clicked', () => {
    openMenu()
    fireEvent.click(screen.getByText('Show in file manager'))
    expect(screen.queryByText('Show in file manager')).not.toBeInTheDocument()
  })

  /**
   * The reveal entry names the GATEWAY's file manager: `/api/reveal` shells out
   * there, so a dashboard opened from a Mac against a Linux gateway must not say
   * Finder. `'gateway'` is the sentinel a non-owner dashboard user gets and must
   * never be read as a platform we can name.
   */
  it.each([
    ['darwin', 'Open in Finder'],
    ['win32', 'Open in File Explorer'],
    ['gateway', 'Show in file manager'],
    ['linux', 'Show in file manager'],
  ])('names the reveal entry for a %s gateway host', (platform, label) => {
    queryClient.setQueryData(['kiro-prerequisite'], { platform })
    openMenu()
    expect(screen.getByText(label)).toBeInTheDocument()
    fireEvent.click(screen.getByText(label))
    expect(api.revealPath).toHaveBeenCalledExactlyOnceWith('/tmp/hello.txt', 'reveal')
  })

  it('never offers two spellings of the same reveal entry at once', () => {
    queryClient.setQueryData(['kiro-prerequisite'], { platform: 'darwin' })
    openMenu()
    expect(screen.queryByText('Show in file manager')).not.toBeInTheDocument()
    expect(screen.queryByText('Open in File Explorer')).not.toBeInTheDocument()
  })

  it('tells the user the path was copied when the host has no desktop', async () => {
    vi.mocked(api).revealPath = vi.fn().mockResolvedValue({ ok: true, copy: '/tmp/hello.txt' })
    openMenu()
    fireEvent.click(screen.getByText('Show in file manager'))
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith(
      'Path copied to clipboard (no desktop available)',
    ))
  })

  it('surfaces the server message when the reveal is refused', async () => {
    vi.mocked(api).revealPath = vi.fn().mockRejectedValue(new Error('access denied'))
    openMenu()
    fireEvent.click(screen.getByText('Open with default app'))
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith('access denied'))
  })
})

/**
 * The ⋯ menu's full inventory, asserted as an ORDERED list.
 *
 * Why a whole-inventory assertion rather than one `getByText` per entry: PR
 * #1083 deleted two entries from this menu and the suite stayed green, because
 * no test named them. Per-entry tests only protect the entries someone thought
 * to name — the two that went missing were, by definition, not among them.
 *
 * This locks the list instead of its members. A deletion fails; so does an
 * addition or a reorder, which is deliberate: the failure asks the author to
 * state the new inventory here, and that edit is the record that the change
 * was intended. Whoever wrote #1083 would have had to make it.
 *
 * The three cases below are the conditional matrix, not three flavours of the
 * same render — every optional entry in the menu is gated on a prop or on
 * fetched state, so a single render can only ever see a subset.
 */
describe('OverflowMenu inventory (regression guard for #1083)', () => {
  /** Menu items in DOM order — the roving-focus order `useListboxKeyboard` walks. */
  const itemsInOrder = () =>
    Array.from(document.querySelectorAll('[role="menu"] [role="menuitem"]'))
      .map(el => (el.textContent || '').trim())

  /** The knowledge hook's own contract: `enabled` + `supported_formats`. */
  function stubKnowledge({ enabled, alreadyAdded }: { enabled: boolean; alreadyAdded: boolean }) {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (String(url).startsWith('/api/knowledge/config')) {
        return { ok: true, json: async () => ({ enabled, supported_formats: ['.md', '.txt'] }) }
      }
      if (String(url).startsWith('/api/knowledge/sources')) {
        return { ok: true, json: async () => (alreadyAdded ? [{ id: 1 }] : []) }
      }
      return { ok: false, json: async () => ({}) }
    }))
  }

  it('renders exactly six entries with no optional props and no library match', async () => {
    stubKnowledge({ enabled: false, alreadyAdded: false })
    render(<OverflowMenu filePath="/tmp/hello.bin" content="x" />, { wrapper })
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    await waitFor(() => expect(screen.getByText('Add to artifacts')).toBeInTheDocument())
    expect(itemsInOrder()).toEqual([
      'Add to artifacts',
      'Open with default app',
      'Show in file manager',
      'Copy path',
      'Copy content',
      'Download',
    ])
  })

  it('renders every entry when all props are supplied and the file is a known artifact', async () => {
    stubKnowledge({ enabled: true, alreadyAdded: false })
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [{ slug: 'notes-md', name: 'notes.md' }] })
    vi.mocked(api).artifact = vi.fn().mockResolvedValue({ live_dirty: false, pinned: false })
    render(
      <OverflowMenu
        filePath="/tmp/notes.md"
        content="x"
        onRefresh={vi.fn()}
        onFullscreen={vi.fn()}
        onSnapshot={vi.fn()}
      />,
      { wrapper },
    )
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    await waitFor(() => expect(screen.getByText('Snapshot version')).toBeInTheDocument())
    expect(itemsInOrder()).toEqual([
      'Refresh',
      'Full screen',
      'In Artifacts',
      'Snapshot version',
      'Add to Knowledge',
      'Open with default app',
      'Show in file manager',
      'Copy path',
      'Copy content',
      'Download',
    ])
  })

  it('swaps Full screen for Exit full screen without changing the rest of the list', async () => {
    stubKnowledge({ enabled: false, alreadyAdded: false })
    render(
      <OverflowMenu filePath="/tmp/hello.bin" content="x" onFullscreen={vi.fn()} fullscreen />,
      { wrapper },
    )
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    await waitFor(() => expect(screen.getByText('Exit full screen')).toBeInTheDocument())
    expect(itemsInOrder()).toEqual([
      'Exit full screen',
      'Add to artifacts',
      'Open with default app',
      'Show in file manager',
      'Copy path',
      'Copy content',
      'Download',
    ])
    expect(screen.queryByText('Full screen')).not.toBeInTheDocument()
  })

  /* Re-homed from the old Files tab, which had its own add-to-library control
   * on each file row. That tab now lists links only, so this guard lives here —
   * the file editor is the remaining way a plain file enters the library, and
   * the failure it prevents is silent, permanent data loss. */
  it('refuses to add a truncated read instead of persisting the prefix', async () => {
    // /api/file-read caps very large files and says so in a header. Promoting
    // that body would store the PREFIX as though it were the whole document,
    // and a disposable file is COPIED, so the original is not referenced.
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers({ 'X-Truncated': 'true' }),
      text: () => Promise.resolve('the first 512 KB only'),
    }) as never
    render(<OverflowMenu filePath="/tmp/huge.txt" content={'prefix'} />, { wrapper })
    fireEvent.click(screen.getAllByRole('button')[0])
    fireEvent.click(await screen.findByText('Add to artifacts'))

    await waitFor(() => expect(global.fetch).toHaveBeenCalled())
    expect(api.createArtifact).not.toHaveBeenCalled()
  })

  it('adds a complete read, so the refusal above is the header and not a dead path', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers(),
      text: () => Promise.resolve('the whole file'),
    }) as never
    render(<OverflowMenu filePath="/tmp/small.txt" content={'the whole file'} />, { wrapper })
    fireEvent.click(screen.getAllByRole('button')[0])
    fireEvent.click(await screen.findByText('Add to artifacts'))

    await waitFor(() => expect(api.createArtifact).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.createArtifact).mock.calls[0][0]).toMatchObject({
      content: 'the whole file', source_path: '/tmp/small.txt',
    })
  })

  it('renders the already-in-library row as a non-actionable status, not a menu item', async () => {
    stubKnowledge({ enabled: true, alreadyAdded: true })
    render(<OverflowMenu filePath="/tmp/notes.md" content="x" />, { wrapper })
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    await waitFor(() => expect(screen.getByText('In Library')).toBeInTheDocument())
    // It is a <span>: nothing happens when it is activated, so exposing it to
    // roving focus as a menuitem would be a dead stop on the keyboard path.
    expect(itemsInOrder()).not.toContain('In Library')
    expect(screen.queryByText('Add to Knowledge')).not.toBeInTheDocument()
  })
})

/**
 * The menu opens with real DOM focus on its first row (`useListboxKeyboard`,
 * WAI-ARIA menu pattern), so whichever tint marks the focused row is on screen
 * from the moment the menu appears — before the pointer has gone anywhere near
 * it. That tint must therefore be scoped to `focus-visible`, which a
 * script-moved focus only matches when a keypress moved it: a bare `focus:`
 * tint is the same colour as `hover:`, leaving the first row lit for the whole
 * time the menu is open and two rows lit as soon as the pointer picks another.
 *
 * Asserted across the whole inventory, not just the first row: the way this
 * regresses is a new entry pasted from an existing one.
 */
describe('OverflowMenu roving-focus tint', () => {
  const rows = () => Array.from(document.querySelectorAll<HTMLElement>('[role="menu"] [role="menuitem"]'))

  it('focuses the first row on open and tints rows only under :focus-visible', async () => {
    render(
      <OverflowMenu filePath="/tmp/notes.md" content="x" onRefresh={vi.fn()} onFullscreen={vi.fn()} />,
      { wrapper },
    )
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    expect(rows()[0]).toHaveTextContent('Refresh')
    // The hook moves focus in a 0ms timeout, so it lands after this tick.
    await waitFor(() => expect(document.activeElement).toBe(rows()[0]))
    for (const row of rows()) {
      expect(row.className).toContain('focus-visible:bg-bg-hover')
      expect(row.className).not.toMatch(/(^|\s)focus:bg-/)
    }
  })
})

describe('breadcrumbSegments', () => {
  it('shows the last three segments with the file last and non-navigable', () => {
    const crumbs = breadcrumbSegments('/home/user/project/src/app.ts')
    expect(crumbs.map(c => c.seg)).toEqual(['project', 'src', 'app.ts'])
    expect(crumbs.map(c => c.isFile)).toEqual([false, false, true])
  })

  it('gives each directory segment its full absolute ancestor path', () => {
    const crumbs = breadcrumbSegments('/home/user/project/src/app.ts')
    // Even though only the tail is shown, a clicked directory opens its true
    // absolute path — not a relative fragment of the visible segments.
    expect(crumbs[0].path).toBe('/home/user/project')
    expect(crumbs[1].path).toBe('/home/user/project/src')
    expect(crumbs[2].path).toBe('/home/user/project/src/app.ts')
  })

  it('preserves a leading slash for a shallow absolute path', () => {
    const crumbs = breadcrumbSegments('/tmp/notes.md')
    expect(crumbs.map(c => c.path)).toEqual(['/tmp', '/tmp/notes.md'])
    expect(crumbs.map(c => c.isFile)).toEqual([false, true])
  })

  it('handles a relative path without inventing a leading slash', () => {
    const crumbs = breadcrumbSegments('docs/guide/intro.md')
    expect(crumbs.map(c => c.path)).toEqual(['docs', 'docs/guide', 'docs/guide/intro.md'])
  })

  it('handles a bare filename as a single file segment', () => {
    const crumbs = breadcrumbSegments('/README.md')
    expect(crumbs).toEqual([{ seg: 'README.md', path: '/README.md', isFile: true }])
  })
})

/**
 * The line-reveal effect must declare its dependencies, and the editor handle
 * it waits on must be state rather than a ref.
 *
 * This is a source-level guard on purpose. A dependency-less effect re-runs on
 * every render, and the nonce guard inside this one makes those extra runs
 * idempotent — so the defect has no rendered symptom to assert against, only a
 * cost that grows with every unrelated re-render of the panel. What IS
 * statically visible is the shape: the dependency array, the state-backed
 * handle that re-runs the effect on the commit that mounts the editor (a ref
 * attaches without a render and would strand a reveal requested before the
 * editor exists), and the latest-value ref that keeps the host's inline
 * `onRevealConsumed` arrow out of the dependency array.
 */
describe('MarkdownPanel line-reveal effect', () => {
  // Read through `readSource`, which normalizes line endings to LF. This block
  // asserts on the SOURCE TEXT with `$`-anchored patterns; `website/.gitattributes`
  // now pins `*.tsx eol=lf` so a Windows checkout no longer carries the CRLF that
  // used to defeat those anchors, and this read stays defensive for a working tree
  // that predates that attribute or carries an unusual git config.
  const src = readSource(join(__dirname, '..', 'components', 'MarkdownPanel.tsx'))

  /** The reveal `useEffect` call, from `useEffect(` through its closing line.
   *  Only the effect's own closer sits at two-space indentation. */
  const effect = (() => {
    const anchor = src.indexOf('lastRevealNonce.current === revealLine.nonce')
    expect(anchor).toBeGreaterThan(-1)
    const open = src.lastIndexOf('useEffect(', anchor)
    const closerLine = src.indexOf('\n  }', anchor)
    return src.slice(open, src.indexOf('\n', closerLine + 1))
  })()

  it('declares a dependency array', () => {
    expect(effect).toMatch(/\}, \[[^\]]*\]\)$/)
  })

  it('depends on the reveal target, the source-mode predicate and the editor handle', () => {
    const deps = (effect.match(/\}, \[([^\]]*)\]\)$/)?.[1] ?? '').split(',').map(d => d.trim())
    expect(deps).toEqual(expect.arrayContaining(['revealLine', 'revealTargetsSource', 'revealEditor']))
  })

  it('keeps the nonce guard, which is what makes a repeat reveal need a new nonce', () => {
    expect(effect).toContain('lastRevealNonce.current === revealLine.nonce')
    expect(effect).toContain('lastRevealNonce.current = revealLine.nonce')
  })

  it('waits on a state-backed handle so a late editor mount still reveals', () => {
    expect(src).toContain('const [revealEditor, setRevealEditor] = useState<PierreEditorHandle | null>(null)')
    expect(src).not.toMatch(/editorRef=\{revealEditorRef\}/)
    expect(src).toMatch(/editorRef=\{setRevealEditor\}/)
  })

  it('reads the host callback through a ref instead of depending on it', () => {
    expect(effect).toContain('onRevealConsumedRef.current?.()')
    expect(effect).not.toMatch(/\bonRevealConsumed\?\.\(\)/)
  })
})
