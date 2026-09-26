import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render as rtlRender, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import FolderConfigModal from '../components/FolderConfigModal'
import { ApiError } from '../api/apiError'
import { api } from '../api/client'
import { ChatFolder } from '../types'

// The modal now fetches the project-scoped roster via react-query (useQuery),
// so every render needs a QueryClient in context. Wrap RTL's render so the
// existing call sites (and their `rerender`) get a provider transparently; a
// fresh client per render keeps tests isolated, and retries are off so a
// rejected queryFn surfaces immediately instead of being retried.
function render(ui: React.ReactElement, options?: Parameters<typeof rtlRender>[1]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  // `wrapper` is re-applied by RTL's own rerender, so a plain rerender(next)
  // keeps the provider without double-wrapping (double-wrapping remounts the
  // subtree and re-runs mount effects — e.g. the tag seed).
  return rtlRender(ui, { wrapper: Wrapper, ...options })
}

vi.mock('../api/client', () => ({
  api: {
    // ProjectPicker fetches these on open; the modal itself never calls them.
    recentProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    browseDirs: vi.fn().mockResolvedValue({ path: '/', parent: '', dirs: [] }),
    // Fetched by the modal ONLY when a project directory is set, to scope the
    // agent dropdown to that directory's project agents. Default: empty roster
    // (the no-dir tests never reach this call).
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

const folder = (id: string, extra: Partial<ChatFolder> = {}): ChatFolder =>
  ({ id, name: id, order: 0, ...extra }) as ChatFolder

const AGENTS = [{ name: 'kirocrew' }, { name: 'kirocrew-dev' }]

function open(props: Partial<React.ComponentProps<typeof FolderConfigModal>> = {}) {
  const onSubmit = vi.fn().mockResolvedValue(undefined)
  const onClose = vi.fn()
  const utils = render(
    <FolderConfigModal
      open={true}
      mode="create"
      parentId=""
      folders={[]}
      installedAgents={AGENTS}
      onClose={onClose}
      onSubmit={onSubmit}
      onRetryTags={vi.fn()}
      {...props}
    />
  )
  return { onSubmit, onClose, ...utils }
}

/* The agent picker is a Radix Select (SimpleSelect), not a native <select>, so
 * it is addressed by its accessible name — the `aria-label` that carries the
 * "Default agent" heading's key, since a <button> cannot be reached by an
 * external <label htmlFor>. Its options live in a portal that only exists while
 * the popup is open, so any assertion about them has to open it first.
 *
 * NOTE ON THE HARNESS: a Radix Select nested in a Radix Dialog cannot be driven
 * in jsdom (Radix's flushSync inside Testing Library's act() throws "Should not
 * already be working"), which is why CrewEditorSelect.test.tsx and
 * WorkspaceModal.test.tsx stub SimpleSelect out. That does NOT apply here:
 * `Modal` is hand-rolled (createPortal + framer-motion), so there is no Radix
 * layer above the select and the real component is driven directly. Keep it
 * that way — a stub here would stop testing the shipped dropdown. */
function agentTrigger() {
  return screen.getByRole('combobox', { name: 'Default agent' })
}

/** Open the agent popup and return its option labels in render order. */
async function openAgents(): Promise<string[]> {
  fireEvent.click(agentTrigger())
  // findAllByRole, not findByRole: the latter throws on more than one match, and
  // every case here has at least the inherit/None row plus an agent.
  const opts = await screen.findAllByRole('option')
  return opts.map(o => o.textContent ?? '')
}

/** Pick an agent by its visible label, then wait for the popup to unmount —
 *  Radix marks the rest of the page inert while it is up, so a later click on
 *  Submit would be swallowed. */
async function pickAgent(label: string | RegExp) {
  fireEvent.click(agentTrigger())
  fireEvent.click(await screen.findByRole('option', { name: label }))
  await waitFor(() => expect(screen.queryByRole('option')).toBeNull())
}

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
    expect(screen.getAllByRole('combobox')).toHaveLength(1)
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
      await new Promise(r => setTimeout(r, 50))
      expect(onSubmit).not.toHaveBeenCalled()
    })
  })

  it('clearing the agent back to inherit submits an empty string', async () => {
    // SimpleSelect routes '' through an internal sentinel because Radix reserves
    // '' for "no selection". '' is a real instruction here — it restores the
    // fall-back to the global default — so it has to survive the round trip
    // rather than arriving as the sentinel or as undefined.
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

  describe('steering directories', () => {
    // The "Add directory" button opens the SAME ProjectPicker the project-dir
    // Browse button uses, routed to push into an ordered array. The mock picker
    // (recentProjects/browseDirs above) resolves the Browse tab at path '/', so
    // clicking its "Select" button commits a path we can assert on.
    const addDir = async (path: string) => {
      fireEvent.click(screen.getByTestId('folder-config-steering-add'))
      const { api } = await import('../api/client')
      await waitFor(() => expect(api.browseDirs).toHaveBeenCalled())
      // Type an absolute path into the picker's combobox and commit it.
      const combo = await screen.findByRole('combobox', { name: /project directory path/i })
      fireEvent.change(combo, { target: { value: path } })
      fireEvent.mouseDown(screen.getByText('Select'))
    }

    it('renders no directory rows for a folder with none', () => {
      open()
      expect(screen.queryByTestId('folder-config-steering-dirs')).toBeNull()
      expect(screen.getByTestId('folder-config-steering-add')).toBeTruthy()
    })

    it('prefills existing steering dirs in edit mode', () => {
      const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a', '/std/b'] })
      open({ mode: 'edit', folder: f, folders: [f] })
      const list = screen.getByTestId('folder-config-steering-dirs')
      expect(list.textContent).toContain('/std/a')
      expect(list.textContent).toContain('/std/b')
    })

    it('adds a directory via the picker and submits it under steering_dirs', async () => {
      const { onSubmit } = open()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Standards' } })
      await addDir('/org/standards')
      await waitFor(() =>
        expect(screen.getByTestId('folder-config-steering-dir-0').textContent).toContain('/org/standards'))
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
        steeringDirs: ['/org/standards'],
        touched: expect.arrayContaining(['steeringDirs']),
      }))
    })

    it('removes a directory row', async () => {
      const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a', '/std/b'] })
      const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
      fireEvent.click(screen.getByTestId('folder-config-steering-dir-remove-0'))
      // /std/a removed, /std/b shifts to index 0.
      await waitFor(() =>
        expect(screen.getByTestId('folder-config-steering-dir-0').textContent).toContain('/std/b'))
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
        steeringDirs: ['/std/b'],
        touched: expect.arrayContaining(['steeringDirs']),
      }))
    })

    it('clearing every directory submits an empty array (PATCH [] clears)', async () => {
      const f = folder('f1', { name: 'Payments', steering_dirs: ['/only'] })
      const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
      fireEvent.click(screen.getByTestId('folder-config-steering-dir-remove-0'))
      await waitFor(() => expect(screen.queryByTestId('folder-config-steering-dirs')).toBeNull())
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
        steeringDirs: [],
        touched: expect.arrayContaining(['steeringDirs']),
      }))
    })

    it('does not report steeringDirs in touched when unchanged', async () => {
      const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a'] })
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(
        <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} onRetryTags={vi.fn()} />
      )
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Renamed' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      expect(onSubmit.mock.calls[0][0].touched).not.toContain('steeringDirs')
    })

    it('shows ancestor steering dirs read-only, accumulative and root-first', () => {
      // A parent folder's dirs are always in effect for a child; the modal
      // shows them read-only so the effective set is visible without letting
      // the child edit the parent's contribution.
      const folders = [
        folder('org', { name: 'Org', steering_dirs: ['/org/standards'] }),
        folder('repo', { name: 'Repo', parent_id: 'org' }),
      ]
      open({ folders, parentId: 'repo' })
      const inherited = screen.getByTestId('folder-config-steering-inherited')
      expect(inherited.textContent).toContain('/org/standards')
      // Read-only: no editable control (remove button) inside the inherited group.
      expect(inherited.querySelector('button')).toBeNull()
    })

    it('does not list an ancestor owned by another principal as inherited', () => {
      // The backend never delivers an app-owned ancestor's dirs to a
      // person-owned child's chats, so the modal must not present them as
      // inherited. A folder created from this dashboard is the person's.
      const folders = [
        folder('app-root', { name: 'Radar', owner_app: 'radar', steering_dirs: ['/radar/rules'] }),
        folder('org', { name: 'Org', parent_id: 'app-root', steering_dirs: ['/org/standards'] }),
      ]
      open({ folders, parentId: 'org' })
      const inherited = screen.getByTestId('folder-config-steering-inherited')
      expect(inherited.textContent).toContain('/org/standards')
      expect(inherited.textContent).not.toContain('/radar/rules')
    })

    it('lists an ancestor owned by the SAME principal as the folder being edited', () => {
      const parent = folder('app-root', { name: 'Radar', owner_app: 'radar', steering_dirs: ['/radar/rules'] })
      const child = folder('app-child', { name: 'Radar child', parent_id: 'app-root', owner_app: 'radar' })
      render(
        <FolderConfigModal open={true} mode="edit" folder={child} folders={[parent, child]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn()} onRetryTags={vi.fn()} />
      )
      expect(screen.getByTestId('folder-config-steering-inherited').textContent).toContain('/radar/rules')
    })

    it('shows no inherited group when every ancestor with dirs belongs to another principal', () => {
      const folders = [folder('app-root', { name: 'Radar', owner_app: 'radar', steering_dirs: ['/radar/rules'] })]
      open({ folders, parentId: 'app-root' })
      expect(screen.queryByTestId('folder-config-steering-inherited')).toBeNull()
    })

    it('adding a steering dir arms the dismiss guard', async () => {
      const { onClose } = open()
      await addDir('/org/standards')
      await waitFor(() =>
        expect(screen.getByTestId('folder-config-steering-dir-0')).toBeTruthy())
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(onClose).not.toHaveBeenCalled()
    })

    it('closing the picker without a selection leaves the list unchanged', async () => {
      // Req 8.2: a cancelled pick must neither add a row nor mark the field
      // touched. Escape on the picker's path field is its no-selection close.
      const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a'] })
      const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
      fireEvent.click(screen.getByTestId('folder-config-steering-add'))
      const { api } = await import('../api/client')
      await waitFor(() => expect(api.browseDirs).toHaveBeenCalled())
      const combo = await screen.findByRole('combobox', { name: /project directory path/i })
      fireEvent.change(combo, { target: { value: '/never/picked' } })
      fireEvent.keyDown(combo, { key: 'Escape' })
      await waitFor(() =>
        expect(screen.queryByRole('combobox', { name: /project directory path/i })).toBeNull())
      const list = screen.getByTestId('folder-config-steering-dirs')
      expect(list.textContent).toContain('/std/a')
      expect(list.textContent).not.toContain('/never/picked')
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Renamed' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      expect(onSubmit.mock.calls[0][0].steeringDirs).toEqual(['/std/a'])
      expect(onSubmit.mock.calls[0][0].touched).not.toContain('steeringDirs')
    })

    it('surfaces the server steering_dirs_invalid message in the error area', async () => {
      // Req 8.5: the server's own wording reaches the modal so the user learns
      // WHICH directory was refused, not just that the save failed.
      const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a'] })
      const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
      onSubmit.mockRejectedValueOnce(
        new Error('Steering directory must be an existing directory: /std/a'))
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Renamed' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      const err = await screen.findByTestId('folder-config-error')
      expect(err.textContent).toContain('Steering directory must be an existing directory: /std/a')
    })

    it('falls back to the generic save-failed string when the rejection carries no message', async () => {
      // Req 8.5: a bare rejection (no message) must still tell the user the
      // save did not land, via the modal's generic string.
      const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a'] })
      const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
      onSubmit.mockRejectedValueOnce(new Error(''))
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Renamed' } })
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      const err = await screen.findByTestId('folder-config-error')
      expect(err.textContent).toContain('Could not save the folder.')
    })
  })

  /* The bug this whole change exists for: when a project directory is set, the
   * agent dropdown must offer that directory's project agents, not just the
   * global `installedAgents` prop (which is the ACTIVE SLOT's roster and has
   * nothing to do with the folder being configured). */
  describe('project-scoped agent roster', () => {
    const mockAgents = api.kirocrewAgents as unknown as ReturnType<typeof vi.fn>

    it('does not fetch a project roster when no directory is set', async () => {
      open()
      // Nothing to scope to; the dropdown uses the installedAgents prop.
      await waitFor(() => {})
      expect(mockAgents).not.toHaveBeenCalled()
      expect(await openAgents()).toEqual(
        expect.arrayContaining(['kirocrew', 'kirocrew-dev']),
      )
    })

    it('fetches the roster for the typed project dir and lists its agents', async () => {
      mockAgents.mockResolvedValue({
        agents: [{ name: 'repo-dev' }, { name: 'repo-reviewer' }],
        default_agent: '',
      })
      open()
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), {
        target: { value: '/repo/pay' },
      })
      // Debounced fetch, keyed on the typed dir, with NO session key.
      await waitFor(() =>
        expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/pay'),
      )
      // The option only renders after the debounced query resolves; findAllByRole
      // inside openAgents waits for it.
      const labels = await openAgents()
      expect(labels).toEqual(expect.arrayContaining(['repo-dev', 'repo-reviewer']))
    })

    it('unions the dir roster with globally installed templates, not replaces it', async () => {
      // Opus FINDING (FolderConfigModal.tsx effectiveAgents): GET
      // /api/agents?project_path= returns only cfg.agents.items() plus the dir's
      // project names — it does NOT carry the installed TEMPLATES (those come
      // only from the agent catalog). With config.agents empty by default,
      // REPLACING the roster with projectRoster strips every installed template
      // (including kirocrew) the instant a dir is set, and an edit folder pinned
      // to one is flagged "(not in this project)" and blocked from Save. The
      // roster must be the UNION: the dir's project agents PLUS the global
      // installed templates. RED before the fix (kirocrew absent from the dir
      // roster is dropped, so the option is missing and pickAgent throws).
      mockAgents.mockResolvedValue({ agents: [{ name: 'repo-dev' }], default_agent: '' })
      open({ installedAgents: [{ name: 'kirocrew', scope: 'global' }] })
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/x' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/x'))
      // Both the dir's project agent AND the global installed template are offered.
      const labels = await openAgents()
      expect(labels).toEqual(expect.arrayContaining(['repo-dev', 'kirocrew']))
      fireEvent.keyDown(document, { key: 'Escape' })
      await waitFor(() => expect(screen.queryByRole('option')).toBeNull())
      // Picking the global template is valid under a project dir — not flagged
      // "(not in this project)", and Save is NOT blocked.
      await pickAgent('kirocrew')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('kirocrew'))
      expect(agentTrigger()).not.toHaveTextContent(/not in this project/i)
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
      )
    })

    it('still flags an agent in NEITHER the dir roster nor the global templates', async () => {
      // The union must not swallow the genuine flag: an edit folder's saved agent
      // that is in NEITHER the dir's project roster NOR the global installed
      // templates is still an orphan under a set directory — flagged
      // "(not in this project)" and blocking Save on a session change.
      mockAgents.mockResolvedValue({ agents: [{ name: 'repo-dev' }], default_agent: '' })
      open({ installedAgents: [{ name: 'kirocrew', scope: 'global' }] })
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/x' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/x'))
      // repo-dev (dir) and kirocrew (global) are both offered; a foreign pick is not.
      await pickAgent('repo-dev')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      // Re-scope to a dir whose roster lacks repo-dev; kirocrew (global) is still
      // unioned in, but repo-dev is in neither source now → flagged and blocked.
      mockAgents.mockResolvedValue({ agents: [{ name: 'other-dir-agent' }], default_agent: '' })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/y' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/y'))
      await waitFor(() => expect(agentTrigger()).toHaveTextContent(/repo-dev.*not in this project/i))
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
    })

    it('scopes to an inherited ancestor dir when the draft dir is empty', async () => {
      mockAgents.mockResolvedValue({ agents: [{ name: 'inherited-agent' }], default_agent: '' })
      // Subfolder create: parent pins a project dir, child inherits it.
      open({
        parentId: 'p1',
        folders: [folder('p1', { name: 'Parent', project_dir: '/repo/parent' })],
      })
      await waitFor(() =>
        expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/parent'),
      )
      // Open the picker and wait for the inherited dir's agent to populate it.
      fireEvent.click(agentTrigger())
      expect(await screen.findByRole('option', { name: 'inherited-agent' })).toBeInTheDocument()
    })

    it('falls back to the global roster when the project fetch fails', async () => {
      mockAgents.mockRejectedValue(new Error('scan failed'))
      open()
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), {
        target: { value: '/repo/pay' },
      })
      await waitFor(() => expect(mockAgents).toHaveBeenCalled())
      // A failed scan must not blank the picker — the global prop roster stays.
      expect(await openAgents()).toEqual(
        expect.arrayContaining(['kirocrew', 'kirocrew-dev']),
      )
    })

    it('flags and blocks Save when the dir changes and the agent is gone', async () => {
      // Dir A has repo-dev; dir B does not.
      mockAgents.mockImplementation((_sk?: string, dir?: string) =>
        Promise.resolve({
          agents: dir === '/repo/a' ? [{ name: 'repo-dev' }] : [{ name: 'other-agent' }],
          default_agent: '',
        }),
      )
      const { onSubmit } = open()
      // Scope to A; wait for A's roster to list repo-dev, then pick it.
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/a' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      await pickAgent('repo-dev')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      // Re-scope to B, whose roster lacks repo-dev. The pick is NOT silently
      // cleared — it stays, flagged "(not in this project)", and Save is blocked.
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/b' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/b'))
      await waitFor(() => expect(agentTrigger()).toHaveTextContent(/repo-dev.*not in this project/i))
      // The notice one line under the trigger must AGREE with it. UX found the
      // installed-notice wording contradicting the label on all three counts: the
      // agent IS installed, "pick an installed agent" is the wrong advice when
      // changing the directory is the other fix, and "chats won't use it" is the
      // wrong consequence when Save is blocked outright.
      expect(screen.getByTestId('folder-config-agent-notice')).not.toHaveTextContent(
        /isn’t installed|isn't installed/i,
      )
      // The notice names no button. Create mode's footer is Cancel / Create
      // folder — the old "Save is blocked" copy sent the user scanning for a
      // "Save" that isn't there. The mode-neutral wording says the folder
      // can't be saved without naming a control.
      expect(screen.getByTestId('folder-config-agent-notice')).toHaveTextContent(/can.t be saved/i)
      expect(screen.getByTestId('folder-config-agent-notice')).not.toHaveTextContent(/Save is blocked/i)
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
      // Choosing a valid agent for the new scope clears the error and saves it.
      await pickAgent('other-agent')
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
      )
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      expect(onSubmit.mock.calls[0][0].defaultAgent).toBe('other-agent')
    })

    it('flags and blocks an edited child whose inherited agent is absent after re-scope', async () => {
      // GPT span 3d755b58415a: an empty own default_agent does not mean there is
      // no agent to validate. Chat start walks the parent chain, so this child
      // effectively runs parent-agent. Re-scoping it from A (where that agent is
      // valid) to B (where it is absent) must flag the inherited binding and
      // block Save rather than deferring the failure to chat start.
      mockAgents.mockImplementation((_sk?: string, dir?: string) =>
        Promise.resolve({
          agents: dir === '/repo/a' ? [{ name: 'parent-agent' }] : [{ name: 'other-agent' }],
          default_agent: '',
        }),
      )
      const parent = folder('parent', { name: 'Parent', default_agent: 'parent-agent' })
      const child = folder('child', {
        name: 'Child', parent_id: 'parent', project_dir: '/repo/a', default_agent: '',
      })
      open({ mode: 'edit', folder: child, folders: [parent, child] })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
      )

      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/b' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/b'))
      const notice = await screen.findByTestId('folder-config-agent-notice')
      expect(notice).toHaveTextContent(/isn.t among this directory.s project agents/i)
      expect(agentTrigger()).toHaveTextContent('Inherit (parent-agent — not in this project)')
      expect(agentTrigger().getAttribute('aria-describedby')).toBe(notice.id)
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
    })

    it('keeps Save enabled for an unchanged edited child while its inherited-agent roster loads', async () => {
      // UX span 9884d6aad247: the folder's OWN saved default_agent is empty,
      // but its seeded EFFECTIVE agent is parent-agent. Opening the modal does
      // not constitute an agent change, so the initial scan must not deaden Save.
      let release: ((v: { agents: { name: string }[]; default_agent: string }) => void) | undefined
      mockAgents.mockImplementation(() => new Promise(res => { release = res }))
      const parent = folder('parent', {
        name: 'Parent', project_dir: '/repo/a', default_agent: 'parent-agent',
      })
      const child = folder('child', {
        name: 'Child', parent_id: 'parent', project_dir: '/repo/a', default_agent: '',
      })
      open({ mode: 'edit', folder: child, folders: [parent, child] })

      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      expect(await screen.findByTestId('folder-config-agent-roster-loading')).toBeInTheDocument()
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)

      release?.({ agents: [{ name: 'parent-agent' }], default_agent: '' })
      await waitFor(() =>
        expect(screen.queryByTestId('folder-config-agent-roster-loading')).not.toBeInTheDocument(),
      )
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
    })

    it('keeps Save enabled for a created child when its inherited agent exists after re-scope', async () => {
      mockAgents.mockResolvedValue({ agents: [{ name: 'parent-agent' }], default_agent: '' })
      const parent = folder('parent', {
        name: 'Parent', project_dir: '/repo/parent', default_agent: 'parent-agent',
      })
      open({ parentId: 'parent', folders: [parent] })
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Child' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/child' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/child'))
      await waitFor(() =>
        expect(screen.queryByTestId('folder-config-agent-roster-loading')).not.toBeInTheDocument(),
      )
      expect(agentTrigger()).toHaveTextContent('Inherit (parent-agent)')
      expect(screen.queryByTestId('folder-config-agent-notice')).toBeNull()
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
    })

    it('keeps Save enabled while a dir scan is in flight when no agent is picked', async () => {
      // UX flicker finding (span 9884d6aad247): rescopeUnsettled disabled Save on
      // EVERY debounced scan even with NO effective agent selected. A child with
      // neither an explicit nor inherited agent has nothing agent-vs-scope to
      // validate, so a directory scan must not gate Save — the dead-button
      // flicker while typing a path. Hang the scan so the in-flight window is
      // deterministic. RED before the original fix: Save was disabled for the
      // whole scan window.
      let release: ((v: { agents: { name: string }[]; default_agent: string }) => void) | undefined
      mockAgents.mockImplementation(() => new Promise(res => { release = res }))
      const parent = folder('parent', { name: 'Parent' })
      open({ parentId: 'parent', folders: [parent] }) // create child; no effective agent
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/typing' } })
      // Wait for the debounced scan to actually FIRE (so `release` is assigned
      // and the query is genuinely fetching, not merely in the debounce gap).
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/typing'))
      // The scan is in flight (loading hint present) ...
      expect(await screen.findByTestId('folder-config-agent-roster-loading')).toBeInTheDocument()
      // ... yet with no effective agent there is nothing to validate, so Save stays enabled.
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
      release?.({ agents: [{ name: 'repo-dev' }], default_agent: '' })
      // Still enabled once the scan settles (no orphan, no effective agent).
      await waitFor(() =>
        expect(screen.queryByTestId('folder-config-agent-roster-loading')).not.toBeInTheDocument(),
      )
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
    })

    it('still blocks Save mid-scan when an agent IS selected (blocking behaviour unchanged)', async () => {
      // The other half of the flicker fix: gating rescopeUnsettled on a selected
      // agent must NOT weaken the in-flight block for a real re-scope with a pick.
      // Pick repo-dev under dir A, then re-scope to a dir whose scan hangs: Save
      // stays BLOCKED through the in-flight window (a stale-scope pick must not be
      // savable), exactly as before.
      let release: ((v: { agents: { name: string }[]; default_agent: string }) => void) | undefined
      mockAgents.mockImplementation((_sk?: string, dir?: string) => {
        if (dir === '/repo/a') return Promise.resolve({ agents: [{ name: 'repo-dev' }], default_agent: '' })
        return new Promise(res => { release = res })
      })
      open()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/a' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      await pickAgent('repo-dev')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      // Re-scope to a dir whose scan is in flight — with an agent selected, Save
      // must be blocked until the new scope is validated.
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/b' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/b'))
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true),
      )
      release?.({ agents: [{ name: 'other-agent' }], default_agent: '' })
      // Settles to an orphan pick → still blocked (now on the orphan).
      await waitFor(() => expect(agentTrigger()).toHaveTextContent(/repo-dev.*not in this project/i))
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
    })

    it('shows a loading hint while the roster is in flight, so Save is not dead with no cue', async () => {
      // UX Review: Save is disabled during the debounce + fetch while the
      // picker still shows the old scope's agents, so without a cue the button
      // just goes dead. The sibling Tags field in this modal shows its own
      // loading hint; this asserts the Default agent field matches it.
      let release: ((v: { agents: { name: string }[]; default_agent: string }) => void) | undefined
      mockAgents.mockImplementation(
        () =>
          new Promise(resolve => {
            release = resolve
          }),
      )
      open()
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/slow' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/slow'))
      // In flight: the hint is present and Save is disabled.
      expect(await screen.findByTestId('folder-config-agent-roster-loading')).toBeInTheDocument()
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
      // Settled: the hint goes away rather than sticking.
      release?.({ agents: [{ name: 'repo-dev' }], default_agent: '' })
      await waitFor(() =>
        expect(screen.queryByTestId('folder-config-agent-roster-loading')).not.toBeInTheDocument(),
      )
    })

    it('surfaces a roster scan error, keeps name-only Save, blocks an agent pick', async () => {
      // GPT span=FolderConfigModal.tsx:235/275 + Opus: a terminal scan error
      // (retry:false) must be VISIBLE (ErrorNotice), must NOT dead-end a
      // name-only save, but MUST block Save when an agent is selected (the pick
      // can't be validated against the dir's real scope).
      mockAgents.mockImplementation((_sk?: string, dir?: string) =>
        dir === '/repo/ok'
          ? Promise.resolve({ agents: [{ name: 'repo-dev' }], default_agent: '' })
          : Promise.reject(new Error('scan 500')),
      )
      const { onSubmit } = open()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/err'))
      // The failure is surfaced, not silent.
      expect(await screen.findByTestId('folder-config-agent-roster-error')).toBeInTheDocument()
      expect(screen.getByTestId('folder-config-agent-roster-retry')).toBeInTheDocument()
      // Name-only (no agent): Save recovers to enabled and submits.
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
      )
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    })

    it('shows the existing loading hint while a roster retry is pending', async () => {
      let releaseRetry: ((v: { agents: { name: string }[]; default_agent: string }) => void) | undefined
      mockAgents
        .mockRejectedValueOnce(new Error('scan 500'))
        .mockImplementationOnce(
          () => new Promise(resolve => {
            releaseRetry = resolve
          }),
        )
      open()
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err' } })
      expect(await screen.findByTestId('folder-config-agent-roster-error')).toBeInTheDocument()

      fireEvent.click(screen.getByTestId('folder-config-agent-roster-retry'))
      await waitFor(() => expect(mockAgents).toHaveBeenCalledTimes(2))
      expect(await screen.findByTestId('folder-config-agent-roster-loading')).toHaveTextContent(
        /Loading agents/i,
      )
      expect(screen.queryByTestId('folder-config-agent-roster-error')).toBeNull()

      releaseRetry?.({ agents: [{ name: 'repo-dev' }], default_agent: '' })
      await waitFor(() =>
        expect(screen.queryByTestId('folder-config-agent-roster-loading')).not.toBeInTheDocument(),
      )
    })

    it('the orphan notice names no button absent from create mode', async () => {
      // Item 1 (design + ux): create mode's footer holds only Cancel / Create
      // folder — there is no "Save" button. The orphan notice must not tell the
      // user "Save is blocked" and send them scanning for a control that isn't
      // there. The wording is mode-neutral: it says the folder can't be saved
      // without naming a button.
      mockAgents.mockResolvedValue({ agents: [{ name: 'repo-dev' }], default_agent: '' })
      open() // create mode; global prop roster (AGENTS) lacks repo-dev
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/a' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      await pickAgent('repo-dev')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      // Clear the dir → repo-dev is orphaned under the (empty) scope, so the
      // rescope notice renders and Save is blocked.
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '' } })
      await screen.findByTestId('folder-config-agent-notice')
      // The defect: it named a "Save" button that create mode does not render.
      // The mode-neutral wording says the folder can't be saved without naming
      // a control.
      await waitFor(() =>
        expect(screen.getByTestId('folder-config-agent-notice')).toHaveTextContent(/can.t be saved/i),
      )
      const notice = screen.getByTestId('folder-config-agent-notice')
      expect(notice).not.toHaveTextContent(/Save is blocked/i)
      // Prove the mode really lacks a Save button: the footer submit is
      // "Create folder", and nothing named "Save" exists.
      expect(screen.getByTestId('folder-config-submit')).toHaveTextContent('Create folder')
      expect(screen.queryByRole('button', { name: /^Save/ })).toBeNull()
    })

    it('suppresses the default-agent hint while the roster-scan error row shows', async () => {
      // Item 2 (design + ux): "Pre-selected for new chats created here." is a
      // promise of pre-selection; rendered directly above "Couldn't read this
      // directory's agents." it undercuts the one row explaining the dead Save.
      // With no agent selected the else-branch hint would render alongside the
      // error row — it must be suppressed while the scan-error row is showing.
      mockAgents.mockRejectedValue(new Error('scan 500'))
      open() // create mode, no agent selected
      // Confirm the hint is present before any scan is triggered.
      expect(screen.getByText(/Pre-selected for new chats created here/i)).toBeTruthy()
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/err'))
      // The scan-error row appears...
      expect(await screen.findByTestId('folder-config-agent-roster-error')).toBeInTheDocument()
      // ...and the pre-selection hint is gone while it shows.
      await waitFor(() =>
        expect(screen.queryByText(/Pre-selected for new chats created here/i)).toBeNull(),
      )
    })

    it('blocks Save when an agent is selected and the roster scan errors', async () => {
      // Scope to a good dir, pick its agent, then re-scope to a dir whose scan
      // errors: the pick can't be validated, so Save is blocked (with the
      // error surfaced) until the user resolves it.
      mockAgents.mockImplementation((_sk?: string, dir?: string) =>
        dir === '/repo/ok'
          ? Promise.resolve({ agents: [{ name: 'repo-dev' }], default_agent: '' })
          : Promise.reject(new Error('scan 500')),
      )
      open()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/ok' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/ok'))
      await pickAgent('repo-dev')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/err'))
      expect(await screen.findByTestId('folder-config-agent-roster-error')).toBeInTheDocument()
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true),
      )
    })

    it('names the pick on a scan error instead of claiming "Inherit"', async () => {
      // UX finding: on a scan error `rosterUnsettled` suppresses `orphanAgent`, so
      // the pick left `agentOptions` and SimpleSelect fell back to its clear label
      // — the trigger read "Inherit (default)" while Save stayed disabled for an
      // agent the user could no longer see. An apparently valid form with a dead
      // Save and nothing naming the cause. The pick must stay visible, flagged as
      // unverified rather than absent: a failed scan cannot establish absence.
      mockAgents.mockImplementation((_sk?: string, dir?: string) =>
        dir === '/repo/ok'
          ? Promise.resolve({ agents: [{ name: 'repo-dev' }], default_agent: '' })
          : Promise.reject(new Error('scan 503')),
      )
      open()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/ok' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/ok'))
      await pickAgent('repo-dev')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/err'))
      expect(await screen.findByTestId('folder-config-agent-roster-error')).toBeInTheDocument()
      // The trigger still NAMES the selection...
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      // ...and never claims the folder inherits the default instead.
      expect(agentTrigger()).not.toHaveTextContent('Inherit')
    })

    it('keeps Save enabled on a scan error when the selected agent is a valid global agent', async () => {
      // Opus finding (FolderConfigModal.tsx:~394): on a scan error effectiveAgents
      // falls back to the GLOBAL installedAgents prop, so a selection that is a
      // valid global agent (e.g. an edit folder's saved default_agent, unrelated
      // to the failing project scan) is still validatable against what we can
      // see. Blocking it would hold a benign rename/recolor hostage — the
      // maintainer's "don't block round-trip edits" rule. Only an agent ABSENT
      // from the fallback roster is unvalidatable and blocks Save.
      mockAgents.mockRejectedValue(new Error('scan 500'))
      const { onSubmit } = open()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/err'))
      // Error is surfaced; the picker falls back to the global roster.
      expect(await screen.findByTestId('folder-config-agent-roster-error')).toBeInTheDocument()
      // Pick a GLOBAL agent (present in the fallback prop roster).
      await pickAgent('kirocrew-dev')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('kirocrew-dev'))
      // A validatable global pick does NOT block Save even while the scan errors.
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
      )
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      await waitFor(() => expect(onSubmit).toHaveBeenCalled())
      expect(onSubmit.mock.calls[0][0].defaultAgent).toBe('kirocrew-dev')
    })

    it('blocks Save while the re-scoped roster is still loading', async () => {
      // GPT span=FolderConfigModal.tsx:262: while a re-scope is loading, the
      // orphan flag is suppressed (to avoid falsely flagging a valid project
      // agent), so Save must be blocked until the scan settles — otherwise a
      // pick not in the new scope could be saved inside the debounce+scan window
      // (the backend does no agent-vs-scope check; this is the only guard).
      let resolveB: (v: { agents: { name: string }[]; default_agent: string }) => void = () => {}
      mockAgents.mockImplementation((_sk?: string, dir?: string) => {
        if (dir === '/repo/a') return Promise.resolve({ agents: [{ name: 'repo-dev' }], default_agent: '' })
        // Dir B's scan hangs until we release it — simulating the loading window.
        return new Promise(res => { resolveB = res })
      })
      open()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/a' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      await pickAgent('repo-dev')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      // Re-scope to B; its scan is in flight, so the orphan flag is suppressed —
      // Save must still be BLOCKED (the scan is pending), not enabled.
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/b' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/b'))
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true),
      )
      // Once B's scan resolves WITHOUT repo-dev, it becomes a visible orphan and
      // Save stays blocked (now on the orphan, not the scan).
      resolveB({ agents: [{ name: 'other-agent' }], default_agent: '' })
      await waitFor(() => expect(agentTrigger()).toHaveTextContent(/repo-dev.*not in this project/i))
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
    })

    it('flags and blocks Save when the dir is cleared and the agent is gone', async () => {
      mockAgents.mockResolvedValue({ agents: [{ name: 'repo-dev' }], default_agent: '' })
      open() // global prop roster is AGENTS (kirocrew, kirocrew-dev) — no repo-dev
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/a' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      await pickAgent('repo-dev')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'X' } })
      // Clear the dir → falls back to the global roster, which lacks repo-dev.
      // The pick is NOT silently cleared: it stays, flagged "(needs a project
      // directory)" (NOT "(not in this project)" — no project is set once the
      // dir is cleared, so naming one would be incoherent), and Save is
      // blocked.
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '' } })
      await waitFor(() => expect(agentTrigger()).toHaveTextContent(/repo-dev.*needs a project directory/i))
      expect(agentTrigger()).not.toHaveTextContent(/not in this project/i)
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
    })

    it('blocks Save on return to the seeded dir while a foreign pick re-validates', async () => {
      // GPT span=FolderConfigModal.tsx:354 (return-to-seed race): pick a foreign
      // agent under dir B, then change the dir BACK to the seeded value. The
      // effective dir now equals the seeded dir, so the dir-based rescope gate is
      // false — but the pick still differs from the seed, and staleTime:0 forces
      // a refetch of A whose in-flight window must keep Save blocked (via the
      // session-changed-pick arm of rescopeUnsettled), so a foreign agent cannot
      // be persisted for a folder scoped to A.
      let aCalls = 0
      let holdReturnA: (v: { agents: { name: string }[]; default_agent: string }) => void = () => {}
      mockAgents.mockImplementation((_sk?: string, dir?: string) => {
        if (dir === '/repo/b') return Promise.resolve({ agents: [{ name: 'repo-b-agent' }], default_agent: '' })
        aCalls += 1
        // First A scan resolves immediately; the RETURN refetch (2nd) is held
        // open so the in-flight window is deterministic.
        if (aCalls === 1) return Promise.resolve({ agents: [{ name: 'repo-dev' }], default_agent: '' })
        return new Promise(res => { holdReturnA = res })
      })
      const f = folder('f1', { name: 'Payments', project_dir: '/repo/a', default_agent: '' })
      open({ mode: 'edit', folder: f, folders: [f] })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      await waitFor(() => expect(agentTrigger()).not.toHaveTextContent(/not installed/i))
      // Go to B, pick B's agent.
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/b' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/b'))
      await pickAgent('repo-b-agent')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-b-agent'))
      // Return to seeded dir A: its refetch is held (in flight). The pick differs
      // from the seed (''), so Save stays blocked despite dir === seeded dir —
      // this is the return-to-seed race the fix closes.
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/a' } })
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true),
      )
      // Coherence check: releasing the held refetch does not itself enable Save (the pick
      // is not in A's roster). Post-resolve orphan-label rendering is covered by
      // the re-scope tests above; here we assert the block persists.
      holdReturnA({ agents: [{ name: 'repo-dev' }], default_agent: '' })
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true),
      )
    })

    it('keeps the selection when the re-scoped project still has that agent', async () => {
      // Both dirs expose repo-dev — a re-scope that still contains the pick must
      // NOT reset it.
      mockAgents.mockResolvedValue({ agents: [{ name: 'repo-dev' }], default_agent: '' })
      open()
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/a' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      await pickAgent('repo-dev')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/b' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/b'))
      // repo-dev is in B too, so it stays. Give the re-scope reconcile a tick,
      // then assert it did NOT clear the pick.
      await new Promise(r => setTimeout(r, 50))
      expect(agentTrigger()).toHaveTextContent('repo-dev')
    })

    it('flags and blocks Save on an INHERITED re-scope (parent dir changes)', async () => {
      // The effective project dir can change without the user touching the
      // draft's own projectDir — an ANCESTOR folder's project_dir changes and
      // this folder inherits it. When the new inherited scope lacks the picked
      // agent it is flagged "(not in this project)" and Save is blocked, so a
      // save cannot persist a pick the new scope does not contain.
      mockAgents.mockImplementation((_sk?: string, dir?: string) =>
        Promise.resolve({
          agents: dir === '/repo/parent-a' ? [{ name: 'repo-dev' }] : [],
          default_agent: '',
        }),
      )
      // Child inherits parent A's dir; draft's own projectDir stays empty.
      const parentA = folder('p1', { name: 'Parent', project_dir: '/repo/parent-a' })
      const { onSubmit, rerender } = open({
        parentId: 'p1',
        folders: [parentA],
      })
      await waitFor(() =>
        expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/parent-a'),
      )
      // Pick the inherited dir's project agent.
      await pickAgent('repo-dev')
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Sub' } })

      // The parent's dir changes to one with NO project agents — the child's
      // effective (inherited) dir changes though its own field never did.
      rerender(
        <FolderConfigModal
          open={true} mode="create" parentId="p1"
          folders={[folder('p1', { name: 'Parent', project_dir: '/repo/parent-b' })]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} onRetryTags={vi.fn()}
        />
      )
      // repo-dev is now orphaned under the inherited scope: flagged, Save blocked.
      await waitFor(() => expect(agentTrigger()).toHaveTextContent(/repo-dev.*not in this project/i))
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
      expect(onSubmit).not.toHaveBeenCalled()
    })

    it('preserves a saved orphan agent on open (edit mode), not treated as a re-scope', async () => {
      // Edit a folder whose saved default_agent is NOT in the project roster and
      // NOT global — opening must keep it (orphan round-trip), never clear it.
      mockAgents.mockResolvedValue({ agents: [{ name: 'repo-dev' }], default_agent: '' })
      const f = folder('f1', { name: 'Payments', project_dir: '/repo/a', default_agent: 'ghost-agent' })
      render(
        <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn().mockResolvedValue(undefined)}
          onRetryTags={vi.fn()} />
      )
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      // The saved orphan is still selected (round-trips), flagged not-installed.
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('ghost-agent'))
    })

    it('blocks Save when an EDIT folder is re-scoped to a dir lacking its SAVED agent', async () => {
      // GPT span=FolderConfigModal.tsx:330: the saved-orphan round-trip
      // exception (base #1182) is scoped to the SEEDED directory. Open an edit
      // folder seeded with repo-dev under dir A (so seedRef.current.defaultAgent
      // === 'repo-dev' AND seededEffectiveDir === '/repo/a'), then re-scope its
      // OWN projectDir to B, which lacks repo-dev. Here orphanAgent ===
      // seedRef.current.defaultAgent, so the seed-INEQUALITY arm of blockingOrphan
      // is false — this is the exact bypass GPT found. The dir-changed arm
      // (effectiveProjectDir !== seededEffectiveDir) must catch it: once B's scan
      // settles WITHOUT repo-dev, the pick is flagged "(not in this project)" and
      // Save is blocked, so it cannot persist repo-dev under a directory B lacks.
      mockAgents.mockImplementation((_sk?: string, dir?: string) =>
        Promise.resolve({
          agents: dir === '/repo/a' ? [{ name: 'repo-dev' }] : [{ name: 'other-agent' }],
          default_agent: '',
        }),
      )
      const f = folder('f1', { name: 'Payments', project_dir: '/repo/a', default_agent: 'repo-dev' })
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(
        <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
          installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} onRetryTags={vi.fn()} />
      )
      // A's roster settles with repo-dev present: seeded agent is valid, Save enabled.
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
      await waitFor(() => expect(agentTrigger()).toHaveTextContent('repo-dev'))
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
      )
      // Re-scope this folder's OWN dir to B, which lacks repo-dev.
      fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/b' } })
      await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/b'))
      // repo-dev (== seeded agent) is now orphaned under B: flagged, Save blocked.
      await waitFor(() => expect(agentTrigger()).toHaveTextContent(/repo-dev.*not in this project/i))
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true),
      )
      // Picking a valid agent for the new scope clears the block and saves it.
      await pickAgent('other-agent')
      await waitFor(() =>
        expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
      )
      fireEvent.click(screen.getByTestId('folder-config-submit'))
      expect(onSubmit.mock.calls[0][0].defaultAgent).toBe('other-agent')
    })

    describe('flattened inherited-agent sibling states (UX: nested parentheticals)', () => {
      // UX Review, stamped on the live head: the not-in-project inherited case
      // got a flattened em-dash string (inherit_named_not_in_project), but the
      // other three inherited-orphan states interpolated the whole composed
      // "Inherit (x)" label into agent_not_available / agent_not_installed /
      // agent_unverified, rendering nested parentheticals like
      // "Inherit (repo-dev) (not available)". Each case here is a child with NO
      // own defaultAgent (so the effective agent is purely the inherited one)
      // and asserts the trigger shows exactly ONE parenthesis pair with an em
      // dash, and never the old nested ") (" spelling.
      const NESTED_PAREN = ') ('

      it('flattens the dir-less inherited orphan to a single em-dash pair, not a nested label', async () => {
        // Parent pins an agent absent from the global roster; the child inherits
        // it with no project directory anywhere, so orphanDirless is true.
        const parent = folder('p1', { name: 'Parent', default_agent: 'ghost-agent' })
        open({ parentId: 'p1', folders: [parent] })
        await waitFor(() =>
          expect(agentTrigger()).toHaveTextContent(/ghost-agent.*needs a project directory/i),
        )
        expect(agentTrigger().textContent).not.toContain(NESTED_PAREN)
        expect(agentTrigger().textContent).toContain('—')
        expect((agentTrigger().textContent?.match(/\(/g) ?? []).length).toBe(1)
      })

      it('flattens the inherited orphan (not installed) to a single em-dash pair', async () => {
        // Edit a folder whose OWN saved default_agent was 'ghost-agent' (not in
        // any roster), then clear the pick to Inherit — the global default also
        // happens to be 'ghost-agent', so the inherited effective agent equals
        // the seeded own pick (orphanIsRescopeOnly is false) and the dir stays
        // set (not orphanDirless), landing on the "(not installed)" branch.
        mockAgents.mockResolvedValue({ agents: [{ name: 'repo-dev' }], default_agent: '' })
        const f = folder('f1', {
          name: 'Sub', project_dir: '/repo/a', default_agent: 'ghost-agent',
        })
        open({ mode: 'edit', folder: f, folders: [f], globalDefaultAgent: 'ghost-agent' })
        await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
        await waitFor(() => expect(agentTrigger()).toHaveTextContent('ghost-agent'))
        await pickAgent('Inherit (ghost-agent)')
        await waitFor(() =>
          expect(agentTrigger()).toHaveTextContent(/ghost-agent.*not installed/i),
        )
        expect(agentTrigger().textContent).not.toContain(NESTED_PAREN)
        expect(agentTrigger().textContent).toContain('—')
        expect((agentTrigger().textContent?.match(/\(/g) ?? []).length).toBe(1)
      })

      it('flattens the inherited orphan (unverified) to a single em-dash pair on a roster scan error', async () => {
        // Edit a folder whose OWN saved default_agent was 'ghost-agent' (absent
        // from the global fallback roster too), then clear the pick to Inherit
        // — the global default is also 'ghost-agent', so the inherited
        // effective agent is what unvalidatableAgent gates on once the
        // folder's OWN project dir scan errors (orphanAgent stays '' on a scan
        // error regardless, per rosterUnsettled).
        mockAgents.mockRejectedValue(new Error('scan 500'))
        const f = folder('f1', {
          name: 'Sub', project_dir: '/repo/err', default_agent: 'ghost-agent',
        })
        open({ mode: 'edit', folder: f, folders: [f], globalDefaultAgent: 'ghost-agent' })
        await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/err'))
        expect(await screen.findByTestId('folder-config-agent-roster-error')).toBeInTheDocument()
        await waitFor(() => expect(agentTrigger()).toHaveTextContent('ghost-agent'))
        await pickAgent('Inherit (ghost-agent)')
        await waitFor(() =>
          expect(agentTrigger()).toHaveTextContent(/ghost-agent.*can't verify/i),
        )
        expect(agentTrigger().textContent).not.toContain(NESTED_PAREN)
        expect(agentTrigger().textContent).toContain('—')
        expect((agentTrigger().textContent?.match(/\(/g) ?? []).length).toBe(1)
      })
    })
  })

  describe('footer hint dims beside a disabled Save (UX Review)', () => {
    // The "Enter to submit" hint renders unconditionally regardless of
    // canSubmit — pressing Enter while Save is disabled does nothing, so a
    // full-strength hint keeps promising a shortcut that is dead. Assert the
    // rendered element's class, not a snapshot, so the test fails on the
    // actual visual token rather than on incidental markup.
    function hint() {
      return screen.getByText('Enter to submit')
    }

    it('renders the dimmed muted token while Save is disabled (no name entered)', () => {
      open()
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
      expect(hint()).toHaveClass('text-muted')
      expect(hint()).not.toHaveClass('text-muted-strong')
    })

    it('renders the normal (non-dimmed) token once a name makes Save enabled', () => {
      open()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
      expect(hint()).toHaveClass('text-muted-strong')
      expect(hint()).not.toHaveClass('text-muted')
    })

    it('keeps the mr-auto spacer in both states so Cancel/Save do not move', () => {
      open()
      expect(hint()).toHaveClass('mr-auto')
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      expect(hint()).toHaveClass('mr-auto')
    })

    it('never hides the hint outright — it stays in the document in both states', () => {
      open()
      expect(hint()).toBeInTheDocument()
      fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
      expect(hint()).toBeInTheDocument()
    })
  })
})
