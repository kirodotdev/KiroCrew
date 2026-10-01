import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, screen, waitFor } from '@testing-library/react'

import MarkdownRenderer from '../components/MarkdownRenderer'
import { SidebarFolderCtx, type SidebarFolderActions } from '../components/markdown/contexts'
import { resolveFolderChip } from '../components/markdown/linkTargets'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { copyToClipboard } from '../utils/clipboard'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn(async () => undefined) }))

/**
 * A sidebar-folder path in prose becomes a chip that REVEALS the folder.
 *
 * The roster is the sidebar's own folder list, provided once by the page through
 * `SidebarFolderCtx` (not a MarkdownRenderer prop), so these tests render the
 * renderer inside that provider the way ChatPage does. A `/`-shaped span is also
 * a path candidate, so the stat probe runs first; the folder chip may only claim
 * the span once the probe has said "not a path on disk", which the fetch stub
 * below answers.
 */

const realFetch = globalThis.fetch

/** The probe answers "this is not a path", the state a folder chip needs. */
function stubMissing() {
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({ ok: false, status: 404, headers: new Headers() } as Response),
  ) as unknown as typeof fetch
}

/** The probe confirms a directory on disk: the PATH chip wins the span. */
function stubDir() {
  globalThis.fetch = vi.fn(() =>
    Promise.resolve({ ok: true, status: 200, headers: new Headers({ 'X-Path-Kind': 'dir' }) } as Response),
  ) as unknown as typeof fetch
}

/** The tree ChatPage would hand over: `goal` at the top, `worker` under it, and a
 *  second `worker` under a different parent so the leaf name alone is ambiguous. */
const FOLDERS = [
  { id: 'f-goal', name: 'goal' },
  { id: 'f-goal-worker', name: 'worker', parent_id: 'f-goal' },
  { id: 'f-other', name: 'other' },
  { id: 'f-other-worker', name: 'worker', parent_id: 'f-other' },
  { id: 'f-long', name: 'monitor-turn-budget' },
  { id: 'f-goal-worker-long', name: 'worker', parent_id: 'f-long' },
]

let onFolderReveal: ReturnType<typeof vi.fn>

/** The chip for a folder id. Its label is segmented into nowrap spans with a
 *  `<wbr>` after each separator, so `getByText` on the whole path cannot see it;
 *  the id attribute is the chip's own claim. */
function chipFor(id: string): HTMLElement {
  const el = document.querySelector(`code[data-folder-id="${id}"]`)
  if (!el) throw new Error(`no chip for ${id}`)
  return el as HTMLElement
}

function renderWithFolders(content: string, actions?: Partial<SidebarFolderActions>) {
  const value: SidebarFolderActions = { folders: FOLDERS, onFolderReveal, ...actions }
  return render(
    <SidebarFolderCtx.Provider value={value}>
      <MarkdownRenderer content={content} />
    </SidebarFolderCtx.Provider>,
  )
}

beforeEach(() => {
  vi.mocked(copyToClipboard).mockClear()
  __resetPathKindCache()
  onFolderReveal = vi.fn()
  stubMissing()
})

afterEach(() => {
  globalThis.fetch = realFetch
})

describe('resolveFolderChip — which spans name a folder', () => {
  const actions: SidebarFolderActions = { folders: FOLDERS, onFolderReveal: () => {} }

  it('matches the full human path, root to leaf', () => {
    expect(resolveFolderChip('goal/worker', actions)).toEqual({ id: 'f-goal-worker', path: 'goal/worker' })
    expect(resolveFolderChip('other/worker', actions)).toEqual({ id: 'f-other-worker', path: 'other/worker' })
  })

  it('reads the breadcrumb spelling the header shows', () => {
    expect(resolveFolderChip('goal › worker', actions)?.id).toBe('f-goal-worker')
    expect(resolveFolderChip('goal›worker', actions)?.id).toBe('f-goal-worker')
  })

  it('refuses a bare name, even one that is a top-level folder', () => {
    // A separator is the one shape signal a folder path has. With a top-level
    // folder called `test` every `test` span in every message would otherwise
    // lose its click-to-copy for a click that jerks the sidebar.
    expect(resolveFolderChip('goal', actions)).toBeNull()
    expect(resolveFolderChip('worker', actions)).toBeNull()
    expect(resolveFolderChip('goal/', actions)).toBeNull()
  })

  it('refuses a path that names more than one folder', () => {
    // The sidebar permits two sibling folders of one name; a chip that picked
    // the first would flash an arbitrary one with no sign it was ambiguous.
    const twins = [...FOLDERS, { id: 'f-goal-worker-2', name: 'worker', parent_id: 'f-goal' }]
    expect(resolveFolderChip('goal/worker', { ...actions, folders: twins })).toBeNull()
    expect(resolveFolderChip('other/worker', { ...actions, folders: twins })?.id).toBe('f-other-worker')
  })

  it('refuses a path a root folder NAMED with a slash also renders to', () => {
    // A folder name may contain `/` (the server only strips and caps it), so a
    // root folder literally called `goal/worker` renders to the same path as the
    // nested `goal` -> `worker`. Neither is the one the span means.
    const slashRoot = [...FOLDERS, { id: 'f-slash', name: 'goal/worker' }]
    expect(resolveFolderChip('goal/worker', { ...actions, folders: slashRoot })).toBeNull()
    // Alone, the slash-named root is reachable by its own rendered path.
    expect(resolveFolderChip('a/b', { ...actions, folders: [{ id: 'f-ab', name: 'a/b' }] })?.id).toBe('f-ab')
  })

  it('reads a breadcrumb-spelled folder NAME as the same path, so it collides too', () => {
    // The span's ` › ` is normalised to `/`; a root folder literally named
    // `goal › worker` must be indexed the same way, or the check never sees it
    // collide with the nested pair and the nested one is silently chosen.
    const crumbRoot = [...FOLDERS, { id: 'f-crumb', name: 'goal › worker' }]
    expect(resolveFolderChip('goal/worker', { ...actions, folders: crumbRoot })).toBeNull()
    expect(resolveFolderChip('goal › worker', { ...actions, folders: crumbRoot })).toBeNull()
    // Alone, it is reachable by either spelling.
    const only = { ...actions, folders: [{ id: 'f-crumb', name: 'goal › worker' }] }
    expect(resolveFolderChip('goal/worker', only)?.id).toBe('f-crumb')
    expect(resolveFolderChip('goal › worker', only)?.id).toBe('f-crumb')
  })

  it('renders no path for an orphan or cyclic row, so it cannot be named', () => {
    const broken = [
      { id: 'f-orphan', name: 'lost', parent_id: 'gone' },
      { id: 'f-c1', name: 'c1', parent_id: 'f-c2' },
      { id: 'f-c2', name: 'c2', parent_id: 'f-c1' },
      ...FOLDERS,
    ]
    expect(resolveFolderChip('lost', { ...actions, folders: broken })).toBeNull()
    expect(resolveFolderChip('c2/c1', { ...actions, folders: broken })).toBeNull()
    expect(resolveFolderChip('goal/worker', { ...actions, folders: broken })?.id).toBe('f-goal-worker')
  })

  it('tolerates one trailing separator and refuses every other empty segment', () => {
    expect(resolveFolderChip('goal/worker/', actions)?.id).toBe('f-goal-worker')
    expect(resolveFolderChip('/goal/worker', actions)).toBeNull()
    expect(resolveFolderChip('goal//worker', actions)).toBeNull()
  })

  it('is case-sensitive and whole-path: a prefix or a parent-less leaf is not a match', () => {
    expect(resolveFolderChip('Goal/worker', actions)).toBeNull()
    expect(resolveFolderChip('goal/work', actions)).toBeNull()
    expect(resolveFolderChip('goal/worker/extra', actions)).toBeNull()
  })

  it('offers nothing when the host wired no roster or no handler', () => {
    expect(resolveFolderChip('goal/worker', { onFolderReveal: () => {} })).toBeNull()
    expect(resolveFolderChip('goal/worker', { folders: FOLDERS })).toBeNull()
    expect(resolveFolderChip('goal/worker', {})).toBeNull()
  })
})

describe('folder chip — a sidebar folder path in prose', () => {
  it('reveals the folder on click, with no probe for a span that is not path-shaped', () => {
    renderWithFolders('The team sits under `goal/worker` in the sidebar.')
    // `goal/worker` has no root, no relative prefix and no extension, so it is
    // not a path candidate: nothing is probed and the chip is there on first render.
    expect(globalThis.fetch).not.toHaveBeenCalled()
    const chip = chipFor('f-goal-worker')
    expect(chip.tagName).toBe('CODE')
    expect(chip).toHaveTextContent('goal/worker')
    expect(chip).toHaveAttribute('role', 'button')
    expect(chip).toHaveAttribute('data-chip-action', 'navigate')
    expect(chip).toHaveAttribute('aria-label', 'Show folder goal/worker in sidebar')

    fireEvent.click(chip)
    expect(onFolderReveal).toHaveBeenCalledWith('f-goal-worker')
    // The whole point: a click reveals, it does not merely copy.
    expect(copyToClipboard).not.toHaveBeenCalled()
  })

  it('activates on Enter and Space', () => {
    renderWithFolders('`goal/worker`')
    const chip = chipFor('f-goal-worker')
    expect(chip).toHaveAttribute('tabindex', '0')
    fireEvent.keyDown(chip, { key: 'Enter' })
    fireEvent.keyDown(chip, { key: ' ' })
    expect(onFolderReveal).toHaveBeenCalledTimes(2)
  })

  it('copies the normalised path on Ctrl/Cmd+click instead of revealing', () => {
    renderWithFolders('`goal › worker`')
    const chip = chipFor('f-goal-worker')
    expect(chip).toHaveTextContent('goal › worker')
    fireEvent.click(chip, { metaKey: true })
    expect(copyToClipboard).toHaveBeenCalledWith('goal/worker')
    expect(onFolderReveal).not.toHaveBeenCalled()
  })

  it('says what a click does in its tooltip', () => {
    renderWithFolders('`goal/worker`')
    const title = chipFor('f-goal-worker').getAttribute('title') ?? ''
    expect(title).toContain('Click to show this folder in the sidebar')
    expect(title).toContain('Ctrl/Cmd+click to copy')
  })

  it('leaves a bare top-level folder name as the click-to-copy span', () => {
    renderWithFolders('Filed under `goal`.')
    const el = screen.getByText('goal')
    expect(el).not.toHaveAttribute('data-folder-id')
    expect(el).toHaveAttribute('aria-label', 'Copy goal')
  })

  it('breaks the label only at separators, never inside a segment', () => {
    renderWithFolders('`monitor-turn-budget/worker`')
    const chip = chipFor('f-goal-worker-long')
    // Each segment is an unbreakable run; the break opportunity follows the `/`.
    const runs = [...chip.querySelectorAll('span.whitespace-nowrap')].map(s => s.textContent)
    expect(runs).toEqual(['monitor-turn-budget', 'worker'])
    expect(chip.querySelectorAll('wbr')).toHaveLength(1)
    expect(chip).toHaveTextContent('monitor-turn-budget/worker')
  })

  it('holds the glyph width for a folder-shaped span the roster does not answer', () => {
    // The roster arrives async (cold query, invalidation on folder create), so a
    // folder-shaped span reserves the glyph's box before the answer, the way a
    // path-shaped span does for its probe: the chip restyles, it does not reflow.
    renderWithFolders('`goal/nobody`')
    const el = screen.getByText('goal/nobody')
    expect(el).not.toHaveAttribute('data-folder-id')
    const glyph = el.querySelector('svg')
    expect(glyph).not.toBeNull()
    expect(glyph!.getAttribute('class')).toContain('opacity-0')
    // A word with no separator gets none: it can never become a folder chip.
    render(<SidebarFolderCtx.Provider value={{ folders: FOLDERS, onFolderReveal }}><MarkdownRenderer content={'`npmtest`'} /></SidebarFolderCtx.Provider>)
    expect(screen.getByText('npmtest').querySelector('svg')).toBeNull()
    // Nor does a folder-shaped span OUTSIDE a provider: no roster, no answer in
    // flight, so no space is held and the span keeps its exact width.
    render(<MarkdownRenderer content={'`x/y`'} />)
    expect(screen.getByText('x/y').querySelector('svg')).toBeNull()
  })

  it('leaves a path no folder has as the click-to-copy span', () => {
    renderWithFolders('`goal/nobody`')
    const el = screen.getByText('goal/nobody')
    expect(el).toHaveAttribute('aria-label', 'Copy goal/nobody')
    expect(el).not.toHaveAttribute('data-folder-id')
    // `goal/nobody` has no root, no relative prefix and no extension, so it is
    // not a path candidate either: nothing was probed.
    expect(globalThis.fetch).not.toHaveBeenCalled()
  })

  it('offers no chip outside a provider, where the host does not know the tree', () => {
    render(<MarkdownRenderer content={'`goal/worker`'} />)
    const el = screen.getByText('goal/worker')
    expect(el).toHaveAttribute('aria-label', 'Copy goal/worker')
    expect(el).not.toHaveAttribute('data-folder-id')
  })

  describe('a folder path that is ALSO a path candidate', () => {
    // `reports/weekly.md` carries an extension, so the stat probe runs first and the
    // folder chip may claim the span only once the probe has answered.
    const SHAPED = [
      { id: 'f-reports', name: 'reports' },
      { id: 'f-reports-weekly', name: 'weekly.md', parent_id: 'f-reports' },
    ]

    it('becomes a folder chip once the probe says it is not on disk', async () => {
      renderWithFolders('`reports/weekly.md`', { folders: SHAPED })
      await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
      await waitFor(() => chipFor('f-reports-weekly'))
      fireEvent.click(chipFor('f-reports-weekly'))
      expect(onFolderReveal).toHaveBeenCalledWith('f-reports-weekly')
    })

    it('offers no chip while the probe is still in flight', () => {
      // A click during the wait must not reveal a folder the probe may overrule.
      globalThis.fetch = vi.fn(() => new Promise<Response>(() => {})) as unknown as typeof fetch
      renderWithFolders('`reports/weekly.md`', { folders: SHAPED })
      expect(globalThis.fetch).toHaveBeenCalled()
      expect(document.querySelector('code[data-folder-id]')).toBeNull()
    })

    it('yields the span to the PATH chip when the probe confirms it on disk', async () => {
      stubDir()
      renderWithFolders('`reports/weekly.md`', { folders: SHAPED })
      await waitFor(() => expect(screen.getByText('reports/weekly.md')).toHaveAttribute('data-path-kind', 'dir'))
      expect(document.querySelector('code[data-folder-id]')).toBeNull()
    })
  })

  it('drops a forged data-folder-id arriving from raw HTML', () => {
    renderWithFolders('<code data-folder-id="f-goal" data-chip-action="navigate">npm test</code>')
    const el = screen.getByText('npm test')
    expect(el).not.toHaveAttribute('data-folder-id')
    expect(el).toHaveAttribute('data-chip-action', 'copy')
  })
})
