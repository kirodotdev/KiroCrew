import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import FolderConfigModal from '../components/FolderConfigModal'
import { api } from '../api/client'
import { render, folder, AGENTS, open } from './folderConfigModal.testHelpers'

// Split out of FolderConfigModal.test.tsx (file-line-cap) — see
// folderConfigModal.testHelpers.tsx for the shared render/open/fixtures this
// file and its siblings all use.
//
// vi.mock factories are hoisted above all imports, so the factory body is
// inlined here rather than imported from the helper module.
vi.mock('../api/client', () => ({
  api: {
    recentProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    favoriteProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    browseDirs: vi.fn().mockResolvedValue({ path: '/', parent: '', dirs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

beforeEach(() => {
  vi.clearAllMocks()
  // A test that sets a persistent roster (mockResolvedValue / mockImplementation
  // on kirocrewAgents) would otherwise leak it into the next test. Re-assert
  // the default empty roster each time; this overrides a leaked implementation
  // without mockReset()'ing the other api.* factory mocks the suite relies on.
  ;(api.kirocrewAgents as unknown as ReturnType<typeof vi.fn>)
    .mockResolvedValue({ agents: [], default_agent: '' })
})

describe('tag picker', () => {
  const TAGS = [
    { id: 't1', name: 'Payments', color: '#ef4444', order: 0 },
    { id: 't2', name: 'Urgent', color: '#3b82f6', order: 1 },
  ]

  // Chips are labels wrapping a visually-hidden checkbox (AUTOSDE
  // max-two-buttons-per-row: a multi-select is not an action row).
  const tagCheckbox = (id: string) =>
    within(screen.getByTestId(`folder-config-tag-${id}`)).getByRole('checkbox')

  it('renders a loading placeholder while the vocabulary is unresolved', () => {
    // UX round-4 chose `undefined` = unresolved so the onboarding hint
    // cannot lie to a user who HAS tags; this round replaces the bare
    // nothing with a muted placeholder under the section heading, so a
    // failed `chat-tags` query no longer silently erases the feature and
    // the section resolving after open does not shift the layout.
    open()
    expect(screen.queryByTestId('folder-config-tags')).toBeNull()
    expect(screen.queryByTestId('folder-config-tags-empty')).toBeNull()
    expect(screen.getByTestId('folder-config-tags-loading')).toBeTruthy()
    expect(screen.queryByTestId('folder-config-tags-error')).toBeNull()
  })

  it('renders an error line, not the loading hint, when the vocabulary query failed', () => {
    // UX round-5: a dead query must not assert an in-progress state
    // indefinitely — "Loading tags…" on a failed fetch misstates what
    // happened and offers no way out.
    open({ availableTagsFailed: true })
    expect(screen.queryByTestId('folder-config-tags-loading')).toBeNull()
    expect(screen.getByTestId('folder-config-tags-error')).toBeTruthy()
  })

  it('recovers a failed vocabulary in place via the inline Retry — no modal dismissal', () => {
    // UX round-7: the previous "close and reopen to retry" copy told users
    // to destroy their own draft (Escape is dismiss-guarded while dirty;
    // X discards typed input). The retry must happen INSIDE the modal.
    const onRetryTags = vi.fn()
    open({ availableTagsFailed: true, onRetryTags })
    const retry = screen.getByTestId('folder-config-tags-retry')
    fireEvent.click(retry)
    expect(onRetryTags).toHaveBeenCalledTimes(1)
    // The modal stayed open — the form is still there.
    expect(screen.getByTestId('folder-config-name')).toBeTruthy()
  })

  it('shows the onboarding hint when the vocabulary is empty', () => {
    open({ availableTags: [] })
    expect(screen.queryByTestId('folder-config-tags')).toBeNull()
    expect(screen.getByTestId('folder-config-tags-empty')).toBeTruthy()
  })

  it('a dangling persisted tag id is filtered out of the seed', () => {
    // A failed best-effort folder strip during tag deletion can leave a
    // deleted id on the folder. Seeding it into the draft would make every
    // save 400 (tags_invalid) — the folder becomes permanently uneditable
    // (GPT round-1 blocker). The seed keeps only ids the vocabulary knows.
    const f = folder('f1', { name: 'Payments', tags: ['gone', 't1'] })
    const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f], availableTags: TAGS })
    expect(tagCheckbox('t1')).toBeChecked()
    // Touch the selection: the submitted list must not carry the dangler.
    fireEvent.click(tagCheckbox('t2'))
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      tags: ['t1', 't2'], touched: expect.arrayContaining(['tags']),
    }))
  })

  it('a dangling id alone does not mark tags as touched', async () => {
    // Renaming a folder that carries a dangler must succeed: the seed and the
    // draft agree (both filtered), so `tags` stays out of touched and the
    // PATCH never sends the stale reference that would 400.
    const f = folder('f1', { name: 'Payments', tags: ['gone'] })
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(
      <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
        installedAgents={AGENTS} availableTags={TAGS} onClose={vi.fn()} onSubmit={onSubmit} />
    )
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Renamed' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].touched).not.toContain('tags')
  })

  it('a cold load cannot delete existing tags (unknown vocabulary keeps the seed)', async () => {
    // GPT round-2 blocker: the tags query is unresolved when the modal opens
    // (availableTags === undefined), so filtering against it as an EMPTY
    // vocabulary would seed [], and a save after the query resolves would
    // silently delete the folder's existing tags. Unknown vocabulary must
    // preserve the persisted ids.
    const f = folder('f1', { name: 'Payments', tags: ['t1'] })
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(
      <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
        installedAgents={AGENTS} availableTags={undefined} onClose={vi.fn()} onSubmit={onSubmit} />
    )
    // The vocabulary resolves while the modal is open; chips render.
    rerender(
      <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
        installedAgents={AGENTS} availableTags={TAGS} onClose={vi.fn()} onSubmit={onSubmit} />
    )
    expect(tagCheckbox('t1')).toBeChecked()
    // The user toggles ANOTHER tag — t1 must survive the save.
    fireEvent.click(tagCheckbox('t2'))
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].tags.sort()).toEqual(['t1', 't2'])
  })

  it('submits the draft list as-is — dangling-id filtering is the SERVER\'s job', async () => {
    // The folder endpoint silently filters unknown ids exactly like the
    // slot-tags endpoint it mirrors (FP round-4 subtraction), so the modal
    // ships no client-side prune: a dangler that survived into the draft
    // under an unknown-vocabulary seed is shed by the server on save and
    // can never 400 the folder.
    const f = folder('f1', { name: 'Payments', tags: ['gone', 't1'] })
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(
      <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
        installedAgents={AGENTS} availableTags={undefined} onClose={vi.fn()} onSubmit={onSubmit} />
    )
    rerender(
      <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
        installedAgents={AGENTS} availableTags={TAGS} onClose={vi.fn()} onSubmit={onSubmit} />
    )
    fireEvent.click(tagCheckbox('t2'))
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    // The raw draft goes through — 'gone' included; the server drops it.
    expect(onSubmit.mock.calls[0][0].tags.sort()).toEqual(['gone', 't1', 't2'])
    expect(onSubmit.mock.calls[0][0].touched).toContain('tags')
  })

  it('a rename-only save never sends tags, even when the seed carries a dangler', async () => {
    // GPT round-3 blocker: pruning must piggyback on a genuine tag edit,
    // never on a rename. A rename that sent a "corrected" tag list would
    // overwrite tags another client added to the folder while this modal
    // sat open — vocabulary pruning is not a user edit.
    const f = folder('f1', { name: 'Payments', tags: ['gone', 't1'] })
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(
      <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
        installedAgents={AGENTS} availableTags={undefined} onClose={vi.fn()} onSubmit={onSubmit} />
    )
    // Vocabulary resolves while the modal is open; the seed kept raw ids.
    rerender(
      <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
        installedAgents={AGENTS} availableTags={TAGS} onClose={vi.fn()} onSubmit={onSubmit} />
    )
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Renamed' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    const call = onSubmit.mock.calls[0][0]
    expect(call.touched).not.toContain('tags')
    // The payload list is the untouched draft — no pruned "correction" that
    // the backend could mistake for an intentional replacement.
    expect(call.tags.sort()).toEqual(['gone', 't1'])
  })

  it('marks selected chips with a check glyph, not just a ring', () => {
    // UX round-2: selection must not hinge on a 1px ring-width difference
    // from the same-colored keyboard-focus ring. Same glyph SlotTagPopover
    // uses for the same "tag is on" state.
    const f = folder('f1', { name: 'Payments', tags: ['t1'] })
    open({ mode: 'edit', folder: f, folders: [f], availableTags: TAGS })
    expect(screen.getByTestId('folder-config-tag-t1').querySelector('svg')).toBeTruthy()
    expect(screen.getByTestId('folder-config-tag-t2').querySelector('svg.lucide-check')).toBeNull()
  })

  it('renders a chip for every tag in the vocabulary', () => {
    open({ availableTags: TAGS })
    const picker = screen.getByTestId('folder-config-tags')
    expect(picker).toBeTruthy()
    expect(screen.getByTestId('folder-config-tag-t1')).toHaveTextContent('Payments')
    expect(screen.getByTestId('folder-config-tag-t2')).toHaveTextContent('Urgent')
  })

  it('pre-selects the folder’s existing tags in edit mode', () => {
    const f = folder('f1', { name: 'Payments', tags: ['t2'] })
    open({ mode: 'edit', folder: f, folders: [f], availableTags: TAGS })
    expect(tagCheckbox('t2')).toBeChecked()
    expect(tagCheckbox('t1')).not.toBeChecked()
  })

  it('toggling a chip updates the draft and submits the selected ids', () => {
    const { onSubmit } = open({ availableTags: TAGS })
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Tagged' } })
    fireEvent.click(tagCheckbox('t1'))
    fireEvent.click(tagCheckbox('t2'))
    // Second click on t1 removes it — toggle, not add-only.
    fireEvent.click(tagCheckbox('t1'))
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      tags: ['t2'], touched: expect.arrayContaining(['tags']),
    }))
  })

  it('reports tags in touched only when the selection changed', async () => {
    const f = folder('f1', { name: 'Payments', tags: ['t1'] })
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(
      <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
        installedAgents={AGENTS} availableTags={TAGS} onClose={vi.fn()} onSubmit={onSubmit} />
    )
    // Open and save without touching the selection.
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].touched).not.toContain('tags')
  })

  it('a tag-only change arms the dismiss guard', () => {
    const { onClose } = open({ availableTags: TAGS })
    fireEvent.click(tagCheckbox('t1'))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('deselecting back to the original set is not a change', () => {
    const f = folder('f1', { name: 'Payments', tags: ['t1'] })
    const { onClose } = open({ mode: 'edit', folder: f, folders: [f], availableTags: TAGS })
    // Add t2 then remove it — draft equals the seed again.
    fireEvent.click(tagCheckbox('t2'))
    fireEvent.click(tagCheckbox('t2'))
    fireEvent.keyDown(window, { key: 'Escape' })
    // Order-insensitive set equality means Escape still dismisses cleanly.
    expect(onClose).toHaveBeenCalled()
  })
})
