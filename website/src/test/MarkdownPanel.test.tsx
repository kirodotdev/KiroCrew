import { describe, it, expect, vi, beforeEach } from 'vitest'
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
