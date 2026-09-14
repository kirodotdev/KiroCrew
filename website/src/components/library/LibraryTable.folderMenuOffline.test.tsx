/**
 * The artifacts folder menu carries the same standing offline reason as its
 * siblings, so its own gateway writes have to be gated too — a reason row above a
 * full-weight Delete states something false and fires a doomed write.
 *
 * Renders `FolderMenu` DIRECTLY: a green assertion driven through the sidebar's
 * folder menu would prove nothing about this component. The row controls below
 * are rendered the same way, for the same reason.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type React from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from '../../test/helpers'
import { ThemeProvider } from '../../hooks/useTheme'
import type { RootState } from '../../store'
import type { Artifact, ArtifactFolder, SessionDoc } from '../../types'

vi.mock('../../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import { FolderMenu, ArtifactRow, SessionDocStar, LibraryTable, LibraryTree, FolderNameInput } from './LibraryTable'

const folder = { id: 'af1', name: 'Reports', parent_id: null, color: undefined } as unknown as ArtifactFolder

/** `connected` is explicit: createTestStore() models a DISCONNECTED dashboard, so
 *  an inherited default would run the offline branch while looking deliberate. */
function renderWithStore(connected: boolean, node: React.ReactNode) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>{node}</MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { utils, store }
}

function renderMenu(connected: boolean) {
  const actions = {
    onRename: vi.fn(), onDelete: vi.fn(), onMove: vi.fn(), onSetColor: vi.fn(),
  }
  const { utils, store } = renderWithStore(connected, <FolderMenu folder={folder} folders={[folder]} actions={actions as never} />)
  fireEvent.keyDown(utils.container.querySelector('button')!, { key: 'Enter' })
  return { actions, store }
}

const itemFor = (label: RegExp) => screen.getByText(label).closest('[aria-disabled]') as HTMLElement | null

beforeEach(() => vi.clearAllMocks())

describe('artifacts FolderMenu – the reason row must be true of the whole menu', () => {
  it('offline Rename and Delete are marked disabled, not just the Move submenu', () => {
    const { store } = renderMenu(false)
    expect(store.getState().dashboard.connected).toBe(false)
    expect(itemFor(/^Rename$/)?.getAttribute('aria-disabled')).toBe('true')
    expect(itemFor(/^Delete…$/)?.getAttribute('aria-disabled')).toBe('true')
  })

  it('offline Rename and Delete fire no write and leave the menu open', () => {
    const { actions } = renderMenu(false)
    fireEvent.click(screen.getByText(/^Delete…$/))
    expect(actions.onDelete).not.toHaveBeenCalled()
    // Suppressed through onSelect, so the menu — and its reason row — survives.
    expect(screen.getByText(/^Delete…$/)).toBeInTheDocument()
    fireEvent.click(screen.getByText(/^Rename$/))
    expect(actions.onRename).not.toHaveBeenCalled()
  })

  it('the reason row sits LAST, so a mid-aim disconnect cannot shift Delete under the pointer', () => {
    renderMenu(false)
    const reason = screen.getByTestId('artifact-folder-offline-reason')
    const del = screen.getByText(/^Delete…$/).closest('[aria-disabled]') as HTMLElement
    expect(del.compareDocumentPosition(reason) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  /** Exactly ONE opacity layer, asserted in both directions. `disabled` is the only
   *  source of the buttons' disabled state, so a per-button dim could never fire
   *  without this row also dimming — and nested opacity multiplies, so the two
   *  together rendered the strip at ~16%, reading as absent rather than inert. */
  it('offline colour swatches READ as inert, not merely refuse', () => {
    renderMenu(false)
    const swatches = screen.getAllByRole('radio')
    const row = swatches[0].closest('[role="radiogroup"]') as HTMLElement
    expect(row.className).toContain('opacity-40')
    for (const s of swatches) expect(s.className).not.toMatch(/opacity-\d/)
  })

  it('connected the swatch row is NOT dimmed — the control for the case above', () => {
    renderMenu(true)
    const row = screen.getAllByRole('radio')[0].closest('[role="radiogroup"]') as HTMLElement
    expect(row.className).not.toContain('opacity-40')
  })

  it('offline colour swatches fire no write', () => {
    const { actions } = renderMenu(false)
    const swatch = screen.getAllByRole('radio')[0]
    fireEvent.click(swatch)
    expect(actions.onSetColor).not.toHaveBeenCalled()
  })

  it('connected the same three still work, so the gate is not blanket', () => {
    const { actions } = renderMenu(true)
    expect(itemFor(/^Rename$/)?.getAttribute('aria-disabled')).toBe('false')
    fireEvent.click(screen.getAllByRole('radio')[0])
    expect(actions.onSetColor).toHaveBeenCalled()
    fireEvent.click(screen.getByText(/^Delete…$/))
    expect(actions.onDelete).toHaveBeenCalled()
  })

  /** Offline the Move row is not a DISABLED trigger but no trigger at all. On a
   *  coarse pointer the SubTrigger renders as a role="button" div whose click and
   *  Enter/Space handlers call onToggle unconditionally, and `disabled` lands in
   *  ...rest on a plain <div> where it means nothing — so the submenu expanded and
   *  the pick died in the caller's guard. `aria-expanded` is the tell: a trigger
   *  carries it, an inert row does not. */
  it('offline the Move row is inert to pointer AND keyboard, so no pick exists to drop', () => {
    const { actions } = renderMenu(false)
    const row = screen.getByText(/^Move to folder/).closest('[role="menuitem"]') as HTMLElement
    expect(row.getAttribute('aria-disabled')).toBe('true')
    expect(row.getAttribute('aria-expanded')).toBeNull()
    fireEvent.click(row)
    fireEvent.keyDown(row, { key: 'Enter' })
    fireEvent.keyDown(row, { key: ' ' })
    // A submenu that opened would render the root entry to pick.
    expect(screen.queryByText(/^No folder/)).not.toBeInTheDocument()
    expect(actions.onMove).not.toHaveBeenCalled()
  })

  it('offline the Move row stays inert on a coarse pointer — the touch path `disabled` never reached', () => {
    const prior = Object.getOwnPropertyDescriptor(window, 'matchMedia')
    Object.defineProperty(window, 'matchMedia', {
      configurable: true, writable: true,
      value: (q: string) => ({
        matches: q === '(pointer: coarse)', media: q,
        addEventListener: () => {}, removeEventListener: () => {},
      }),
    })
    try {
      const { actions } = renderMenu(false)
      const row = screen.getByText(/^Move to folder/).closest('[role="menuitem"]') as HTMLElement
      fireEvent.click(row)
      fireEvent.keyDown(row, { key: 'Enter' })
      expect(screen.queryByText(/^No folder/)).not.toBeInTheDocument()
      expect(actions.onMove).not.toHaveBeenCalled()
    } finally {
      if (prior) Object.defineProperty(window, 'matchMedia', prior)
      else delete (window as unknown as Record<string, unknown>).matchMedia
    }
  })

  it('connected the Move row IS a trigger — the control for the two cases above', () => {
    renderMenu(true)
    const row = screen.getByText(/^Move to folder/).closest('[role="menuitem"]') as HTMLElement
    expect(row.getAttribute('aria-disabled')).not.toBe('true')
    expect(row.getAttribute('aria-expanded')).not.toBeNull()
  })

  it('offline Rename and Delete read as refused, not clickable', () => {
    renderMenu(false)
    expect(itemFor(/^Rename$/)?.className).toContain('cursor-not-allowed')
    expect(itemFor(/^Delete…$/)?.className).toContain('cursor-not-allowed')
  })

  it('connected Rename and Delete keep the hand cursor — the control for the case above', () => {
    renderMenu(true)
    expect(itemFor(/^Rename$/)?.className).not.toContain('cursor-not-allowed')
    expect(itemFor(/^Delete…$/)?.className).not.toContain('cursor-not-allowed')
  })
})

const artifact = {
  slug: 'a1', name: 'Report', kind: 'markdown', version: 1, source: 'chat',
  updated_at: '2026-09-16T00:00:00Z', tags: [], pinned: false, folder_id: '',
} as unknown as Artifact

const doc = {
  path: 'notes.md', name: 'notes.md', session_key: 's1', session_title: 'Chat',
  updated_at: '2026-09-16T00:00:00Z', message_ts: '1', saved: false, slug: '',
} as unknown as SessionDoc

describe('artifacts row writes – a dimmed control must still say why', () => {
  /** `offlineProps` sets the offline title, so the caller's own `title` has to be
   *  declared BEFORE the spread. Declared after, an explicit `undefined` wins
   *  (JSX later-attr-wins) and a mouse user hovering an inert control gets nothing. */
  it('offline the row star and Delete keep a tooltip naming the reason', () => {
    renderWithStore(false, <table><tbody><ArtifactRow a={artifact} onOpen={vi.fn()} onDelete={vi.fn()} deletingSlug={null} onTogglePin={vi.fn()} /></tbody></table>)
    const star = screen.getByRole('button', { name: /^star artifact disabled/i })
    expect(star.getAttribute('title')).toMatch(/gateway offline/i)
    const del = screen.getByRole('button', { name: /^remove from artifacts library disabled/i })
    expect(del.getAttribute('title')).toMatch(/gateway offline/i)
  })

  it('connected the row star keeps its own tooltip, not the offline one', () => {
    renderWithStore(true, <table><tbody><ArtifactRow a={artifact} onOpen={vi.fn()} onDelete={vi.fn()} deletingSlug={null} onTogglePin={vi.fn()} /></tbody></table>)
    const star = screen.getByRole('button', { name: /^star artifact$/i })
    expect(star.getAttribute('title')).toMatch(/^star artifact$/i)
    expect(star.getAttribute('title')).not.toMatch(/gateway offline/i)
  })

  it('offline the session-document star refuses too — the third star write in this file', () => {
    const onMaterialize = vi.fn()
    renderWithStore(false, <SessionDocStar d={doc} busy={false} onMaterialize={onMaterialize} />)
    const star = screen.getByRole('button')
    expect(star.getAttribute('aria-disabled')).toBe('true')
    expect(star.getAttribute('title')).toMatch(/gateway offline/i)
    fireEvent.click(star)
    expect(onMaterialize).not.toHaveBeenCalled()
  })

  /** A dimmed control that still shows a hand cursor and lights up on hover invites
   *  the very click the dim refuses. The colour swatches already read inert through
   *  `disabled:cursor-not-allowed`; these three say the same thing on their offline
   *  branch, so the dim and the pointer agree. */
  it('offline the row star and Delete read as refused, not as clickable', () => {
    renderWithStore(false, <table><tbody><ArtifactRow a={artifact} onOpen={vi.fn()} onDelete={vi.fn()} deletingSlug={null} onTogglePin={vi.fn()} /></tbody></table>)
    const star = screen.getByRole('button', { name: /^star artifact disabled/i })
    expect(star.className).toContain('cursor-not-allowed')
    expect(star.className).not.toContain('hover:text-accent')
    const del = screen.getByRole('button', { name: /^remove from artifacts library disabled/i })
    expect(del.className).toContain('cursor-not-allowed')
    expect(del.className).not.toContain('hover:text-danger')
  })

  /** Delete carries a SECOND dim for an in-flight delete, and a delete dispatched
   *  while connected is still pending when the gateway drops — so both could apply
   *  at once and nested opacity multiplies (0.6 × 0.4 ≈ 24%). Offline keeps one
   *  layer; the control below proves the in-flight dim is not lost when connected. */
  it('offline Delete carries exactly one dim layer, not the in-flight one as well', () => {
    renderWithStore(false, <table><tbody><ArtifactRow a={artifact} onOpen={vi.fn()} onDelete={vi.fn()} deletingSlug={null} onTogglePin={vi.fn()} /></tbody></table>)
    const del = screen.getByRole('button', { name: /^remove from artifacts library disabled/i })
    expect(del.className).toContain('opacity-40')
    expect(del.className).not.toContain('disabled:opacity-60')
  })

  it('connected Delete keeps its in-flight dim — the control for the case above', () => {
    renderWithStore(true, <table><tbody><ArtifactRow a={artifact} onOpen={vi.fn()} onDelete={vi.fn()} deletingSlug={null} onTogglePin={vi.fn()} /></tbody></table>)
    const del = screen.getByRole('button', { name: /^remove from artifacts library$/i })
    expect(del.className).toContain('disabled:opacity-60')
    expect(del.className).not.toContain('opacity-40')
  })

  it('offline the star tooltip fits an already-starred row as well as an empty one', () => {
    // One verb serves both directions of the toggle, so a verb naming only
    // "star" contradicts the label of a row whose action is to un-star.
    const pinned = { ...artifact, pinned: true } as Artifact
    renderWithStore(false, <table><tbody><ArtifactRow a={pinned} onOpen={vi.fn()} onDelete={vi.fn()} deletingSlug={null} onTogglePin={vi.fn()} /></tbody></table>)
    const star = screen.getByRole('button', { name: /^remove star from artifact disabled/i })
    expect(star.getAttribute('title')).toMatch(/unstar/i)
  })

  it('connected the row star and Delete keep the hand cursor and hover colour — the control', () => {
    renderWithStore(true, <table><tbody><ArtifactRow a={artifact} onOpen={vi.fn()} onDelete={vi.fn()} deletingSlug={null} onTogglePin={vi.fn()} /></tbody></table>)
    const star = screen.getByRole('button', { name: /^star artifact$/i })
    expect(star.className).toContain('cursor-pointer')
    expect(star.className).toContain('hover:text-accent')
    const del = screen.getByRole('button', { name: /^remove from artifacts library$/i })
    expect(del.className).toContain('cursor-pointer')
    expect(del.className).toContain('hover:text-danger')
  })

  it('offline the session-document star reads as refused too', () => {
    renderWithStore(false, <SessionDocStar d={doc} busy={false} onMaterialize={vi.fn()} />)
    const star = screen.getByRole('button')
    expect(star.className).toContain('cursor-not-allowed')
    expect(star.className).not.toContain('hover:text-accent')
    expect(star.className).toContain('opacity-40')
  })

  /** `IconButton` dims itself while disabled and nested opacity multiplies, so the
   *  offline dim must stand down while busy (~12% otherwise). Both states still
   *  refuse — the cue stays, only the second opacity layer goes. */
  it('offline AND mid-materialize the star does not stack a second dim', () => {
    renderWithStore(false, <SessionDocStar d={doc} busy={true} onMaterialize={vi.fn()} />)
    const star = screen.getByRole('button')
    expect(star.className).toContain('cursor-not-allowed')
    expect(star.className).not.toContain('opacity-40')
    expect(star.className).toContain('disabled:opacity-30')
  })

  it('connected the session-document star materializes — the control for the case above', () => {
    const onMaterialize = vi.fn()
    renderWithStore(true, <SessionDocStar d={doc} busy={false} onMaterialize={onMaterialize} />)
    fireEvent.click(screen.getByRole('button'))
    expect(onMaterialize).toHaveBeenCalledWith('notes.md', 's1')
  })
})

/**
 * A dimmed row control states its reason in a `title`, and a coarse pointer never
 * opens one — so on touch the whole explanation is lost and only dead controls
 * remain. The folder menu answers that with a standing row; the table needs its
 * own, because the row controls live outside any menu.
 */
describe('artifacts table – the offline reason must reach a pointer that cannot hover', () => {
  const tableProps = {
    items: [artifact], sort: null, onSort: vi.fn(), onOpen: vi.fn(), onDelete: vi.fn(),
    deletingSlug: null, onTogglePin: vi.fn(), pinningSlug: null,
  }
  const treeProps = {
    ...tableProps, folders: [folder], expandedIds: new Set<string>(), onToggleExpand: vi.fn(),
    folderActions: { onRename: vi.fn(), onDelete: vi.fn(), onMove: vi.fn(), onSetColor: vi.fn() } as never,
    overFolderId: null, dragActive: false,
  }

  it('offline the flat table states the reason without needing a hover', () => {
    renderWithStore(false, <LibraryTable {...tableProps} />)
    const row = screen.getByTestId('artifact-table-offline-reason')
    expect(row.getAttribute('role')).toBe('status')
    expect(row.textContent).toMatch(/gateway offline/i)
  })

  /** Arriving ABOVE the rows on disconnect shifts one under a mid-aim pointer, and
   *  here that misses the dimmed control and opens the artifact instead — the same
   *  hazard that puts the folder menu's row last. */
  it('the flat table renders the reason AFTER its rows, so mounting shifts none of them', () => {
    const { utils } = renderWithStore(false, <LibraryTable {...tableProps} />)
    const table = utils.container.querySelector('table')!
    const row = screen.getByTestId('artifact-table-offline-reason')
    expect(table.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('connected the flat table shows no reason row — the control', () => {
    renderWithStore(true, <LibraryTable {...tableProps} />)
    expect(screen.queryByTestId('artifact-table-offline-reason')).toBeNull()
  })

  it('offline the folder tree states the reason too', () => {
    renderWithStore(false, <LibraryTree {...treeProps} />)
    expect(screen.getByTestId('artifact-table-offline-reason').textContent).toMatch(/gateway offline/i)
  })

  it('the folder tree renders the reason AFTER its rows as well', () => {
    const { utils } = renderWithStore(false, <LibraryTree {...treeProps} />)
    const table = utils.container.querySelector('table')!
    const row = screen.getByTestId('artifact-table-offline-reason')
    expect(table.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('connected the folder tree shows no reason row — the control', () => {
    renderWithStore(true, <LibraryTree {...treeProps} />)
    expect(screen.queryByTestId('artifact-table-offline-reason')).toBeNull()
  })

  /** Last-in-DOM keeps the rows from shifting, but in a long library it also puts
   *  the only visible explanation below the fold — where a touch user, who has no
   *  tooltip either, never meets it. Sticky keeps both properties: the row travels
   *  up to the scrollport's bottom edge and settles at the region's end. The
   *  background must be opaque, since while travelling it covers `bg-card` rows. */
  it.each([
    ['flat table', <LibraryTable {...tableProps} key="t" />],
    ['folder tree', <LibraryTree {...treeProps} key="r" />],
  ])('offline the %s pins the reason on screen, opaquely', (_label, element) => {
    renderWithStore(false, element)
    const cls = screen.getByTestId('artifact-table-offline-reason').className
    expect(cls).toMatch(/\bsticky\b/)
    expect(cls).toMatch(/\bbottom-0\b/)
    expect(cls).toMatch(/\bbg-bg\b/)
  })

  /** The row is shared with the folder menu and the held-draft note, neither of
   *  which scrolls a long region — pinning them would park text over a menu item
   *  and over the field's own control. So the pin belongs to the host, and the
   *  component must not carry it by default. */
  it('the folder menu keeps its row unpinned — the shared-component control', () => {
    renderMenu(false)
    expect(screen.getByTestId('artifact-folder-offline-reason').className).not.toMatch(/\bsticky\b/)
  })
})

/**
 * Gating the control that OPENS a name editor covers only the open. The gateway
 * can drop while the editor is up, and the commit closes the editor before
 * mutating — so an ungated commit hands the typed name to a write that cannot
 * land and the name goes with the field. This input owns the draft, so it is
 * where the commit has to check.
 */
describe('FolderNameInput – an offline commit must not consume the draft', () => {
  const renderInput = (connected: boolean) => {
    const onCommit = vi.fn()
    const onCancel = vi.fn()
    renderWithStore(connected, <FolderNameInput initial="Reports" placeholder="Rename folder" onCommit={onCommit} onCancel={onCancel} />)
    // The sole textbox, and its accessible name is now the same in both states.
    const field = screen.getByRole('textbox') as HTMLInputElement
    return { field, onCommit, onCancel }
  }

  it('offline the commit is refused and the typed name stays in the field', () => {
    const { field, onCommit, onCancel } = renderInput(false)
    fireEvent.change(field, { target: { value: 'Quarterly reports' } })
    fireEvent.blur(field)
    expect(onCommit).not.toHaveBeenCalled()
    expect(onCancel).not.toHaveBeenCalled()
    expect(field.value).toBe('Quarterly reports')
  })

  /** A hold the user cannot see is the silent refusal this gating exists to end, and
   *  a `title` does not carry it: a coarse pointer opens no tooltip, and the gallery
   *  this field also serves has no surface-wide reason row. So the note is VISIBLE.
   *
   *  The wording is pinned too, in both directions. The draft lives only in this
   *  mounted input, so copy promising the name is kept would over-promise: closing
   *  the host discards it. The note must ask for the field to be kept open, and must
   *  NOT claim the name is already saved or retained.
   *
   *  The field is refused but still INTERACTIVE, so unlike the dimmed controls it must
   *  not use `offlineProps`: an `aria-disabled` and a "… disabled" name would tell an
   *  AT user the field is dead and invite them to abandon the very draft it is holding.
   *  It keeps its own name and points `aria-describedby` at the note instead. */
  it('offline the field says why nothing was saved, visibly and not only on hover', () => {
    renderWithStore(false, <FolderNameInput initial="Reports" placeholder="Rename folder" onCommit={vi.fn()} onCancel={vi.fn()} />)
    const note = screen.getByTestId('folder-name-offline-held')
    expect(note.getAttribute('role')).toBe('status')
    expect(note.textContent).toMatch(/not saved/i)
    expect(note.textContent).toMatch(/keep this field open/i)
    expect(note.textContent).not.toMatch(/\bkept\b|\bsaved until\b|\bretained\b/i)
    const field = screen.getByRole('textbox')
    expect(field.getAttribute('title')).toMatch(/gateway offline/i)
    expect(field.getAttribute('aria-disabled')).toBeNull()
    expect(field.getAttribute('aria-label')).toBe('Rename folder')
    expect(note.getAttribute('id')).toBeTruthy()
    expect(field.getAttribute('aria-describedby')).toBe(note.getAttribute('id'))
  })

  it('connected the field carries no offline reason, visible or hovered — the control', () => {
    const { field } = renderInput(true)
    expect(screen.queryByTestId('folder-name-offline-held')).toBeNull()
    expect(field.getAttribute('title')).toBeNull()
    expect(field.getAttribute('aria-describedby')).toBeNull()
    expect(field.getAttribute('aria-label')).toBe('Rename folder')
  })

  it('connected the same edit commits — the control for the case above', () => {
    const { field, onCommit } = renderInput(true)
    fireEvent.change(field, { target: { value: 'Quarterly reports' } })
    fireEvent.blur(field)
    expect(onCommit).toHaveBeenCalledWith('Quarterly reports')
  })

  it('offline Escape still cancels — refusing a write must not trap the user in the editor', () => {
    const { field, onCommit, onCancel } = renderInput(false)
    fireEvent.change(field, { target: { value: 'Quarterly reports' } })
    fireEvent.keyDown(field, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalled()
    expect(onCommit).not.toHaveBeenCalled()
  })
})
