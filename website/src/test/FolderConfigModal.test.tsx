import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import FolderConfigModal from '../components/FolderConfigModal'
import { ApiError } from '../api/apiError'
import { api } from '../api/client'
import { render, folder, AGENTS, open, agentTrigger, openAgentPopup, openAgents, pickAgent } from './folderConfigModal.testHelpers'

// Split out of FolderConfigModal.test.tsx (file-line-cap) — see
// folderConfigModal.testHelpers.tsx for the shared render/open/fixtures this
// file and its siblings (FolderConfigModal.tags.test.tsx,
// FolderConfigModal.steering.test.tsx, FolderConfigModal.roster.test.tsx,
// FolderConfigModal.footer.test.tsx) all use.
//
// vi.mock factories are hoisted above all imports, so the factory body is
// inlined here rather than imported from the helper module (an imported
// reference inside vi.mock throws "Cannot access '<binding>' before
// initialization").
vi.mock('../api/client', () => ({
  api: {
    recentProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    favoriteProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    browseDirs: vi.fn().mockResolvedValue({ path: '/', parent: '', dirs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

describe('FolderConfigModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // A test that sets a persistent roster (mockResolvedValue / mockImplementation
    // on kirocrewAgents) would otherwise leak it into the next test. Re-assert
    // the default empty roster each time; this overrides a leaked implementation
    // without mockReset()'ing the other api.* factory mocks the suite relies on.
    ;(api.kirocrewAgents as unknown as ReturnType<typeof vi.fn>)
      .mockResolvedValue({ agents: [], default_agent: '' })
  })

  it('offers no parent-folder input — the destination is fixed by the entry point', () => {
    open({ parentId: '' })
    // A picker for the parent would let the user contradict where they clicked.
    // The only dropdown in the modal is the agent picker.
    expect(document.querySelectorAll('[aria-haspopup="listbox"]')).toHaveLength(1)
    expect(agentTrigger()).toBeTruthy()
  })

  it('restates the destination as a read-only breadcrumb', () => {
    const folders = [folder('a', { name: 'Kiro' }), folder('b', { name: 'Backend', parent_id: 'a' })]
    open({ folders, parentId: 'b' })
    const dest = screen.getByTestId('folder-config-destination')
    expect(dest.textContent).toContain('Kiro')
    expect(dest.textContent).toContain('Backend')
    // Read-only: no editable control inside it.
    expect(dest.querySelector('input,select,button')).toBeNull()
  })

  it('blocks submit until a name is entered', () => {
    const { onSubmit } = open()
    const submit = screen.getByTestId('folder-config-submit') as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
    expect(submit.disabled).toBe(false)
    fireEvent.click(submit)
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ name: 'Payments' }))
  })

  it('trims the name before submitting', () => {
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: '  Spaced  ' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ name: 'Spaced' }))
  })

  it('treats a whitespace-only name as empty', () => {
    open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: '   ' } })
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
  })

  it('submits with no color by default', () => {
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Plain' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ color: '' }))
  })

  it('picks a color from the palette', () => {
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Redteam' } })
    // The palette is always visible — no trigger to click first.
    fireEvent.click(screen.getByLabelText('Set color to Red'))
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ color: '#ef4444' }))
  })

  it('renders the icon controls with the default glyph preview', () => {
    open()
    // Preview shows the default folder glyph until an emoji is typed; the
    // input is empty (empty = keep the default glyph, no generation).
    expect(screen.getByTestId('folder-config-icon-preview')).toBeTruthy()
    expect((screen.getByTestId('folder-config-icon') as HTMLInputElement).value).toBe('')
    // Auto-generate is an edit-mode affordance; the create modal has no
    // generation path — an empty icon keeps the default glyph.
    expect(screen.queryByTestId('folder-config-icon-regenerate')).toBeNull()
    expect(screen.getByTestId('folder-config-color-reset')).toBeTruthy()
  })

  it('submits a typed emoji as the icon', () => {
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Rockets' } })
    fireEvent.change(screen.getByTestId('folder-config-icon'), { target: { value: '🚀' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ icon: '🚀', regenerateIcon: false }))
  })

  it('previews the typed emoji in place of the default glyph', () => {
    open()
    fireEvent.change(screen.getByTestId('folder-config-icon'), { target: { value: '🚀' } })
    expect(screen.getByTestId('folder-config-icon-preview').textContent).toBe('🚀')
  })




  it('flags a seeded orphan agent but keeps Save enabled so it round-trips verbatim', async () => {
    // A folder saved with an agent that is not in the roster (uninstalled or
    // renamed since save) stays SELECTABLE and flagged so the user sees why —
    // but Save is NOT blocked: the folder's OWN saved agent round-trips a benign
    // rename/recolor (base #1182), and run time fails loud if it is genuinely
    // gone. Blocking is for a pick that goes out of scope IN SESSION, covered by
    // 'flags the picked agent as (needs a project directory), ...' below and, for a re-scoped
    // edit, by 'blocks Save when an EDIT folder is re-scoped to a dir lacking its
    // SAVED agent' in the project-scoped-agent-roster describe.
    const f = folder('f1', { name: 'Payments', default_agent: 'retired-agent' })
    const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
    expect(agentTrigger()).toHaveTextContent(/retired-agent.*not installed/i)
    // It is a real, flagged row in the popup, right after the inherit/None row.
    expect(await openAgents()).toEqual(['None', 'retired-agent (not installed)', 'kirocrew', 'kirocrew-dev'])
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('option')).toBeNull())
    // Save is ALLOWED — the saved orphan round-trips verbatim.
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ defaultAgent: 'retired-agent' }))
  })

  it('does not flag an installed agent as uninstalled', async () => {
    const f = folder('f1', { name: 'Payments', default_agent: 'kirocrew-dev' })
    open({ mode: 'edit', folder: f, folders: [f] })
    expect(agentTrigger()).toHaveTextContent('kirocrew-dev')
    // Open the popup before the negative assertion: the rows only exist while
    // it is up, so asserting the absence of the flag on a closed picker would
    // pass vacuously.
    expect(await openAgents()).toEqual(['None', 'kirocrew', 'kirocrew-dev'])
    expect(screen.queryByText(/not installed/i)).toBeNull()
  })

  it('excludes the active slot project agents from a no-directory folder roster', async () => {
    // GPT F2 (FolderConfigModal.tsx:280): `installedAgents` is the ACTIVE CHAT
    // SLOT's roster — it includes that slot's `scope:'project'` agents, which
    // belong to a DIFFERENT project than the folder being configured. With no
    // effective directory (create mode, no dir typed, no inherited dir) the
    // fallback roster must offer ONLY the global rows: offering a foreign
    // project-scoped agent lets it be saved as the folder default, and every
    // chat opened under that dir-less folder then fails "unavailable" at
    // dispatch because the agent does not resolve outside its own project.
    open({
      installedAgents: [
        { name: 'kirocrew', scope: 'global' },
        { name: 'kirocrew-dev', scope: 'global' },
        { name: 'slot-only-agent', scope: 'project' },
      ],
    })
    // Only global agents appear; the active slot's project-scoped agent is gone.
    expect(await openAgents()).toEqual(['None', 'kirocrew', 'kirocrew-dev'])
  })

  describe('orphan agent notice', () => {
    // Item 1: a disabled/misconfigured control that does not say why IS the
    // defect. An orphan selection is round-tripped (Save is NOT blocked — that
    // would let a folder rename wipe a temporarily-uninstalled agent), so the
    // notice explains why the SELECTED AGENT will not run and is bound to the
    // control with aria-describedby so a screen reader reaches it.

    it('shows a field-bound notice while an orphan agent is selected', () => {
      const f = folder('f1', { name: 'Payments', default_agent: 'retired-agent' })
      open({ mode: 'edit', folder: f, folders: [f] })
      const notice = screen.getByTestId('folder-config-agent-notice')
      expect(notice.textContent).toMatch(/isn.t installed/i)
      // The reason is programmatically associated with the control, not merely
      // placed near it: the combobox's aria-describedby names the notice's id.
      expect(agentTrigger().getAttribute('aria-describedby')).toBe(notice.id)
    })

    it('does not disable Save for an orphan — the orphan round-trips instead', () => {
      const f = folder('f1', { name: 'Payments', default_agent: 'retired-agent' })
      open({ mode: 'edit', folder: f, folders: [f] })
      // A folder whose agent is temporarily uninstalled must still be renamable.
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
      // Save still works, so the notice keeps the warn tone rather than the
      // error surface reserved for a blocked Save.
      expect(screen.getByTestId('folder-config-agent-notice')).not.toHaveAttribute('role', 'alert')
    })

    it('shows the hint and no notice, unassociated, when the agent is installed', () => {
      const f = folder('f1', { name: 'Payments', default_agent: 'kirocrew-dev' })
      open({ mode: 'edit', folder: f, folders: [f] })
      expect(screen.queryByTestId('folder-config-agent-notice')).toBeNull()
      expect(agentTrigger().getAttribute('aria-describedby')).toBeNull()
      expect(screen.getByText(/Pre-selected for new chats/i)).toBeTruthy()
    })
  })

  describe('dir-less orphan wording (UX 9396d3a07bc3)', () => {
    // A project agent picked under a directory, then the directory cleared: the
    // flag must NOT read "(not in this project)" — no project is set, so that
    // names a scope that does not exist — but a distinct dir-less "(not
    // available)" variant, with a notice that stays coherent without a dir. The
    // /repo/a scope has a project-only agent; every other scope (and the empty
    // dir's global fallback) does not.
    const roster = (dir?: string) =>
      Promise.resolve({
        agents: dir === '/repo/a'
          ? [{ name: 'repo-dev' }, { name: 'kirocrew' }, { name: 'kirocrew-dev' }]
          : [{ name: 'kirocrew' }, { name: 'kirocrew-dev' }],
        default_agent: '',
      })
    const globalOnly = [
      { name: 'kirocrew', scope: 'global' as const },
      { name: 'kirocrew-dev', scope: 'global' as const },
    ]

    it('flags the picked agent as (needs a project directory), not (not in this project), once the dir is cleared — and keeps that distinct from (not installed)', async () => {
      ;(api.kirocrewAgents as unknown as ReturnType<typeof vi.fn>)
        .mockImplementation((_s: unknown, dir?: string) => roster(dir))
      open({ installedAgents: globalOnly })
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      const dir = screen.getByTestId('folder-config-project-dir')
      // Scope to /repo/a and pick its project-only agent (valid there).
      fireEvent.change(dir, { target: { value: '/repo/a' } })
      await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      await pickAgent('repo-dev')
      expect(agentTrigger()).toHaveTextContent('repo-dev')
      // Clear the directory: the folder now pins no project at all.
      fireEvent.change(dir, { target: { value: '' } })
      // The flag settles to the dir-less variant, never the in-a-project one.
      // UX Review: this wording must now match the inherited variant's "needs a
      // project directory" — the same cause, one name — rather than the old
      // "(not available)".
      await waitFor(() =>
        expect(agentTrigger()).toHaveTextContent(/repo-dev.*\(needs a project directory\)/i))
      expect(agentTrigger()).not.toHaveTextContent(/not in this project/i)
      // The two causes must stay distinguishable: a genuinely-uninstalled saved
      // agent still reads "(not installed)", never the dir-less wording above. A
      // test that only asserted the dir-less string would pass even if the two
      // causes had been collapsed into one label.
      expect(agentTrigger()).not.toHaveTextContent(/not installed/i)
      // The accompanying notice is coherent with no dir set — it must not
      // assert "this directory's project agents" when there is no directory.
      const notice = screen.getByTestId('folder-config-agent-notice')
      expect(notice.textContent).toMatch(/without a project directory/i)
      // Behaviour unchanged: this fresh, out-of-scope pick still blocks Save.
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
    })

    it('keeps a genuinely uninstalled saved agent reading (not installed), distinct from the dir-less (needs a project directory) wording', async () => {
      // Same cause-pair as above, from the other side: a saved orphan that was
      // never re-scoped in session (seed-equal, at its seeded dir) keeps "(not
      // installed)" — collapsing the two wordings would make this row
      // indistinguishable from the dir-less case tested above.
      const f = folder('f1', { name: 'Payments', default_agent: 'retired-agent' })
      open({ mode: 'edit', folder: f, folders: [f] })
      await waitFor(() => expect(agentTrigger()).toHaveTextContent(/retired-agent.*\(not installed\)/i))
      expect(agentTrigger()).not.toHaveTextContent(/needs a project directory/i)
    })

    it('still flags a re-scoped agent as (not in this project) when a different dir IS set', async () => {
      ;(api.kirocrewAgents as unknown as ReturnType<typeof vi.fn>)
        .mockImplementation((_s: unknown, dir?: string) => roster(dir))
      open({ installedAgents: globalOnly })
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      const dir = screen.getByTestId('folder-config-project-dir')
      fireEvent.change(dir, { target: { value: '/repo/a' } })
      await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      await pickAgent('repo-dev')
      // Re-scope to a DIFFERENT directory that lacks repo-dev: a project IS set,
      // so the in-a-project wording must still apply (unchanged by this fix).
      fireEvent.change(dir, { target: { value: '/repo/b' } })
      await waitFor(() =>
        expect(agentTrigger()).toHaveTextContent(/repo-dev.*\(not in this project\)/i))
      expect(agentTrigger()).not.toHaveTextContent(/needs a project directory/i)
    })
  })

  describe('partial-path flag flicker (UX CONCERN 5633240977)', () => {
    // Pausing mid-typing at a path PREFIX that is itself a real, scannable
    // directory lets that prefix's scan SETTLE — `rosterInFlight` correctly
    // goes false, because the fetch for the prefix genuinely finished. A
    // settled prefix roster is indistinguishable from a settled destination
    // roster, so without this fix the orphan flag and the blocking notice
    // fire truthfully against the wrong (prefix) directory while the user is
    // still mid-keystroke toward the real one.
    const roster = (dir?: string) =>
      Promise.resolve({
        // The prefix `/repo` is itself a real directory whose roster lacks
        // `repo-dev`; only the FULL destination `/repo/project` has it.
        agents: dir === '/repo/project'
          ? [{ name: 'repo-dev' }, { name: 'kirocrew' }]
          : [{ name: 'kirocrew' }],
        default_agent: '',
      })

    it('does not flag the agent against a settled PREFIX directory while still typing toward the real one', async () => {
      ;(api.kirocrewAgents as unknown as ReturnType<typeof vi.fn>)
        .mockImplementation((_s: unknown, dir?: string) => roster(dir))
      // Seed an edit folder whose saved agent (repo-dev) is valid at its own
      // saved directory, so the field starts in a settled, unflagged state.
      const f = folder('f1', { name: 'Payments', project_dir: '/repo/project', default_agent: 'repo-dev' })
      open({ mode: 'edit', folder: f, folders: [f] })
      await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledWith(undefined, '/repo/project'))
      expect(screen.queryByTestId('folder-config-agent-notice')).toBeNull()

      const dir = screen.getByTestId('folder-config-project-dir')
      // Simulate the user re-typing the same path from scratch, pausing at the
      // PREFIX `/repo` — a real directory whose roster lacks repo-dev — before
      // continuing on to `/repo/project`. Focus first: the fix gates on the
      // field being actively edited, not merely on the debounce window.
      fireEvent.focus(dir)
      fireEvent.change(dir, { target: { value: '/repo' } })
      // Let the prefix's scan fully settle — this is the crux of the bug: the
      // debounce gap closes AND the fetch resolves, so `rosterInFlight` goes
      // false while the user is still mid-edit.
      await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledWith(undefined, '/repo'))
      await waitFor(() => expect(screen.queryByTestId('folder-config-agent-roster-loading')).not.toBeInTheDocument())

      // The flag and the blocking notice must NOT fire against this settled
      // prefix — the user has not said they are done editing the field.
      expect(agentTrigger()).not.toHaveTextContent(/not in this project/i)
      expect(screen.queryByTestId('folder-config-agent-notice')).toBeNull()
      // Save stays BLOCKED regardless: only the user-visible ALARM (the
      // trigger label and the notice, asserted above) defers while the field
      // is active — the settled prefix roster genuinely lacks repo-dev, and
      // that fact still gates Save even though nothing on screen says so yet
      // (the dimmed "Enter to submit" hint is the field's only cue here).
      // Deferring this too was the regression an Enter keystroke could exploit
      // mid-typing: canSubmit is captured at call time, so a silently-enabled
      // Save would let Enter commit an agent absent from the typed directory
      // in the same tick the field commits.
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)

      // Finish typing to the real destination and commit by blurring — now the
      // flag is free to reflect whatever the FINAL directory's roster says
      // (here, back to the agent's own valid home, so it stays clear).
      fireEvent.change(dir, { target: { value: '/repo/project' } })
      fireEvent.blur(dir)
      await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledWith(undefined, '/repo/project'))
      await waitFor(() => expect(screen.queryByTestId('folder-config-agent-roster-loading')).not.toBeInTheDocument())
      expect(agentTrigger()).not.toHaveTextContent(/not in this project/i)
      expect(screen.queryByTestId('folder-config-agent-notice')).toBeNull()
    })

    it('flags truthfully once the field is committed (blur) against a settled directory that really lacks the agent', async () => {
      // The deferral must not become a permanent suppression: once the field
      // is committed (blur) and the roster for THAT directory has settled,
      // a genuine orphan still flags and blocks Save.
      ;(api.kirocrewAgents as unknown as ReturnType<typeof vi.fn>)
        .mockImplementation((_s: unknown, dir?: string) => roster(dir))
      const f = folder('f1', { name: 'Payments', project_dir: '/repo/project', default_agent: 'repo-dev' })
      open({ mode: 'edit', folder: f, folders: [f] })
      await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledWith(undefined, '/repo/project'))

      const dir = screen.getByTestId('folder-config-project-dir')
      fireEvent.focus(dir)
      fireEvent.change(dir, { target: { value: '/repo' } })
      fireEvent.blur(dir)
      await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledWith(undefined, '/repo'))
      await waitFor(() =>
        expect(agentTrigger()).toHaveTextContent(/repo-dev.*\(not in this project\)/i))
      expect(screen.getByTestId('folder-config-agent-notice')).toBeTruthy()
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
    })

    it('does not let Enter mid-typing submit an agent absent from the typed dir', async () => {
      ;(api.kirocrewAgents as unknown as ReturnType<typeof vi.fn>)
        .mockImplementation((_s: unknown, dir?: string) => roster(dir))
      const f = folder('f1', { name: 'Payments', project_dir: '/repo/a', default_agent: 'repo-dev' })
      const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
      await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      const dir = screen.getByTestId('folder-config-project-dir')
      // Type a directory whose roster LACKS repo-dev, let it settle, stay focused.
      fireEvent.focus(dir)
      fireEvent.change(dir, { target: { value: '/repo' } })
      await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalledWith(undefined, '/repo'))
      await waitFor(() =>
        expect(screen.queryByTestId('folder-config-agent-roster-loading')).not.toBeInTheDocument())
      // Enter commits the field AND calls submit() in the same tick — canSubmit
      // is captured from the render where `projectDirFieldActive` is still
      // true, so a Save gate that deferred with the display alarm would still
      // read enabled at this exact call. The gate must be true (blocked) here
      // regardless of what the field's own alarm is currently showing.
      fireEvent.keyDown(dir, { key: 'Enter' })
      expect(onSubmit).not.toHaveBeenCalled()
    })
  })

  it('clearing the agent back to inherit submits an empty string', async () => {
    // '' is a real instruction here — it restores the fall-back to the global
    // default — so it has to survive the round trip rather than arriving as
    // undefined.
    const f = folder('f1', { name: 'Payments', default_agent: 'kirocrew-dev' })
    const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f], globalDefaultAgent: 'kirocrew' })
    // With a global default the top row names it, so the user can see what
    // "inherit" actually resolves to.
    expect(await openAgents()).toEqual(['Inherit (kirocrew)', 'kirocrew', 'kirocrew-dev'])
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('option')).toBeNull())
    await pickAgent('Inherit (kirocrew)')
    expect(agentTrigger()).toHaveTextContent('Inherit (kirocrew)')
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      defaultAgent: '', touched: ['defaultAgent'],
    }))
  })

  it('names the nearest ANCESTOR agent in the inherit row, not the global default', async () => {
    // `default_agent: ''` inherits from the nearest ancestor that pins one, the
    // same way `project_dir` does. A row hardcoded to the global default reads
    // "Inherit (kirocrew)" over a subfolder whose chats will in fact run
    // kirocrew-dev — the label contradicting the behaviour it describes.
    const folders = [
      folder('a', { name: 'Kiro', default_agent: 'kirocrew-dev' }),
      folder('b', { name: 'Backend', parent_id: 'a' }),
    ]
    open({ folders, parentId: 'b', globalDefaultAgent: 'kirocrew' })
    expect(await openAgents()).toEqual(['Inherit (kirocrew-dev)', 'kirocrew', 'kirocrew-dev'])
  })

  it('names what clearing WOULD inherit in edit mode, ignoring the folder own pin', async () => {
    // Clearing removes this folder's own value, so the row must name the parent's
    // agent — never the value the picker is about to drop.
    const parent = folder('a', { name: 'Kiro', default_agent: 'kirocrew-dev' })
    const self = folder('b', { name: 'Backend', parent_id: 'a', default_agent: 'kirocrew' })
    open({ mode: 'edit', folder: self, folders: [parent, self], globalDefaultAgent: 'kirocrew' })
    expect(await openAgents()).toEqual(['Inherit (kirocrew-dev)', 'kirocrew', 'kirocrew-dev'])
  })

  describe('agent filter box', () => {
    // A long agent roster is unscannable in a plain list, so the popup carries the
    // same "Type to filter…" box as the composer's agent picker.
    const MANY = [
      { name: 'kirocrew' },
      { name: 'kirocrew-dev' },
      { name: 'gpu-autosde-analyzer' },
    ]

    it('narrows the list to the agents matching the typed text', async () => {
      open({ installedAgents: MANY })
      fireEvent.change(await openAgentPopup(), { target: { value: 'dev' } })
      const names = screen.getAllByRole('option').map(o => o.textContent)
      expect(names).toEqual(['kirocrew-dev'])
    })

    it('commits the top match on Enter and submits it', async () => {
      const { onSubmit } = open({ installedAgents: MANY })
      const box = await openAgentPopup()
      fireEvent.change(box, { target: { value: 'gpu' } })
      fireEvent.keyDown(box, { key: 'Enter' })
      await waitFor(() => expect(screen.queryByRole('option')).toBeNull())
      expect(agentTrigger()).toHaveTextContent('gpu-autosde-analyzer')
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ defaultAgent: 'gpu-autosde-analyzer' }))
    })
  })

  it('Enter on a typed agent name picks that agent, not the inherit row naming it', async () => {
    // "Inherit (kirocrew)" sits first and contains "kirocrew", so a top-row
    // commit would save '' (inherit) when the user typed the agent to pin.
    const { onSubmit } = open({ globalDefaultAgent: 'kirocrew' })
    const box = await openAgentPopup()
    fireEvent.change(box, { target: { value: 'kirocrew' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(screen.queryByRole('option')).toBeNull())
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ defaultAgent: 'kirocrew' }))
  })

  it('labels the inherited directory as inherited, not as a value', () => {
    // A bare inherited path renders identically to a real value, so the field
    // read as "already set" when it was actually empty.
    const folders = [folder('a', { name: 'Kiro', project_dir: '/projects/root' })]
    open({ folders, parentId: 'a' })
    const dir = screen.getByTestId('folder-config-project-dir') as HTMLInputElement
    expect(dir.value).toBe('')
    expect(dir.placeholder).toContain('/projects/root')
    expect(dir.placeholder).toMatch(/inherited/i)
  })

  describe('failed save keeps the draft', () => {
    // GPT (blocking), Design and UX all converged on this: submit was
    // fire-and-forget, so a 400 from the backend closed the modal and threw the
    // whole draft away with no feedback. The backend rejects a free-typed
    // project_dir (not absolute / not an existing directory / sensitive),
    // which this modal can produce.
    const reject = () => vi.fn().mockRejectedValue(new Error('project_dir must be an existing directory'))

    it('stays open and keeps every field when the save is rejected', async () => {
      const onSubmit = reject()
      const onClose = vi.fn()
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={onClose} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: 'relative/path' } })
      // Changing the dir holds Save until the new dir's roster settles (so a
      // stale-scope pick cannot be saved); wait it out, then submit.
      await waitFor(() => expect(screen.getByTestId('folder-config-submit')).toBeEnabled())
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      // Never auto-closes on failure...
      expect(onClose).not.toHaveBeenCalled()
      // ...and the draft survives so the user can correct the one bad field.
      await waitFor(() => {
        expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Payments')
        expect((screen.getByTestId('folder-config-project-dir') as HTMLInputElement).value).toBe('relative/path')
      })
    })

    it("surfaces the backend's reason verbatim", async () => {
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={reject()} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      const err = await screen.findByTestId('folder-config-error')
      // friendlyErrText already unwraps {"error": …} into ApiError.message.
      expect(err.textContent).toContain('must be an existing directory')
      expect(err.getAttribute('role')).toBe('alert')
    })

    it('closes only after the save resolves', async () => {
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      const onClose = vi.fn()
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={onClose} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Good' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      // The parent owns closing on success; the modal must not have errored.
      expect(screen.queryByTestId('folder-config-error')).toBeNull()
    })

    it('does not double-submit while a save is in flight', async () => {
      let release: (() => void) | undefined
      const onSubmit = vi.fn(() => new Promise<void>(res => { release = res }))
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Once' } })
      const btn = screen.getByTestId('folder-config-submit') as HTMLButtonElement
      fireEvent.click(btn)
      await waitFor(() => expect(btn.disabled).toBe(true))
      fireEvent.click(btn)
      expect(onSubmit).toHaveBeenCalledTimes(1)
      release?.()
    })

    it('clears a previous error when the retry succeeds', async () => {
      const onSubmit = vi.fn()
        .mockRejectedValueOnce(new Error('project_dir must be an existing directory'))
        .mockResolvedValueOnce(undefined)
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Retry' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await screen.findByTestId('folder-config-error')
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(screen.queryByTestId('folder-config-error')).toBeNull())
    })
  })

  describe('icon rejection is field-anchored (issue #7992)', () => {
    // The server 400s a non-single-emoji icon with code `icon_invalid`. That
    // used to render as the raw English server text in the modal's TOP alert,
    // naming no field. It now renders localized, AT the Icon field.
    const iconReject = () => vi.fn().mockRejectedValue(
      new ApiError(400, 'icon must be a single emoji',
        '{"error": "icon must be a single emoji", "code": "icon_invalid"}'))

    it('renders the localized error at the Icon field, not the top alert', async () => {
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={iconReject()} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      fireEvent.change(screen.getByTestId('folder-config-icon'), { target: { value: 'abc' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      const fieldErr = await screen.findByTestId('folder-config-icon-error')
      // The localized catalog string, not the server's raw body text.
      expect(fieldErr.textContent).toContain('Use a single emoji, or leave the field empty for the default folder icon.')
      // The generic top alert stays down: this failure has a field to point at.
      expect(screen.queryByTestId('folder-config-error')).toBeNull()
    })

    it('clears the field error as soon as the user edits the icon', async () => {
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={iconReject()} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      fireEvent.change(screen.getByTestId('folder-config-icon'), { target: { value: 'abc' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await screen.findByTestId('folder-config-icon-error')
      fireEvent.change(screen.getByTestId('folder-config-icon'), { target: { value: '🚀' } })
      expect(screen.queryByTestId('folder-config-icon-error')).toBeNull()
    })

    it('routes regenerate_icon_invalid to the top alert, not the field', async () => {
      // A request-shape error (non-boolean `regenerate_icon`) the modal can
      // never produce — but if it ever arrives, the field hint "must be a
      // single emoji" would misdescribe an empty field the user never typed
      // in. It stays in the generic top alert.
      const onSubmit = vi.fn().mockRejectedValue(
        new ApiError(400, 'regenerate_icon must be a boolean',
          '{"error": "regenerate_icon must be a boolean", "code": "regenerate_icon_invalid"}'))
      const f = folder('f1', { name: 'Payments' })
      render(
        <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.click(screen.getByTestId('folder-config-icon-regenerate'))
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await screen.findByTestId('folder-config-error')
      expect(screen.queryByTestId('folder-config-icon-error')).toBeNull()
    })

    it('keeps every other failure in the top alert with no field error', async () => {
      const onSubmit = vi.fn().mockRejectedValue(
        new ApiError(400, 'project_dir must be an existing directory',
          '{"error": "project_dir must be an existing directory", "code": "project_dir_invalid"}'))
      render(
        <FolderConfigModal open={true} mode="create" parentId="" folders={[]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await screen.findByTestId('folder-config-error')
      expect(screen.queryByTestId('folder-config-icon-error')).toBeNull()
    })
  })

  describe('round-2 review findings', () => {
    it('does not re-seed when the folder object identity changes mid-failure', async () => {
      // GPT blocking: the re-seed effect was keyed on the `folder` OBJECT. A
      // rejected edit produces three cache changes in a row (optimistic write ->
      // rollback -> invalidate), each handing down a fresh object, so the effect
      // re-ran and restored the persisted fields — erasing the very draft the
      // keep-open-on-error fix exists to preserve.
      const f = () => folder('f1', { name: 'Payments', project_dir: '/repo/pay' })
      const first = f()
      const { rerender } = render(
        <FolderConfigModal open={true} mode="edit" folder={first} folders={[first]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn().mockResolvedValue(undefined)} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments v2' } })
      // Same id, brand-new object — exactly what the cache churn hands down.
      const churned = f()
      rerender(
        <FolderConfigModal open={true} mode="edit" folder={churned} folders={[churned]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn().mockResolvedValue(undefined)} />
      )
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Payments v2'))
    })

    it('still re-seeds when it retargets to a different folder', async () => {
      const a = folder('a', { name: 'Alpha' })
      const b = folder('b', { name: 'Beta' })
      const { rerender } = render(
        <FolderConfigModal open={true} mode="edit" folder={a} folders={[a, b]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn().mockResolvedValue(undefined)} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'edited' } })
      rerender(
        <FolderConfigModal open={true} mode="edit" folder={b} folders={[a, b]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn().mockResolvedValue(undefined)} />
      )
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Beta'))
    })



    it('ignores backdrop and Escape once the draft is dirty', () => {
      // UX: four fields of work behind a click-anywhere backdrop.
      const { onClose } = open()
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(onClose).toHaveBeenCalledTimes(1)   // clean draft still dismisses

      onClose.mockClear()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Typed' } })
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(onClose).not.toHaveBeenCalled()
    })

    it('a color-only pick also arms the dismiss guard', () => {
      // Regression: the dirty check once omitted `color`, so a draft whose
      // ONLY change was a swatch pick was silently discarded by Escape.
      const { onClose } = open()
      fireEvent.click(screen.getByLabelText('Set color to Red'))
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(onClose).not.toHaveBeenCalled()
    })

    it('always closes from the explicit Cancel button, even when dirty', () => {
      const { onClose } = open()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Typed' } })
      fireEvent.click(screen.getByText(/^Cancel$/))
      expect(onClose).toHaveBeenCalledTimes(1)
    })

    it('shows the typed name as the breadcrumb leaf in create mode', () => {
      open({ parentId: '' })
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments rewrite' } })
      expect(screen.getByTestId('folder-config-destination').textContent).toContain('Payments rewrite')
    })
  })


  describe('reports only user-edited fields (touched)', () => {
    // The caller builds its PATCH from `touched` (measured against what the
    // modal opened with), never from a diff against live cache — a field
    // another client changed while the modal was open differs from the draft
    // without the user having touched it, and re-sending the stale value
    // would silently revert it. Only the modal knows the open-time seed, so
    // it is the only place that can say what the *user* changed.
    const seedFolder = folder('f1', { name: 'Payments', project_dir: '/repo/pay' })

    it('a name-only edit reports name alone', async () => {
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(
        <FolderConfigModal open={true} mode="edit" folder={seedFolder} folders={[seedFolder]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments v2' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      const draft = onSubmit.mock.calls[0][0]
      expect(draft.touched).toEqual(['name'])
    })

    it('reports nothing when the user opens and saves without editing', async () => {
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(
        <FolderConfigModal open={true} mode="edit" folder={seedFolder} folders={[seedFolder]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      expect(onSubmit.mock.calls[0][0].touched).toEqual([])
    })

    it('reports each field the user actually edited', async () => {
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      // /repo/new's roster includes the global agents (the backend unions global
      // + project rows), so the picked global agent kirocrew-dev stays valid.
      ;(api.kirocrewAgents as unknown as ReturnType<typeof vi.fn>)
        .mockResolvedValue({ agents: [{ name: 'kirocrew' }, { name: 'kirocrew-dev' }], default_agent: '' })
      render(
        <FolderConfigModal open={true} mode="edit" folder={seedFolder} folders={[seedFolder]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/new' } })
      await pickAgent('kirocrew-dev')
      // The dir change holds Save until the new dir's roster settles.
      await waitFor(() => expect(screen.getByTestId('folder-config-submit')).toBeEnabled())
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      const t = onSubmit.mock.calls[0][0].touched
      expect(t).toContain('projectDir')
      expect(t).toContain('defaultAgent')
      expect(t).not.toContain('name')
    })

    it('seeds the icon from the folder and reports an icon edit', async () => {
      const iconFolder = folder('f2', { name: 'Rockets', icon: '🚀' })
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(
        <FolderConfigModal open={true} mode="edit" folder={iconFolder} folders={[iconFolder]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      expect((screen.getByTestId('folder-config-icon') as HTMLInputElement).value).toBe('🚀')
      // Clearing falls back to the default glyph — '' is a real instruction.
      fireEvent.change(screen.getByTestId('folder-config-icon'), { target: { value: '' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      const draft = onSubmit.mock.calls[0][0]
      expect(draft.touched).toEqual(['icon'])
      expect(draft.icon).toBe('')
      expect(draft.regenerateIcon).toBe(false)
    })

    it('Auto-generate arms regenerateIcon and restores the seeded icon value', async () => {
      // The backend rejects icon + regenerate_icon in one request, so arming
      // regenerate must also discard a manual edit — and vice versa.
      const iconFolder = folder('f2', { name: 'Rockets', icon: '🚀' })
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(
        <FolderConfigModal open={true} mode="edit" folder={iconFolder} folders={[iconFolder]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.change(screen.getByTestId('folder-config-icon'), { target: { value: '🧪' } })
      fireEvent.click(screen.getByTestId('folder-config-icon-regenerate'))
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      const draft = onSubmit.mock.calls[0][0]
      expect(draft.regenerateIcon).toBe(true)
      // The manual edit was discarded, so a caller keying on touched cannot
      // accidentally send both icon and regenerate_icon.
      expect(draft.icon).toBe('🚀')
      expect(draft.touched).toContain('icon')
    })

    it('typing after Auto-generate disarms the pending regenerate', async () => {
      const iconFolder = folder('f2', { name: 'Rockets', icon: '🚀' })
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(
        <FolderConfigModal open={true} mode="edit" folder={iconFolder} folders={[iconFolder]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} />
      )
      fireEvent.click(screen.getByTestId('folder-config-icon-regenerate'))
      fireEvent.change(screen.getByTestId('folder-config-icon'), { target: { value: '🧪' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      const draft = onSubmit.mock.calls[0][0]
      expect(draft.regenerateIcon).toBe(false)
      expect(draft.icon).toBe('🧪')
    })

    it('an armed regenerate renders an empty input, matching the default-glyph preview', () => {
      // While armed, the preview falls back to the default glyph; if the input
      // kept showing the old emoji the preview would stop previewing the input.
      const iconFolder = folder('f2', { name: 'Rockets', icon: '🚀' })
      render(
        <FolderConfigModal open={true} mode="edit" folder={iconFolder} folders={[iconFolder]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn()} />
      )
      fireEvent.click(screen.getByTestId('folder-config-icon-regenerate'))
      expect((screen.getByTestId('folder-config-icon') as HTMLInputElement).value).toBe('')
    })

    it('edit mode shows the cleared-state hint only when the field is emptied', () => {
      // Empty means the default glyph in both modes; the edit-mode cleared
      // state keeps its own hint so clearing an existing icon is visibly
      // acknowledged rather than silently reverting.
      const iconFolder = folder('f2', { name: 'Rockets', icon: '🚀' })
      render(
        <FolderConfigModal open={true} mode="edit" folder={iconFolder} folders={[iconFolder]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn()} />
      )
      expect(screen.queryByText(/Empty keeps the default folder icon/)).toBeNull()
      fireEvent.change(screen.getByTestId('folder-config-icon'), { target: { value: '' } })
      expect(screen.getByText(/Empty keeps the default folder icon/)).toBeTruthy()
    })

  })

  it('associates every label with its control', () => {
    // eslint's jsx-a11y/label-has-for cannot see through the `Input` wrapper to
    // confirm nesting, so it warns even when the association is correct. Assert
    // the runtime truth instead of silencing the rule.
    open()
    expect(screen.getByLabelText(/^Name$/)).toBe(screen.getByTestId('folder-config-name'))
    // The agent picker renders a <button>, so its name comes from an aria-label
    // carrying the same "Default agent" key its visible heading uses — an
    // external <label htmlFor> would dangle.
    expect(screen.getByLabelText(/Default agent/)).toBe(agentTrigger())
  })

  it('does not submit while an IME composition is in flight', () => {
    // Regression guard: the first cut of this modal used a bare
    // `if (e.key === 'Enter') submit()`, so the Enter that COMMITS a Chinese /
    // Japanese / Korean composition also created the folder — named after a
    // half-typed word. The inline input this modal replaced guarded it; so must this.
    const { onSubmit } = open()
    const name = screen.getByTestId('folder-config-name')
    fireEvent.change(name, { target: { value: '支付' } })
    fireEvent.compositionStart(name)
    fireEvent.keyDown(name, { key: 'Enter', keyCode: 13 })
    expect(onSubmit).not.toHaveBeenCalled()
    // After the composition ends the same key submits normally.
    fireEvent.compositionEnd(name)
    fireEvent.keyDown(name, { key: 'Enter', keyCode: 229 })   // still IME-processing
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('does not submit from the project-dir field mid-composition', () => {
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Named' } })
    const dir = screen.getByTestId('folder-config-project-dir')
    fireEvent.compositionStart(dir)
    fireEvent.keyDown(dir, { key: 'Enter', keyCode: 13 })
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('shows the inherited project dir as a placeholder, never a value', () => {
    const folders = [folder('a', { name: 'Kiro', project_dir: '/projects/root' })]
    open({ folders, parentId: 'a' })
    const dir = screen.getByTestId('folder-config-project-dir') as HTMLInputElement
    // Pre-filling would write a duplicate explicit value and sever the
    // inheritance link that resolveFolderProjectDir provides.
    expect(dir.value).toBe('')
    expect(dir.placeholder).toContain('/projects/root')
  })

  it('inherits through a grandparent', () => {
    const folders = [
      folder('a', { name: 'Kiro', project_dir: '/projects/root' }),
      folder('b', { name: 'Backend', parent_id: 'a' }),
    ]
    open({ folders, parentId: 'b' })
    expect((screen.getByTestId('folder-config-project-dir') as HTMLInputElement).placeholder)
      .toContain('/projects/root')
  })

  describe('edit mode', () => {
    const existing = folder('f1', {
      name: 'Payments', project_dir: '/repo/pay', default_agent: 'kirocrew-dev',
    })

    it('prefills every field from the folder', () => {
      open({ mode: 'edit', folder: existing, folders: [existing], parentId: undefined })
      expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Payments')
      expect((screen.getByTestId('folder-config-project-dir') as HTMLInputElement).value).toBe('/repo/pay')
      expect(agentTrigger()).toHaveTextContent('kirocrew-dev')
    })

    it('reset clears the color back to default', () => {
      const withColor = { ...existing, color: '#3b82f6' }
      const { onSubmit } = open({ mode: 'edit', folder: withColor, folders: [withColor] })
      // "No color" is the leading swatch in the always-visible palette row.
      fireEvent.click(screen.getByTestId('folder-config-color-reset'))
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      // '' is a real instruction on the color pipe: PATCH color:'' clears it.
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ color: '', touched: expect.arrayContaining(['color']) }))
    })

    it('clearing the project dir submits an empty string, restoring inheritance', () => {
      const { onSubmit } = open({ mode: 'edit', folder: existing, folders: [existing] })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ projectDir: '' }))
    })
  })

  it('does not leak a draft between openings', async () => {
    const a = folder('a', { name: 'Alpha' })
    const b = folder('b', { name: 'Beta' })
    const { rerender } = render(
      <FolderConfigModal open={true} mode="edit" folder={a} folders={[a, b]}
        installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn()} />
    )
    expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Alpha')
    rerender(
      <FolderConfigModal open={true} mode="edit" folder={b} folders={[a, b]}
        installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn()} />
    )
    await waitFor(() =>
      expect((screen.getByTestId('folder-config-name') as HTMLInputElement).value).toBe('Beta'))
  })

  it('opens the project picker from Browse', async () => {
    open()
    fireEvent.click(screen.getByTestId('folder-config-browse'))
    const { api } = await import('../api/client')
    await waitFor(() => expect(api.recentProjects).toHaveBeenCalled())
  })

  it('routes a Browse pick to projectDir even after a steering pick armed the target', async () => {
    // Regression (GPT review F2): the steering "Add directory" button sets the
    // shared picker target to 'steering'; if Project Browse does not reset it,
    // the project selection is appended to steeringDirs and projectDir stays
    // empty. Open steering first, then Project Browse, and assert the pick
    // lands in projectDir with steeringDirs untouched.
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
    // Arm the steering target, then close the picker without selecting.
    fireEvent.click(screen.getByTestId('folder-config-steering-add'))
    const { api } = await import('../api/client')
    await waitFor(() => expect(api.browseDirs).toHaveBeenCalled())
    fireEvent.keyDown(document.body, { key: 'Escape' })
    // Now open Project Browse and commit a path.
    fireEvent.click(screen.getByTestId('folder-config-browse'))
    const combo = await screen.findByRole('combobox', { name: /project directory path/i })
    fireEvent.change(combo, { target: { value: '/proj/root' } })
    fireEvent.mouseDown(screen.getByText('Select'))
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    const submitted = onSubmit.mock.calls[0][0]
    expect(submitted.projectDir).toBe('/proj/root')
    expect(submitted.steeringDirs).toEqual([])
  })
})
