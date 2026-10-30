import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import CommandBarOverlay from './CommandBarOverlay'

/**
 * The launcher rows an installed app contributes (`contributes.commands`), rendered.
 * `contributedCommands.test.ts` covers the declaration rules on their own.
 *
 * The mocks and `resolvingDispatch` below are a word-for-word copy of the ones in
 * `CommandBarOverlay.test.tsx`, because a `vi.mock` applies only to the file that calls
 * it. A change to one copy belongs in the other.
 */

const dispatch = vi.fn()
const navigate = vi.fn()
const newSessionWithToken = vi.fn()
const enterInsertOrNewSession = vi.fn()

/** Live store the overlay reads for the attention section. Mutated per test. */
const storeState: {
  dashboard: { slots: Record<string, unknown>[]; unreadSlots: string[] }
  chat: { slotStatusDetail: Record<string, unknown>; activeSlot: string | null }
} = {
  dashboard: { slots: [], unreadSlots: [] },
  chat: { slotStatusDetail: {}, activeSlot: null },
}

vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
  // The session rows read the active slot through a store HANDLE once their
  // switch settles; the same fixture state answers that read.
  useAppStore: () => ({ getState: () => storeState }),
}))
vi.mock('../../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  setPendingInput: (text: string) => ({ type: 'setPendingInput', text }),
  // Mirrors the real thunk's `SwitchSlotArg`: a bare key, or an options object whose
  // `keepTargetOnMissing` decides whether a 404 unwinds to the previous slot.
  switchSlot: (arg: string | { key: string; keepTargetOnMissing?: boolean }) =>
    typeof arg === 'string' ? { type: 'switchSlot', key: arg } : { type: 'switchSlot', ...arg },
  requestFolderReveal: (folderId: string) => ({ type: 'requestFolderReveal', folderId }),
  requestSlotFolderOpen: (arg: Record<string, unknown>) => ({ type: 'requestSlotFolderOpen', ...arg }),
}))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({ navigate, enterInsertOrNewSession, newSessionWithToken }),
}))
const sessionSearch = vi.fn(async () => [] as unknown[])
vi.mock('../../components/commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => ({ search: sessionSearch }),
}))
const recentsSearch = vi.fn(async () => [] as unknown[])
// `sessionStatus` is kept REAL: it is the pure classifier that decides which slots
// owe the user something, and a stubbed one would let the attention section pass
// its tests while disagreeing with every other surface about what "needs input"
// means.
vi.mock('../../components/commandPalette/providers/recentsProvider', async importOriginal => ({
  ...(await importOriginal<typeof import('../../components/commandPalette/providers/recentsProvider')>()),
  useRecentsProvider: () => ({ search: recentsSearch }),
}))
// Passed through, and watched: a settings row must be scored by the scorer the
// other settings searches share, not by the bar's own field matcher.
const scoreSettingEntry = vi.hoisted(() => ({ spy: null as null | ReturnType<typeof vi.fn> }))
vi.mock('../../components/commandPalette/settingsSearchCore', async importOriginal => {
  const real = await importOriginal<typeof import('../../components/commandPalette/settingsSearchCore')>()
  scoreSettingEntry.spy = vi.fn(real.scoreSettingEntry)
  return { ...real, scoreSettingEntry: (...args: Parameters<typeof real.scoreSettingEntry>) => scoreSettingEntry.spy!(...args) }
})
vi.mock('../../hooks/useVisualViewport', () => ({ useVisualViewport: () => ({ height: 800 }) }))
vi.mock('../../hooks/useDialogFocusTrap', () => ({ useDialogFocusTrap: () => {} }))
const cycleTheme = vi.fn()
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: cycleTheme }) }))
// Only the filing call is stubbed — it talks to the folder API. `commandFolderName`
// stays REAL: it decides what the folder is called, which is exactly what these tests
// assert, and a stub would let them agree with themselves.
const fileSessionInCommandFolder = vi.fn(async () => 'folder-1')
vi.mock('./sessionFolder', async importOriginal => ({
  ...(await importOriginal<typeof import('./sessionFolder')>()),
  fileSessionInCommandFolder: (...args: unknown[]) => fileSessionInCommandFolder(...(args as [])),
}))

/** Resolve the promise `createSlot` dispatch is expected to produce. */
const resolvingDispatch = () => dispatch.mockReturnValue({ unwrap: () => Promise.resolve('slot-1') })

/**
 * Commands contributed by an installed app — the seam that lets a launcher row live
 * outside this repository.
 *
 * What is worth pinning here is not that a row renders. It is that the host stays in
 * charge of a declaration it did not write: a malformed contribution is skipped
 * rather than taking the Cmd+K gesture down for every app, an argument is checked
 * against the app's own pattern BEFORE a session exists, and a command that sends
 * its prompt shows the reader that prompt first.
 */
describe('CommandBarOverlay contributed commands', () => {
  beforeEach(() => {
    dispatch.mockReset()
    navigate.mockReset()
    sessionSearch.mockReset()
    sessionSearch.mockResolvedValue([])
    recentsSearch.mockReset()
    recentsSearch.mockResolvedValue([])
    enterInsertOrNewSession.mockReset()
    newSessionWithToken.mockReset()
    cycleTheme.mockReset()
    fileSessionInCommandFolder.mockClear()
    storeState.dashboard = { slots: [], unreadSlots: [] }
    storeState.chat = { slotStatusDetail: {}, activeSlot: null }
    window.localStorage.clear()
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  const LINK = 'https://github.com/kirodotdev/KiroCrew/pulls?q=is%3Aopen'

  /** A well-formed argument-taking command, as an app would declare it. */
  const APPROVE_ALL = {
    id: 'approve-all',
    title: 'Approve all PRs',
    subtitle: 'Approve every pull request behind a link',
    icon: 'Check',
    keywords: ['pr', 'lgtm'],
    prompt: 'Approve every pull request behind {argument}. Skip any I authored.',
    autoSend: true,
    argument: {
      placeholder: 'Paste a GitHub link…',
      hint: 'A PR search, a label, or a single pull request.',
      kind: 'url',
      hosts: ['github.com'],
      patternError: 'Not a GitHub link.',
    },
  }

  /** A command that needs nothing: activating it IS the action. */
  const STANDUP = {
    id: 'standup',
    title: 'Write my standup',
    prompt: 'Summarise what I did yesterday.',
  }

  const appWith = (commands: unknown, over: Record<string, unknown> = {}) => ({
    name: 'pr-bulk-ops',
    displayName: 'PR Bulk Ops',
    enabled: true,
    origin: 'registry',
    manifest: { contributes: { commands } },
    ...over,
  })

  /** Mount with the shared `['apps']` cache seeded, which is how the bar reads apps. */
  function mountWithApps(apps: unknown[], onClose = vi.fn()) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], apps)
    render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    return onClose
  }

  const optionByTitle = (re: RegExp) =>
    screen.getAllByRole('option').find(el => re.test(el.textContent ?? '')) as HTMLElement

  function enterCommand(title: RegExp) {
    fireEvent.mouseDown(optionByTitle(title))
    return screen.getByRole('combobox') as HTMLInputElement
  }

  it('renders a command an installed app contributed', () => {
    mountWithApps([appWith([APPROVE_ALL])])
    const row = optionByTitle(/Approve all PRs/)
    expect(row).toBeTruthy()
    // The app's own subtitle, so the row says which app put it there.
    expect(row.textContent).toContain('Approve every pull request behind a link')
  })

  it('contributes nothing while the app is disabled', () => {
    // The enable switch is the reader's control over the whole app; a row that still
    // ran from a disabled app would make that switch a lie.
    mountWithApps([appWith([APPROVE_ALL], { enabled: false })])
    expect(optionByTitle(/Approve all PRs/)).toBeUndefined()
  })

  it('skips a malformed contribution without taking the launcher down', () => {
    // A third party must not be able to break the Cmd+K gesture for every other app.
    mountWithApps([
      appWith([
        { ...APPROVE_ALL, id: 'Approve_All' }, // id is not a kebab slug
        { ...APPROVE_ALL, id: 'no-prompt', prompt: '' },
        { ...APPROVE_ALL, id: 'bad-kind', argument: { ...APPROVE_ALL.argument, kind: 'regex' } },
        { ...APPROVE_ALL, id: 'bad-hosts', argument: { kind: 'text', hosts: ['github.com'] } },
        { ...APPROVE_ALL, id: 'orphan-token', argument: undefined },
        STANDUP,
      ]),
    ])
    // Every bad entry is gone; the good one in the same array still renders.
    expect(optionByTitle(/Approve all PRs/)).toBeUndefined()
    expect(optionByTitle(/Write my standup/)).toBeTruthy()
    // And the builtin rows are untouched.
    expect(optionByTitle(/New Session/)).toBeTruthy()
  })

  it('survives contributes.commands not being an array', () => {
    mountWithApps([appWith({ approve: APPROVE_ALL })])
    expect(optionByTitle(/New Session/)).toBeTruthy()
  })

  it('namespaces a contributed row so it cannot impersonate a builtin', () => {
    // A contribution claiming the New Session id would otherwise inherit its
    // frecency record and its place in the list.
    mountWithApps([appWith([{ ...APPROVE_ALL, id: 'new-session' }])])
    const rows = screen.getAllByRole('option').map(el => el.textContent ?? '')
    expect(rows.filter(t => /New Session/.test(t))).toHaveLength(1)
    expect(optionByTitle(/Approve all PRs/)).toBeTruthy()
  })

  it('does not lead the empty query with a command that needs an argument', () => {
    // A launcher pre-highlights its first row, and a row that cannot act until it is
    // given a value has nothing to offer a bar that just opened.
    mountWithApps([appWith([APPROVE_ALL])])
    expect(screen.getAllByRole('option')[0].textContent ?? '').not.toMatch(/Approve all PRs/)
  })

  it('the first Enter collects the argument instead of acting', () => {
    resolvingDispatch()
    mountWithApps([appWith([APPROVE_ALL])])
    enterCommand(/Approve all PRs/)
    expect(screen.queryAllByRole('option')).toHaveLength(0)
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: expect.objectContaining({ activate: false }) })
    expect(navigate).not.toHaveBeenCalled()
  })

  it('runs a command with no argument straight away', () => {
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-new' }) })
    mountWithApps([appWith([STANDUP])])
    enterCommand(/Write my standup/)
    expect(dispatch).toHaveBeenCalledWith({
      type: 'createSlot',
      arg: { activate: false, sessionFolder: false, memory_mode: 'persistent' },
    })
  })

  it("files the session it opened under the command's own folder", async () => {
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-new' }) })
    mountWithApps([appWith([STANDUP])])
    enterCommand(/Write my standup/)
    await waitFor(() => expect(fileSessionInCommandFolder).toHaveBeenCalled())
    const [slotKey, folderName] = fileSessionInCommandFolder.mock.calls[0] as unknown as [
      string,
      string,
    ]
    expect(slotKey).toBe('slot-new')
    // The row's own title, so a second command lands in a folder of its own rather
    // than sharing one with every other row this app contributed.
    expect(folderName).toBe('Write my standup')
  })

  it('opens the folder path it filed the run in, while the run is still in view', async () => {
    // The command's folder can be collapsed after an earlier run, which would leave
    // the session on screen with no visible row. The request waits for the filing,
    // the only place the folder id is known, and the reducer drops it if the reader
    // has moved to another session by then.
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-new' }) })
    mountWithApps([appWith([STANDUP])])
    enterCommand(/Write my standup/)
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({
      type: 'requestSlotFolderOpen', key: 'slot-new', folder_id: 'folder-1', onlyIfActive: true,
    }))
  })

  it('asks for no folder path when the filing did not land', async () => {
    fileSessionInCommandFolder.mockResolvedValueOnce(null as never)
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-new' }) })
    mountWithApps([appWith([STANDUP])])
    enterCommand(/Write my standup/)
    await waitFor(() => expect(fileSessionInCommandFolder).toHaveBeenCalled())
    await act(async () => {})
    expect(dispatch).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'requestSlotFolderOpen' }))
  })

  it('files an argument-taking command under its title too', async () => {
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-approve' }) })
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(fileSessionInCommandFolder).toHaveBeenCalled())
    expect(fileSessionInCommandFolder.mock.calls[0]?.[1]).toBe('Approve all PRs')
  })

  it('files nothing when the seed was abandoned mid-flight', async () => {
    // The prompt never became a message, so there is no session worth filing -- and a
    // folder named after a command that did not run is worse than an unfiled session.
    let resolveSlot: (v: unknown) => void = () => {}
    dispatch.mockReturnValue({ unwrap: () => new Promise(res => (resolveSlot = res)) })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], [appWith([STANDUP])])
    const onClose = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    enterCommand(/Write my standup/)
    // Dismissed while the create is still in flight, which revokes the run's claim.
    view.rerender(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open={false} onClose={onClose} />
      </QueryClientProvider>,
    )
    await act(async () => {
      resolveSlot({ key: 'slot-abandoned' })
      // flake-ok: pre-existing microtask flush inside act(), moved verbatim; tracked for a de-flake pass
      await Promise.resolve()
    })
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'setPendingInput', text: STANDUP.prompt })
    expect(fileSessionInCommandFolder).not.toHaveBeenCalled()
  })

  it('creates the seeded session as the agent the command names', () => {
    // Always as a TEMPLATE: a host crew member with the same alias must not win
    // resolution and run the app's prompt with its template and private memory.
    // `createSlot` already accepted `agent`; the row only selects WHICH tool-enabled
    // agent receives the app-authored prompt. '' / absent both mean the dashboard
    // default, which the no-agent test above pins (undefined agent === no key).
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-new' }) })
    mountWithApps([appWith([{ ...STANDUP, agent: 'ticket-analyst' }])])
    enterCommand(/Write my standup/)
    expect(dispatch).toHaveBeenCalledWith({
      type: 'createSlot',
      arg: { activate: false, agent: 'ticket-analyst', agent_kind: 'template', appAgentOwner: 'pr-bulk-ops', sessionFolder: false, memory_mode: 'persistent' },
    })
  })

  it("refuses a value the app's own pattern rejects, creating nothing", async () => {
    resolvingDispatch()
    const onClose = mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: 'https://gitlab.com/g/p/-/merge_requests/1' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    // The app supplies the message, because only the app knows what shape it wanted.
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Not a GitHub link.'))
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: expect.objectContaining({ activate: false }) })
    expect(onClose).not.toHaveBeenCalled()
    expect(input.value).toBe('https://gitlab.com/g/p/-/merge_requests/1')
  })

  it('refuses to fire a command whose app was disabled while the field was open', async () => {
    // The field stays open across an arbitrary pause -- the reader is pasting a link --
    // and the app can be switched off from the Apps page in another tab meanwhile. The
    // row disappears at once, but the command object captured when the field opened
    // would not, so a snapshot-trusting submit would run the prompt of an app the
    // reader had just disabled.
    resolvingDispatch()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], [appWith([APPROVE_ALL])])
    const onClose = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })

    // The app is disabled underneath the open argument field.
    client.setQueryData(['apps'], [appWith([APPROVE_ALL], { enabled: false })])
    view.rerender(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )

    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toContain('no longer available'),
    )
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: expect.objectContaining({ activate: false }) })
  })

  it('refuses to send a prompt that differs from the one it previewed', async () => {
    // Re-resolving at submit fixed a stale snapshot firing after its app was disabled,
    // but the preview still renders the snapshot -- so an app whose prompt changes while
    // the field is open would have the reader consenting to one instruction and sending
    // another. The preview refreshes and nothing goes out; the next Enter acts on what is
    // now on screen.
    resolvingDispatch()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], [appWith([APPROVE_ALL])])
    const onClose = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })

    // The app rewrites the prompt underneath the open field.
    client.setQueryData(['apps'], [
      appWith([{ ...APPROVE_ALL, prompt: 'Delete every branch behind {argument}.' }]),
    ])
    view.rerender(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )

    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('changed'))
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: expect.objectContaining({ activate: false }) })
    // The refreshed preview shows the new text, so the next Enter is informed.
    expect(screen.getByText(/Delete every branch behind https/)).toBeTruthy()
  })

  it('revokes an in-flight activation when the argument state is left', async () => {
    // Escape abandons the question but leaves the bar open, so the close-edge and unmount
    // revokes never fire. Without a revoke here, a slow session create started by Enter
    // resolves afterwards and seeds an auto-sent session the reader already cancelled.
    //
    // Only `createSlot` is held. Holding every dispatch would stall the chain at
    // `switchSlot` and the test would pass whether or not the revoke works — which is
    // exactly what the first version of it did.
    let release: ((v: unknown) => void) | undefined
    dispatch.mockImplementation((action: { type?: string }) => ({
      unwrap: () =>
        action?.type === 'createSlot'
          ? new Promise(r => {
              release = r
            })
          : Promise.resolve('ok'),
    }))
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // Step back out while the create is still in flight, then let it land.
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })
    release?.({ key: 'slot-new' })
    await waitFor(() => expect(release).toBeDefined())
    // flake-ok: pre-existing microtask flush before an absence check, moved verbatim; tracked for a de-flake pass
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(dispatch).not.toHaveBeenCalledWith({ type: 'setPendingInput', text: expect.anything() })
    expect(navigate).not.toHaveBeenCalledWith('/chat?autoSend=1')
  })

  it('keeps the fresh slot selected through its own 404 so the seed cannot land elsewhere', async () => {
    // `switchSlot.rejected` treats a 404 as "target is gone" and restores the slot it
    // came from (#6309). Without `keepTargetOnMissing`, a create/fetch race on the slot
    // just created would put the reader back in their PREVIOUS conversation and then
    // auto-send a bulk instruction into it.
    dispatch.mockImplementation((action: { type?: string }) => ({
      unwrap: () =>
        action?.type === 'switchSlot'
          ? Promise.reject(Object.assign(new Error('not found'), { status: 404 }))
          : Promise.resolve({ key: 'slot-new' }),
    }))
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({
        type: 'switchSlot',
        key: 'slot-new',
        keepTargetOnMissing: true,
      }),
    )
    // The rejection is survivable: the seed still happens, on the slot just created.
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({ type: 'setPendingInput', text: expect.stringContaining(LINK) }),
    )
  })

  it('abandons an in-flight seed when its app is disabled during session creation', async () => {
    // `owned()` tracks the dialog's lifetime and cannot see this: disabling the app from
    // the Apps page while the create is still awaiting leaves the run legitimately owned
    // and the command gone, so the disabled app's prompt would still be sent.
    let release: ((v: unknown) => void) | undefined
    dispatch.mockImplementation((action: { type?: string }) => ({
      unwrap: () =>
        action?.type === 'createSlot'
          ? new Promise(r => {
              release = r
            })
          : Promise.resolve('ok'),
    }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], [appWith([APPROVE_ALL])])
    const onClose = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // The app is disabled while the create is still in flight, then the create lands.
    client.setQueryData(['apps'], [appWith([APPROVE_ALL], { enabled: false })])
    view.rerender(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    release?.({ key: 'slot-new' })
    await waitFor(() => expect(release).toBeDefined())
    // flake-ok: pre-existing microtask flush before an absence check, moved verbatim; tracked for a de-flake pass
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(dispatch).not.toHaveBeenCalledWith({ type: 'setPendingInput', text: expect.anything() })
    expect(navigate).not.toHaveBeenCalledWith('/chat?autoSend=1')
  })

  it('refuses to send when a broadened matcher means no preview was ever shown', async () => {
    // The preview is withheld until the value validates. If the app widens its matcher
    // while the field is open, the reader has been looking at a rejection and never saw a
    // preview, while the live matcher now passes -- and the prompt itself is unchanged, so
    // comparing prompts cannot catch it.
    resolvingDispatch()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], [appWith([APPROVE_ALL])])
    const onClose = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    const input = enterCommand(/Approve all PRs/)
    // Refused by the declared url+github.com matcher, so no preview renders.
    fireEvent.change(input, { target: { value: 'https://gitlab.com/g/p/-/merge_requests/1' } })
    expect(screen.queryByText(/Approve every pull request behind https/)).toBeNull()

    // The app widens the matcher to plain text, which would accept that value.
    client.setQueryData(['apps'], [
      appWith([{ ...APPROVE_ALL, argument: { kind: 'text' } }]),
    ])
    view.rerender(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )

    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('changed'))
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: expect.objectContaining({ activate: false }) })
  })

  it('a stale activation does not clear a live one\'s duplicate-run guard', async () => {
    // `pendingRow` is what makes a second Enter a no-op while a create is in flight.
    // Cleared unconditionally, a STALE run wipes a LIVE one's: revoke during create A,
    // start create B, then let A resolve -- A's finally would release B's guard and the
    // next Enter would start a second create for the same intent, which with autoSend is
    // a duplicate session that sends.
    const releases: Array<(v: unknown) => void> = []
    dispatch.mockImplementation((action: { type?: string }) => ({
      unwrap: () =>
        action?.type === 'createSlot'
          ? new Promise(r => releases.push(r))
          : Promise.resolve('ok'),
    }))
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(releases).toHaveLength(1))

    // Revoke A (Escape out of the argument state), then start B. The pasted link stays in
    // the box as the root query, so clear it before the row is matchable again.
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '' } })
    const again = enterCommand(/Approve all PRs/)
    fireEvent.change(again, { target: { value: LINK } })
    fireEvent.keyDown(again, { key: 'Enter' })
    await waitFor(() => expect(releases).toHaveLength(2))

    // A resolves late. It must not release B's guard. Wrapped in `act` so the state
    // update from the promise callback is flushed before the next keystroke reads it --
    // without that the handler closure still sees the pre-clear value and the test
    // cannot tell the two behaviours apart.
    await act(async () => {
      releases[0]({ key: 'slot-a' })
      // flake-ok: pre-existing microtask flush inside act(), moved verbatim; tracked for a de-flake pass
      await Promise.resolve()
      await Promise.resolve()
    })

    // A third Enter while B is still in flight must start no further create.
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    // flake-ok: pre-existing microtask flush before an absence check, moved verbatim; tracked for a de-flake pass
    await Promise.resolve()
    expect(releases).toHaveLength(2)
  })

  it('refuses an over-cap value instead of sending a truncated prefix', async () => {
    // The field carried `maxLength` for one round, which made the BROWSER clip a paste to
    // the cap before `onChange` -- so a 2001-character value arrived as a valid-looking
    // 2000-character prefix and was sent. That is the silent truncation this module
    // refuses to do, and it had been reintroduced one layer below the check that refuses
    // it. The validator must see the whole value.
    resolvingDispatch()
    mountWithApps([appWith([{ ...APPROVE_ALL, argument: { kind: 'text' } }])])
    const input = enterCommand(/Approve all PRs/)
    // The load-bearing assertion. The behavioural half below CANNOT catch a regression
    // here: `fireEvent.change` assigns `.value` directly, which bypasses jsdom's
    // maxlength enforcement, so re-adding the attribute leaves every behavioural
    // expectation passing. Asserting the attribute is absent is what actually guards it.
    expect(input.getAttribute('maxLength')).toBeNull()
    const oversized = 'x'.repeat(2001)
    fireEvent.change(input, { target: { value: oversized } })

    // The whole value stays in the field -- nothing clipped it on the way in.
    expect(input.value).toHaveLength(2001)
    // No preview, because the value does not validate.
    expect(screen.queryByText(/WILL SEND/i)).toBeNull()

    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'createSlot', arg: expect.objectContaining({ activate: false }) })
  })

  it('shows the resolved prompt before an auto-sending command fires', async () => {
    // The consent mechanism for autoSend: app-authored text goes to an agent with
    // tools as if the reader typed it, so the reader is shown the instruction with
    // their own value already spliced in.
    resolvingDispatch()
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    await waitFor(() => expect(screen.getByText(/Approve every pull request behind https/)).toBeTruthy())
    expect(screen.getByText(/Skip any I authored/)).toBeTruthy()
  })

  describe('the clipped-preview cue', () => {
    // jsdom does no layout: `scrollHeight` and `clientHeight` are both 0, so
    // `scrollHeight > clientHeight + 1` is `0 > 1` and the cue's branch is UNREACHABLE
    // in a test that does not stub the metrics. That is why it shipped with no coverage
    // at all, and why its only evidence was a screenshot -- one that happens to show a
    // prompt clipped mid-sentence with no cue in the frame. Stubbing the two properties
    // is what makes the safety cue testable rather than merely asserted.
    const stubMetrics = (scrollHeight: number, clientHeight: number) => {
      const proto = HTMLPreElement.prototype
      const original = {
        scrollHeight: Object.getOwnPropertyDescriptor(proto, 'scrollHeight'),
        clientHeight: Object.getOwnPropertyDescriptor(proto, 'clientHeight'),
      }
      Object.defineProperty(proto, 'scrollHeight', { configurable: true, value: scrollHeight })
      Object.defineProperty(proto, 'clientHeight', { configurable: true, value: clientHeight })
      return () => {
        for (const [key, desc] of Object.entries(original)) {
          if (desc) Object.defineProperty(proto, key, desc)
          else delete (proto as unknown as Record<string, unknown>)[key]
        }
      }
    }

    it('warns when the instruction continues out of sight', async () => {
      const restore = stubMetrics(500, 160)
      try {
        resolvingDispatch()
        mountWithApps([appWith([APPROVE_ALL])])
        const input = enterCommand(/Approve all PRs/)
        fireEvent.change(input, { target: { value: LINK } })
        await waitFor(() => expect(screen.getByText(/Scroll to read the rest/)).toBeTruthy())
      } finally {
        restore()
      }
    })

    it('stays silent when the whole instruction is visible', async () => {
      // The converse, so the cue cannot pass by always warning -- which would train the
      // reader to ignore the one line that means the tail is hidden.
      const restore = stubMetrics(120, 160)
      try {
        resolvingDispatch()
        mountWithApps([appWith([APPROVE_ALL])])
        const input = enterCommand(/Approve all PRs/)
        fireEvent.change(input, { target: { value: LINK } })
        await waitFor(() =>
          expect(screen.getByText(/Approve every pull request behind https/)).toBeTruthy(),
        )
        expect(screen.queryByText(/Scroll to read the rest/)).toBeNull()
      } finally {
        restore()
      }
    })

    it('re-measures when the box changes, not only when the text does', async () => {
      // The effect's deps are the previewed CONTENT, so on its own it cannot notice the
      // box shrinking: narrow the viewport and a prompt that fitted starts clipping with
      // the cue absent -- and with autoSend, Enter then sends a tail never shown. That is
      // the unsafe direction, so the box itself is observed. jsdom supplies no
      // ResizeObserver, so it is stubbed the way the rest of this repo stubs it.
      const callbacks: Array<() => void> = []
      class FakeResizeObserver {
        constructor(cb: () => void) {
          callbacks.push(cb)
        }
        observe = vi.fn()
        unobserve = vi.fn()
        disconnect = vi.fn()
      }
      const originalRO = globalThis.ResizeObserver
      globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
      // Starts fitting, so the cue is correctly absent.
      let restoreMetrics = stubMetrics(120, 160)
      try {
        resolvingDispatch()
        mountWithApps([appWith([APPROVE_ALL])])
        const input = enterCommand(/Approve all PRs/)
        fireEvent.change(input, { target: { value: LINK } })
        await waitFor(() =>
          expect(screen.getByText(/Approve every pull request behind https/)).toBeTruthy(),
        )
        expect(screen.queryByText(/Scroll to read the rest/)).toBeNull()
        expect(callbacks.length).toBeGreaterThan(0)

        // The box now overflows with the TEXT UNCHANGED, which only the observer can see.
        restoreMetrics()
        restoreMetrics = stubMetrics(500, 160)
        await act(async () => {
          for (const cb of callbacks) cb()
        })
        expect(screen.getByText(/Scroll to read the rest/)).toBeTruthy()
      } finally {
        restoreMetrics()
        globalThis.ResizeObserver = originalRO
      }
    })

    it('gives the preview a keyboard stop, so the scroll instruction is followable', async () => {
      // Structural, not behavioural, and deliberately so: jsdom does not scroll, so no
      // behavioural test can show a keyboard user reaching the tail. The defect was that
      // the cue said "scroll to read the rest" while the box was a scroll container with
      // no tab stop -- unreachable without a mouse, on the surface that carries consent.
      const restore = stubMetrics(500, 160)
      try {
        resolvingDispatch()
        mountWithApps([appWith([APPROVE_ALL])])
        const input = enterCommand(/Approve all PRs/)
        fireEvent.change(input, { target: { value: LINK } })
        await waitFor(() => expect(screen.getByText(/Scroll to read the rest/)).toBeTruthy())
        const preview = screen.getByRole('region', { name: /will send/i })
        expect(preview.getAttribute('tabIndex')).toBe('0')
        // The name matters as much as the stop: a bare focusable block is a silent halt.
        expect(preview.getAttribute('aria-labelledby')).toBeTruthy()
      } finally {
        restore()
      }
    })
  })

  it('names the contributing app in the row meta, where a subtitle cannot hide it', () => {
    // `subtitle || appLabel` meant an app that wrote its own subtitle left the root row
    // visually identical to a builtin -- same badge, same shape -- while its prompt goes
    // to an agent with tools. An argument-less command never reaches the argument state
    // either, so provenance could go unanswered end to end. The meta column is the one
    // place in the root list that can carry it.
    resolvingDispatch()
    mountWithApps([appWith([{ id: 'standup', title: 'Zzstandup', prompt: 'Summarise' }])])
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zzstandup' } })
    // The composed meta, not a bare app-name match: with no subtitle the fallback renders
    // the app name too, so a loose matcher would pass on the subtitle alone and prove
    // nothing about the meta column this fix is about.
    // The VALIDATED identifier, not the fixture's `displayName`: provenance never comes
    // from a field the app chooses, or it could claim to be the host.
    expect(screen.getByText(`pr-bulk-ops \u00B7 Command`)).toBeTruthy()
  })

  it('names the contributing app even when the app wrote its own subtitle', () => {
    // `subtitle || appLabel` meant any app that supplied a subtitle displaced the only
    // mention of who authored the prompt -- and this is the step right before that prompt
    // goes to an agent with tools, so a third-party instruction read as a native one.
    resolvingDispatch()
    mountWithApps([appWith([APPROVE_ALL])])
    enterCommand(/Approve all PRs/)
    expect(screen.getByText(APPROVE_ALL.subtitle)).toBeTruthy()
    expect(screen.getByText('pr-bulk-ops')).toBeTruthy()
  })

  it.each(['__proto__', 'constructor', 'toString'])(
    'renders a row whose icon names the inherited key %s',
    (icon) => {
      // Measured, not assumed: with a plain `CONTRIBUTED_ICONS[name] ?? fallback`, only
      // `__proto__` actually throws -- "Objects are not valid as a React child" -- and it
      // throws while BUILDING the row list, so the whole overlay goes down on every open
      // rather than that one row degrading. `constructor` and `toString` return FUNCTIONS,
      // which are equally not-nullish and equally never the intended glyph, but which this
      // React version tolerates rather than rejecting; they are covered here because the
      // lookup being wrong is the defect, not because they crash today. `icon` is
      // deliberately unvalidated past being a string, so any name reaches this line.
      resolvingDispatch()
      mountWithApps([appWith([{ ...APPROVE_ALL, icon }])])
      expect(screen.getByText(/Approve all PRs/)).toBeTruthy()
    },
  )

  it('does not promise a view for a row that opens none', () => {
    // Uses an ARGUMENT-LESS contributed command, which is the shape the label was most
    // wrong for: its Enter creates a seeded session, so "Open View" named something that
    // does not happen at all. It also makes the selection deterministic -- a unique query
    // leaves this row selected at index 0, and the footer names the SELECTED row, so
    // without that the assertion would read whatever row happened to be first and pass
    // for the wrong reason. Asserting the old label is ABSENT is the half that guards
    // against reverting to the view row's shared key, since any correct label satisfies
    // the positive check.
    resolvingDispatch()
    mountWithApps([
      appWith([{ id: 'standup', title: 'Zzstandup', prompt: 'Summarise yesterday' }]),
    ])
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zzstandup' } })
    expect(screen.queryByText('Open View')).toBeNull()
    expect(screen.getByText('Continue')).toBeTruthy()
  })

  it('withholds the preview until the value satisfies the pattern', () => {
    // A half-built template would advertise text that is not what would be sent.
    // Asserted on a phrase unique to the PROMPT: the subtitle shares its opening
    // words, so a looser matcher is satisfied by copy that is always on screen.
    resolvingDispatch()
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: 'https://gitl' } })
    expect(screen.queryByText(/Skip any I authored/)).toBeNull()
  })

  it('seeds the resolved prompt and sends it when the app asked to', async () => {
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-new' }) })
    const onClose = mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'createSlot', arg: expect.objectContaining({ activate: false }) }))
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({
        type: 'switchSlot',
        key: 'slot-new',
        keepTargetOnMissing: true,
      }),
    )
    const seeded = dispatch.mock.calls.map(c => c[0]).find(a => a?.type === 'setPendingInput')
    expect(seeded.text).toBe(`Approve every pull request behind ${LINK}. Skip any I authored.`)
    // `autoSend=1` and NOT `newSession=1`: the session already exists.
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/chat?autoSend=1'))
    expect(onClose).toHaveBeenCalled()
  })

  it('leaves a non-auto-sending command in the composer', async () => {
    dispatch.mockReturnValue({ unwrap: () => Promise.resolve({ key: 'slot-new' }) })
    mountWithApps([appWith([{ ...APPROVE_ALL, autoSend: false }])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/chat'))
  })

  it('Escape leaves the command before it closes the bar', () => {
    resolvingDispatch()
    const onClose = mountWithApps([appWith([APPROVE_ALL])])
    enterCommand(/Approve all PRs/)
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('Backspace on an empty field leaves the command too', () => {
    resolvingDispatch()
    const onClose = mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.keyDown(input, { key: 'Backspace' })
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getAllByRole('option').length).toBeGreaterThan(0)
  })

  it('refuses a second Enter while the first is still creating', () => {
    let release: ((v: unknown) => void) | undefined
    dispatch.mockReturnValue({ unwrap: () => new Promise(r => { release = r }) })
    mountWithApps([appWith([APPROVE_ALL])])
    const input = enterCommand(/Approve all PRs/)
    fireEvent.change(input, { target: { value: LINK } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(dispatch.mock.calls.filter(c => c[0]?.type === 'createSlot')).toHaveLength(1)
    release?.({ key: 'slot-new' })
  })

  it('seeds nothing when the bar is dismissed mid-create', async () => {
    let release: ((v: unknown) => void) | undefined
    dispatch.mockReturnValue({ unwrap: () => new Promise(r => { release = r }) })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(['apps'], [appWith([APPROVE_ALL])])
    const onClose = vi.fn()
    const view = render(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open onClose={onClose} />
      </QueryClientProvider>,
    )
    fireEvent.mouseDown(optionByTitle(/Approve all PRs/))
    fireEvent.change(screen.getByRole('combobox'), { target: { value: LINK } })
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    view.rerender(
      <QueryClientProvider client={client}>
        <CommandBarOverlay open={false} onClose={onClose} />
      </QueryClientProvider>,
    )
    release?.({ key: 'slot-new' })
    // flake-ok: pre-existing microtask flush before an absence check, moved verbatim; tracked for a de-flake pass
    await Promise.resolve()
    expect(dispatch).not.toHaveBeenCalledWith({ type: 'setPendingInput', text: expect.anything() })
    expect(navigate).not.toHaveBeenCalledWith('/chat?autoSend=1')
  })
})
