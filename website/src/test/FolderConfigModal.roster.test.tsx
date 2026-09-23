import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import FolderConfigModal from '../components/FolderConfigModal'
import { ApiError } from '../api/apiError'
import { api } from '../api/client'
import { render, folder, AGENTS, open, agentTrigger, openAgents, pickAgent } from './folderConfigModal.testHelpers'

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
    // The orphan notice is a client-side validation hint, so AUTOSDE's
    // blocking errors-use-error-notice rule keeps it off ErrorNotice even
    // when it blocks Save: warn tone, no alert role. Save is gated by
    // canSubmit (the disabled assertion above), not by the notice surface.
    const blockingNotice = screen.getByTestId('folder-config-agent-notice')
    expect(blockingNotice).not.toHaveAttribute('role', 'alert')
    expect(blockingNotice).toHaveClass('text-warn')
    expect(agentTrigger().getAttribute('aria-describedby')).toBe('folder-config-agent-notice')
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

  it('keeps Save enabled for a renamed child whose untouched inherited agent is absent', async () => {
    // Design span 40df5a0fc771: an inheriting child's own seed is '', so an
    // inherited agent missing from the settled roster must not read as an
    // in-session agent change. A rename sends no agent, directory or parent.
    mockAgents.mockResolvedValue({ agents: [{ name: 'other-agent' }], default_agent: '' })
    const parent = folder('parent', {
      name: 'Parent', project_dir: '/repo/a', default_agent: 'parent-agent',
    })
    const child = folder('child', {
      name: 'Child', parent_id: 'parent', project_dir: '/repo/a', default_agent: '',
    })
    const { onSubmit } = open({ mode: 'edit', folder: child, folders: [parent, child] })
    await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/a'))
    // The inherited agent really is absent from the settled roster: flagged.
    await screen.findByTestId('folder-config-agent-notice')
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Renamed' } })
    await waitFor(() =>
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
    )
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].touched).toEqual(['name'])
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

  // Decision B: a 503 carrying `code: "scan_unsupported_platform"` is a
  // PERMANENT platform limitation -- no retry can succeed, so the modal shows
  // the unsupported copy and withholds Retry (offering one is the exact lie the
  // brief exists to close). Any other failure (pin, audit-write, entry-count
  // overflow) keeps the ordinary Retry row. The two states are mutually
  // exclusive and must never collapse into one, in either direction. Either
  // way a name-only folder (no agent) still saves.
  const unsupportedErr = () => new ApiError(
    503,
    'folder-level project-agent selection is not supported on this platform',
    JSON.stringify({
      error: 'folder-level project-agent selection is not supported on this platform',
      code: 'scan_unsupported_platform',
    }),
  )
  // Waits for the scan of `dir` to have FIRED and then SETTLED (loading row
  // gone), so every assertion after it reads the terminal error state rather
  // than the transient in-flight window.
  const settledScan = async (dir: string) => {
    await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, dir))
    await waitFor(() =>
      expect(screen.queryByTestId('folder-config-agent-roster-loading')).toBeNull(),
    )
  }

  it('renders the transient scan-error row with Retry and still saves a name-only folder', async () => {
    mockAgents.mockRejectedValue(new ApiError(503, 'project roster scan could not be verified', ''))
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
    fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err' } })
    await settledScan('/repo/err')
    // Shown even with nothing blocked: Retry is the only in-modal way back to
    // this directory's project agents.
    expect(screen.getByTestId('folder-config-agent-roster-error')).toHaveTextContent(/couldn.t read this directory.s agents/i)
    expect(screen.queryByTestId('folder-config-agent-roster-unsupported')).toBeNull()
    expect(screen.getByTestId('folder-config-agent-roster-retry')).toBeInTheDocument()
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
  })

  it('surfaces the unsupported-platform explanation and keeps the hint when nothing is blocked', async () => {
    // GPT 6.1 + UX: the failed request must remain visible through ErrorNotice,
    // but a permanent platform limitation must not displace the ordinary hint
    // or claim that Save is blocked when no agent is selected.
    mockAgents.mockRejectedValue(unsupportedErr())
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments' } })
    fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err' } })
    await settledScan('/repo/err')
    const notice = screen.getByTestId('folder-config-agent-roster-unsupported')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent(/can.t be read on Windows yet/i)
    expect(notice).toHaveTextContent(/Global agents still work/i)
    expect(notice).not.toHaveTextContent(/Pick a global agent to save this folder/i)
    expect(screen.queryByTestId('folder-config-agent-roster-error')).toBeNull()
    expect(screen.queryByTestId('folder-config-agent-roster-retry')).toBeNull()
    expect(screen.getByText(/Pre-selected for new chats created here/i)).toBeTruthy()
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
  })

  it('shows the way out while blocked, then keeps the compact explanation on a global pick', async () => {
    // A create child inherits a PROJECT agent the failed scan cannot validate:
    // Save is dead, so the row says how to unblock it, with no Retry. Picking
    // a global agent unblocks Save; the same failed request remains surfaced,
    // but switches to explanation-only copy beside the normal hint.
    mockAgents.mockRejectedValue(unsupportedErr())
    const parent = folder('p1', { name: 'Parent', project_dir: '/repo/err', default_agent: 'repo-dev' })
    open({ parentId: 'p1', folders: [parent] })
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Child' } })
    await settledScan('/repo/err')
    const row = screen.getByTestId('folder-config-agent-roster-unsupported')
    expect(row).toHaveTextContent(/can.t be read on Windows yet/i)
    expect(row).toHaveTextContent(/Pick a global agent to save this folder/i)
    expect(row).toHaveAttribute('role', 'alert')
    // The trigger one line above names the same cause as the row: Windows.
    expect(agentTrigger()).toHaveTextContent(/repo-dev.*not supported on Windows yet/i)
    expect(agentTrigger()).not.toHaveTextContent(/this platform/i)
    expect(screen.queryByTestId('folder-config-agent-roster-error')).toBeNull()
    expect(screen.queryByTestId('folder-config-agent-roster-retry')).toBeNull()
    expect(screen.queryByText(/Pre-selected for new chats created here/i)).toBeNull()
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
    await pickAgent('kirocrew-dev')
    await waitFor(() => expect(agentTrigger()).toHaveTextContent('kirocrew-dev'))
    await waitFor(() =>
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
    )
    expect(screen.getByTestId('folder-config-agent-roster-unsupported')).toHaveTextContent(
      /Global agents still work/i,
    )
    expect(screen.queryByTestId('folder-config-agent-roster-retry')).toBeNull()
    expect(screen.getByText(/Pre-selected for new chats created here/i)).toBeTruthy()
  })

  it('keeps the explanation for an untouched EDIT rename and strengthens it once the dir changes', async () => {
    // The untouched round-trip remains savable, so it gets explanation-only
    // copy beside the normal hint. Re-scoping makes the saved project agent
    // unvalidatable; only then does the copy name the required global pick.
    mockAgents.mockRejectedValue(unsupportedErr())
    const f = folder('f1', { name: 'Payments', project_dir: '/repo/err', default_agent: 'repo-dev' })
    open({ mode: 'edit', folder: f, folders: [f] })
    await settledScan('/repo/err')
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments v2' } })
    await waitFor(() =>
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
    )
    expect(screen.getByTestId('folder-config-agent-roster-unsupported')).toHaveTextContent(
      /Global agents still work/i,
    )
    expect(screen.getByText(/Pre-selected for new chats created here/i)).toBeTruthy()
    fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err2' } })
    await settledScan('/repo/err2')
    expect(screen.getByTestId('folder-config-agent-roster-unsupported')).toHaveTextContent(
      /Pick a global agent to save this folder/i,
    )
    expect(screen.queryByText(/Pre-selected for new chats created here/i)).toBeNull()
    expect(agentTrigger()).toHaveTextContent(/repo-dev.*not supported on Windows yet/i)
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
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

  it('keeps Save enabled for an EDIT rename on a scan error when the saved agent and dir are untouched', async () => {
    // UX Review: `unvalidatableAgent` lacked the untouched-agent round-trip
    // exception `blockingOrphan` already has, so opening a folder that pins a
    // PROJECT agent while its roster scan errors (any query failure — a 500,
    // a timeout — not only scan_unsupported_platform) left Save dead for a
    // plain rename. Such a save sends no agent or directory, so nothing needs
    // validating. A changed DIRECTORY must still block (asserted below).
    mockAgents.mockRejectedValue(new Error('scan 500'))
    const f = folder('f1', { name: 'Payments', project_dir: '/repo/err', default_agent: 'repo-dev' })
    const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
    await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/err'))
    expect(await screen.findByTestId('folder-config-agent-roster-error')).toBeInTheDocument()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Payments v2' } })
    await waitFor(() =>
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
    )
    // Re-scoping the directory makes the pinned agent unvalidatable again.
    fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err2' } })
    await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/err2'))
    // Assert only once the scan has SETTLED into its terminal error, so the
    // block comes from `unvalidatableAgent`, not the transient in-flight guard.
    await waitFor(() => {
      expect(screen.queryByTestId('folder-config-agent-roster-loading')).toBeNull()
      expect(screen.getByTestId('folder-config-agent-roster-error')).toBeInTheDocument()
    })
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
    // Back at the seeded dir, the untouched rename round-trips and submits.
    fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err' } })
    await waitFor(() =>
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
    )
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0]).toEqual(expect.objectContaining({
      name: 'Payments v2', defaultAgent: 'repo-dev', projectDir: '/repo/err',
    }))
  })

  it('keeps an EDIT child inheriting a project agent savable on a scan error only while untouched', async () => {
    // The inherited effective agent is still validated (an empty own field
    // does not mean no agent runs): clearing nothing, a rename round-trips;
    // overriding the inherited dir blocks.
    mockAgents.mockRejectedValue(new Error('scan 500'))
    const parent = folder('p1', { name: 'Parent', project_dir: '/repo/err', default_agent: 'repo-dev' })
    const child = folder('c1', { name: 'Child', parent_id: 'p1' })
    open({ mode: 'edit', folder: child, folders: [parent, child] })
    await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/err'))
    expect(await screen.findByTestId('folder-config-agent-roster-error')).toBeInTheDocument()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Child v2' } })
    await waitFor(() =>
      expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(false),
    )
    fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/err2' } })
    await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/err2'))
    // Assert only once the scan has SETTLED into its terminal error, so the
    // block comes from `unvalidatableAgent`, not the transient in-flight guard.
    await waitFor(() => {
      expect(screen.queryByTestId('folder-config-agent-roster-loading')).toBeNull()
      expect(screen.getByTestId('folder-config-agent-roster-error')).toBeInTheDocument()
    })
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
  })

  it('never lets the orphan notice say "can\'t be saved" while Save is actually enabled', async () => {
    // Regression pin: `orphanIsRescopeOnly`/`orphanDirless` read only the
    // folder's OWN seeded `defaultAgent` (always '' for an inheriting child
    // with no own pick), so they always classify an inherited orphan as a
    // "rescope" and render the "can't be saved" notice copy — even on the
    // ONE combination `blockingOrphan` deliberately exempts: an EDIT child
    // that inherits an orphaned agent, left genuinely UNTOUCHED since open.
    // `blockingOrphan` reads the seeded EFFECTIVE agent (inheritance-aware)
    // and is false there, so Save stays enabled while the notice claimed
    // the opposite. Assert the RELATIONSHIP (notice text vs. Save state),
    // not a fixed string, so this pins the invariant through a reword.
    mockAgents.mockResolvedValue({ agents: [{ name: 'other-dev' }], default_agent: '' })
    const parent = folder('p1', { name: 'Parent', project_dir: '/repo/other', default_agent: 'repo-dev' })
    const child = folder('c1', { name: 'Child', parent_id: 'p1' })
    open({ mode: 'edit', folder: child, folders: [parent, child] })
    await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/other'))

    const notice = await screen.findByTestId('folder-config-agent-notice')
    const submit = screen.getByTestId('folder-config-submit') as HTMLButtonElement
    // The invariant under test: whenever this notice names the agent at
    // all, it must not say the folder can't be saved unless Save is
    // actually disabled. Here it is not — the child was left untouched —
    // so the old copy's claim is live and false until this is fixed.
    if (/can.t be saved/i.test(notice.textContent ?? '')) {
      expect(submit.disabled).toBe(true)
    }
    expect(submit.disabled).toBe(false)

    // Touching the directory (any genuine rescope) is a different state —
    // the untouched exception no longer applies, Save blocks, and the
    // "can't be saved" copy becomes true again.
    fireEvent.change(screen.getByTestId('folder-config-project-dir'), { target: { value: '/repo/third' } })
    await waitFor(() => expect(mockAgents).toHaveBeenCalledWith(undefined, '/repo/third'))
    await waitFor(() => expect(submit.disabled).toBe(true))
    const rescopedNotice = await screen.findByTestId('folder-config-agent-notice')
    expect(rescopedNotice.textContent ?? '').toMatch(/can.t be saved/i)
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
    // The pick can't be validated against the failed scope, so Save is
    // blocked. Asserted with the error row up, i.e. once the scan has SETTLED
    // (the loading row outranks it while in flight).
    expect((screen.getByTestId('folder-config-submit') as HTMLButtonElement).disabled).toBe(true)
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
    // Also the other half of the no-agent flicker fix: gating rescopeUnsettled
    // on a selected agent must not weaken this in-flight block.
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
