import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within, cleanup } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from '@tanstack/react-query'
import type { MemberRosterRow } from '../../api/client'
import { createTestStore } from '../../test/helpers'

/* The crewmate's own settings, behind ONE Settings row on its profile card's
 * Profile tab. The row reads the current picks; tapping it pushes the Settings
 * page (an iOS-style group of rows) over the card through the card's
 * `ProfilePagePushContext`, and each picker row there pushes a single-choice
 * page one level deeper. What is pinned here:
 *   - the card shows the row alone: no group, no form control, until it is tapped;
 *   - the row reads the stored values (an unset permission reads as Trust, the
 *     default the thread opens in) and the picker page checks the same one;
 *   - each pick writes the crew record AND the live DM slot, so the open thread
 *     follows at once; the picker page stays open, check mark moved;
 *   - "Inherited" goes over the wire as '' (the record's inherit spelling);
 *   - the lock stays a switch in the group, with its help as the group's caption.
 * The card is stood in for by a fake push with two hosts (depth 0: the group,
 * depth 1: the picker); the pages are portalled into them, so the test reads
 * them like the card would show them. */

const mockApi = vi.hoisted(() => ({
  updateKirocrewAgent: vi.fn(),
  chatMode: vi.fn(),
  chatSlotModel: vi.fn(),
  chatSlotReasoningEffort: vi.fn(),
  agentResolvedModel: vi.fn(),
  harnessModels: vi.fn(),
  memberDashboard: vi.fn(),
  memberDashboardLock: vi.fn(),
  kirocrewConfig: vi.fn(),
}))
vi.mock('../../api/client', () => ({ api: mockApi }))
const modelsState = vi.hoisted(() => ({ isDegraded: false }))
vi.mock('../../hooks/useAvailableModels', () => ({
  useAvailableModelsQuery: () => ({ data: [{ name: 'auto' }, { name: 'claude-opus-5' }, { name: 'claude-haiku-4' }], isDegraded: modelsState.isDegraded, error: null }),
}))

import CrewProfileSettings from './CrewProfileSettings'
import { ProfilePagePushContext, type ProfilePagePush } from './CrewProfilePanel'
import { isTrustRootTarget } from '../../guide/findTargetPolicy'
import { ApiError } from '../../api/apiError'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { MEMBER_DASHBOARD_QUERY_PREFIX } from '../../hooks/useWebSocket'
import { PREVIEW_CREWMATE_HARNESS } from '../../utils/previewFlags'

let lastQc: QueryClient
let hosts: [HTMLDivElement, HTMLDivElement]
let push: ProfilePagePush & { open: ReturnType<typeof vi.fn>; close: ReturnType<typeof vi.fn> }

const member = (overrides: Partial<MemberRosterRow> = {}): MemberRosterRow => ({
  name: 'oncall', slug: 'oncall', slot_key: 'member-oncall', running: false, model: '',
  ...overrides,
} as MemberRosterRow)

function setup(m: MemberRosterRow, slotKey: string | null = 'member-oncall', waiting = false, { openGroup = true } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(MEMBERS_ROSTER_QUERY_KEY, [m])
  lastQc = qc
  hosts = [document.createElement('div'), document.createElement('div')]
  hosts[0].setAttribute('data-testid', 'page-host-0')
  hosts[1].setAttribute('data-testid', 'page-host-1')
  document.body.append(...hosts)
  push = { open: vi.fn(), close: vi.fn(), host: hosts[0], hosts }
  const store = createTestStore()
  const tree = (row: MemberRosterRow, p: ProfilePagePush = push) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ProfilePagePushContext.Provider value={p}>
          <CrewProfileSettings member={row} slotKey={slotKey} waiting={waiting} />
        </ProfilePagePushContext.Provider>
      </Provider>
    </QueryClientProvider>
  )
  const r = render(tree(m))
  // Most cases read the Settings page: open it the way the person does.
  if (openGroup) fireEvent.click(settingsRow())
  return {
    /** The roster re-read the host would hand down after a save elsewhere. */
    rerender: (next: MemberRosterRow) => r.rerender(tree(next)),
    /** A new context value from the card (a host mounted), same callbacks. */
    rerenderPush: (p: ProfilePagePush) => r.rerender(tree(m, p)),
  }
}
const settingsRow = () => screen.getByTestId('crew-profile-settings-row')
const group = () => within(hosts[0]).getByTestId('crew-profile-settings')
const row = (key: string) => within(group()).getByTestId(`crew-profile-setting-${key}`)
const rowValue = (key: string) => within(group()).getByTestId(`crew-profile-setting-${key}-value`)
const page = (key: string) => within(hosts[1]).getByTestId(`crew-profile-setting-page-${key}`)
const open = (key: string) => { fireEvent.click(row(key)); return page(key) }
/** Open the setting's page and pick one option by its label. */
const pick = (key: string, option: string) => fireEvent.click(within(open(key)).getByRole('radio', { name: option }))
const checked = (key: string) => within(page(key)).getByRole('radio', { checked: true })

beforeEach(() => {
  vi.clearAllMocks()
  modelsState.isDegraded = false
  mockApi.memberDashboard.mockResolvedValue({ structure_locked: false })
  mockApi.memberDashboardLock.mockResolvedValue({ ok: true, structure_locked: true })
  mockApi.updateKirocrewAgent.mockResolvedValue({ ok: true })
  mockApi.chatMode.mockResolvedValue({ ok: true })
  mockApi.chatSlotModel.mockResolvedValue({ ok: true })
  mockApi.chatSlotReasoningEffort.mockResolvedValue({ ok: true })
  mockApi.agentResolvedModel.mockResolvedValue({ model: 'claude-opus-5' })
  mockApi.kirocrewConfig.mockResolvedValue({ dashboard: {} })
  mockApi.harnessModels.mockResolvedValue([{ model_name: 'auto' }, { model_name: 'sonnet-cc' }])
  localStorage.removeItem(PREVIEW_CREWMATE_HARNESS)
})
afterEach(() => { cleanup(); hosts?.forEach((h) => h.remove()); localStorage.removeItem(PREVIEW_CREWMATE_HARNESS) })

describe('CrewProfileSettings row and group', () => {
  it('shows one Settings row on the card, reading the ceiling and the model', () => {
    setup(member({ model: 'claude-opus-5', reasoning_effort: 'high' }), 'member-oncall', false, { openGroup: false })
    expect(settingsRow()).toHaveTextContent('Settings')
    expect(settingsRow()).toHaveTextContent('Trust · claude-opus-5')
    // Not the worker pick: "Same as oncall" beside oncall's own card read as a riddle.
    expect(settingsRow()).not.toHaveTextContent('Same as')
    expect(settingsRow()).toHaveAccessibleName('Settings')
    // The reading on screen is what a screen reader hears too.
    expect(settingsRow()).toHaveAccessibleDescription('Trust · claude-opus-5')
    // Nothing is pushed until the row is tapped. (The group paints into the
    // depth-0 host whenever the card provides one; the real card mounts that
    // host only while a page is pushed, which CrewProfilePanel.test.tsx pins.)
    expect(push.open).not.toHaveBeenCalled()
    expect(within(hosts[0]).queryByTestId('crew-profile-setting-approval_mode')).not.toBeNull()
    expect(hosts[1]).toBeEmptyDOMElement()
  })

  it('pushes the Settings page over the card, and renders the group into its host', () => {
    setup(member())
    expect(push.open).toHaveBeenCalledWith('Settings', expect.any(Function), 0)
    expect(within(hosts[0]).getByTestId('crew-profile-settings')).toBeInTheDocument()
    expect(hosts[1]).toBeEmptyDOMElement()
  })

  it('is one group of rows, each reading its value, with no form control', () => {
    setup(member({ model: 'claude-opus-5', reasoning_effort: 'high' }))
    expect(rowValue('approval_mode')).toHaveTextContent('Trust')
    expect(rowValue('model')).toHaveTextContent('claude-opus-5')
    expect(rowValue('reasoning_effort')).toHaveTextContent('High')
    expect(rowValue('worker_model')).toHaveTextContent('Same as oncall')
    expect(rowValue('worker_reasoning_effort')).toHaveTextContent('Same level as oncall')
    // The pickers are doors (buttons that push a page), never selects.
    expect(group().querySelector('select, [role="combobox"], [role="listbox"]')).toBeNull()
  })

  it('pushes the picker one level deeper, titled with the row', () => {
    setup(member())
    open('approval_mode')
    expect(push.open).toHaveBeenLastCalledWith('Permissions', expect.any(Function), 1)
    expect(within(hosts[1]).getByRole('radiogroup', { name: 'Permissions' })).toBeInTheDocument()
  })

  it('hands the picker back when the card pops it, so the next open starts clean', () => {
    setup(member())
    open('model')
    const onPop = push.open.mock.calls[1][1] as () => void
    expect(push.open).toHaveBeenCalledTimes(2)
    act(() => onPop())
    open('approval_mode')
    expect(push.open).toHaveBeenLastCalledWith('Permissions', expect.any(Function), 1)
  })

  it('drops an open picker with the group when the card pops the Settings page', () => {
    setup(member())
    open('model')
    const popGroup = push.open.mock.calls[0][1] as () => void
    act(() => popGroup())
    // The row is back on the card; reopening starts at the group, no picker over it.
    push.open.mockClear()
    fireEvent.click(settingsRow())
    expect(push.open).toHaveBeenCalledTimes(1)
    expect(push.open).toHaveBeenCalledWith('Settings', expect.any(Function), 0)
  })

  it('does not re-push when the card hands down a new push object with the same callbacks', () => {
    // The card's context value changes whenever a host mounts; only `open` /
    // `close` are stable. An effect keyed on the object would re-run `open`
    // and rebuild the level with the wrong opener.
    const { rerenderPush } = setup(member())
    open('model')
    expect(push.open).toHaveBeenCalledTimes(2)
    rerenderPush({ ...push, hosts: [hosts[0], hosts[1]] })
    expect(push.open).toHaveBeenCalledTimes(2)
  })

  it('moves between options with the arrow keys, one Tab stop on the checked one, and never picks by moving', () => {
    setup(member({ approval_mode: 'trust_reads' }))
    const p = open('approval_mode')
    const [normal, reads, trust] = within(p).getAllByRole('radio')
    expect(reads).toHaveAttribute('tabindex', '0')
    expect(normal).toHaveAttribute('tabindex', '-1')
    reads.focus()
    fireEvent.keyDown(reads, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(trust)
    fireEvent.keyDown(trust, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(normal)
    fireEvent.keyDown(normal, { key: 'End' })
    expect(document.activeElement).toBe(trust)
    fireEvent.keyDown(trust, { key: 'Home' })
    expect(document.activeElement).toBe(normal)
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
  })

  it('keeps the notices under the group while no picker is open', () => {
    modelsState.isDegraded = true
    setup(member())
    expect(within(group()).getByTestId('crew-profile-settings-models-error')).not.toBeEmptyDOMElement()
  })
})

describe('CrewProfileSettings permission', () => {
  it('reads Trust when the mate never chose a permission', () => {
    setup(member())
    expect(rowValue('approval_mode')).toHaveTextContent('Trust')
    open('approval_mode')
    expect(checked('approval_mode')).toHaveTextContent('Trust')
  })

  it.each([['normal', 'Normal'], ['trust_reads', 'Reads']])('shows a stored %s as stored, not the default', (mode, label) => {
    setup(member({ approval_mode: mode }))
    expect(rowValue('approval_mode')).toHaveTextContent(label)
  })

  it('keeps the permission row and its page out of reach of a name-only guide', () => {
    setup(member())
    // `members.permissions` is denied by id; a guide that asks for "Permissions"
    // by name must be refused too, so the row and the page sit in trust-root regions.
    expect(isTrustRootTarget(row('approval_mode'), 'Permissions')).toBe(true)
    expect(isTrustRootTarget(within(open('approval_mode')).getByRole('radio', { name: /Trust/ }), 'Trust')).toBe(true)
  })

  it('names the row Permissions for find_ui and describes it with the value', () => {
    setup(member())
    expect(row('approval_mode')).toHaveAccessibleName('Permissions')
    expect(row('approval_mode')).toHaveAccessibleDescription('Trust')
  })

  it('says under each option what that mode lets the mate do', () => {
    setup(member())
    const p = open('approval_mode')
    expect(p).toHaveTextContent('oncall asks you before each tool, in every chat with it.')
    expect(p).toHaveTextContent('oncall runs read-only commands on its own and asks for the rest, in every chat with it.')
    expect(p).toHaveTextContent('oncall runs tools without asking you first, in every chat with it.')
    // Normal is the row above, so no description tells the person to go pick it.
    expect(p).not.toHaveTextContent('Pick Normal')
  })

  it('sends one compare-and-set write; the server moves the live thread', async () => {
    setup(member())
    pick('approval_mode', /Normal/)
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { approval_mode: 'normal', expected_approval_mode: '' }))
    // The browser never writes the thread's permission itself.
    expect(mockApi.chatMode).not.toHaveBeenCalled()
    // The page stays open with the check moved; the row behind it reads the new value.
    expect(checked('approval_mode')).toHaveTextContent('Normal')
    expect(rowValue('approval_mode')).toHaveTextContent('Normal')
    expect(push.close).not.toHaveBeenCalled()
  })

  it('does nothing when the checked option is tapped again', () => {
    setup(member())
    pick('approval_mode', /Trust/)
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
  })

  it('writes only the record when the mate has no thread yet', async () => {
    setup(member(), null)
    pick('approval_mode', /Reads/)
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { approval_mode: 'trust_reads', expected_approval_mode: '' }))
    expect(mockApi.chatMode).not.toHaveBeenCalled()
  })

  it('takes one save at a time, so a slow Trust cannot land after a later Normal', async () => {
    let finish!: () => void
    mockApi.updateKirocrewAgent.mockReturnValueOnce(new Promise((r) => { finish = () => r({ ok: true }) }))
    setup(member({ approval_mode: 'normal' }))
    pick('approval_mode', /Trust/)
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledTimes(1))
    // The page's options are held while the save runs.
    expect(page('approval_mode')).toHaveAttribute('aria-busy', 'true')
    const normal = within(page('approval_mode')).getByRole('radio', { name: /Normal/ })
    expect(normal).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(normal)
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledTimes(1)
    finish()
    await waitFor(() => expect(page('approval_mode')).not.toHaveAttribute('aria-busy', 'true'))
    expect(normal).not.toHaveAttribute('aria-disabled')
    expect(checked('approval_mode')).toHaveTextContent('Trust')
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledTimes(1)
  })

  it('keeps the picked option focusable through the save, so Escape still reaches the card', async () => {
    // The `disabled` attribute on a focused button drops focus onto the body,
    // outside the card, where its Escape handler returns early. The hold is
    // aria-disabled instead, and focus stays where the pick was made.
    let finish!: () => void
    mockApi.updateKirocrewAgent.mockReturnValueOnce(new Promise((r) => { finish = () => r({ ok: true }) }))
    setup(member())
    const normal = within(open('approval_mode')).getByRole('radio', { name: /Normal/ })
    normal.focus()
    fireEvent.click(normal)
    await waitFor(() => expect(page('approval_mode')).toHaveAttribute('aria-busy', 'true'))
    expect(normal).not.toBeDisabled()
    expect(document.activeElement).toBe(normal)
    finish()
    await waitFor(() => expect(page('approval_mode')).not.toHaveAttribute('aria-busy', 'true'))
    expect(document.activeElement).toBe(normal)
  })

  it('writes a saved choice into the cached roster, so a reopen never shows the old one', async () => {
    setup(member())
    pick('approval_mode', /Normal/)
    await waitFor(() => expect(lastQc.getQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY)?.[0].approval_mode).toBe('normal'))
  })

  it('saves nothing while the thread is still being confirmed', async () => {
    setup(member(), null, true)
    expect(within(group()).getByTestId('crew-profile-settings-waiting')).toHaveTextContent('These unlock once the chat with this mate is open.')
    const p = open('approval_mode')
    expect(p).toHaveAttribute('aria-busy', 'true')
    expect(within(p).getByRole('radio', { name: /Normal/ })).toHaveAttribute('aria-disabled', 'true')
    expect(within(p).getByTestId('crew-profile-settings-waiting')).toBeInTheDocument()
    fireEvent.click(within(p).getByRole('radio', { name: /Normal/ }))
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
    expect(rowValue('approval_mode')).toHaveTextContent('Trust')
  })

  it('shows the current value when the server refuses a stale pick', async () => {
    mockApi.updateKirocrewAgent.mockRejectedValueOnce(
      new ApiError(409, 'conflict', JSON.stringify({ code: 'approval_mode_conflict', approval_mode: 'trust_reads' })),
    )
    setup(member({ approval_mode: 'normal' }))
    pick('approval_mode', /Trust/)
    // The notice lands on the open page, where the person is.
    await waitFor(() => expect(within(page('approval_mode')).getByTestId('crew-profile-settings-notice')).toHaveTextContent('The permission changed elsewhere'))
    expect(checked('approval_mode')).toHaveTextContent('Reads')
    expect(rowValue('approval_mode')).toHaveTextContent('Reads')
    expect(lastQc.getQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY)?.[0].approval_mode).toBe('trust_reads')
  })

  it('puts the stored value back and says why when the save fails', async () => {
    mockApi.updateKirocrewAgent.mockRejectedValueOnce(new Error('disk full'))
    setup(member({ approval_mode: 'normal' }))
    pick('approval_mode', /Trust/)
    await waitFor(() => expect(within(page('approval_mode')).getByTestId('crew-profile-settings-error')).toHaveTextContent('disk full'))
    expect(checked('approval_mode')).toHaveTextContent('Normal')
    expect(mockApi.chatMode).not.toHaveBeenCalled()
  })
})

describe('CrewProfileSettings model and effort', () => {
  it.each([
    ['model', 'claude-opus-5'],
    ['reasoning_effort', 'High'],
  ])('touches no thread when saving %s fails', async (key, label) => {
    mockApi.updateKirocrewAgent.mockRejectedValueOnce(new Error('record save failed'))
    setup(member({ model: 'claude-haiku-4', reasoning_effort: 'low' }))
    pick(key, label)
    await waitFor(() => expect(within(page(key)).getByTestId('crew-profile-settings-error')).toHaveTextContent('record save failed'))
    expect(mockApi.chatSlotModel).not.toHaveBeenCalled()
    expect(mockApi.chatSlotReasoningEffort).not.toHaveBeenCalled()
  })

  it('pins a model on the record and the thread', async () => {
    setup(member())
    pick('model', 'claude-opus-5')
    await waitFor(() => expect(mockApi.chatSlotModel).toHaveBeenCalledWith('member-oncall', 'claude-opus-5'))
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { model: 'claude-opus-5' })
    expect(rowValue('model')).toHaveTextContent('claude-opus-5')
  })

  it('sends Inherited as the empty pin', async () => {
    setup(member({ model: 'claude-opus-5' }))
    pick('model', 'Inherited')
    await waitFor(() => expect(mockApi.chatSlotModel).toHaveBeenCalledWith('member-oncall', ''))
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { model: '' })
  })

  it('offers a stored model the list no longer carries, checked', () => {
    setup(member({ model: 'claude-sonnet-5' }))
    expect(rowValue('model')).toHaveTextContent('claude-sonnet-5')
    open('model')
    expect(checked('model')).toHaveTextContent('claude-sonnet-5')
  })

  it('offers effort on a model that reasons and writes both sides', async () => {
    setup(member({ model: 'claude-opus-5' }))
    pick('reasoning_effort', 'High')
    await waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('member-oncall', 'high'))
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { reasoning_effort: 'high' })
    expect(rowValue('reasoning_effort')).toHaveTextContent('High')
  })

  it('names the model an inherited pin resolves to, on the Inherited option itself', async () => {
    setup(member())
    const p = open('model')
    const inherit = within(p).getByRole('radio', { name: /Inherited/ })
    await waitFor(() => expect(inherit).toHaveTextContent('Using claude-opus-5, the crew default'))
  })

  it('names the level an inherited effort resolves to, on the Inherited option itself', async () => {
    mockApi.agentResolvedModel.mockResolvedValue({ model: 'claude-opus-5', reasoning_effort: 'medium' })
    setup(member({ model: 'claude-opus-5' }))
    const p = open('reasoning_effort')
    await waitFor(() => expect(within(p).getByRole('radio', { name: /Inherited/ })).toHaveTextContent('Medium'))
  })

  it('shows a failed resolve instead of silently hiding effort', async () => {
    mockApi.agentResolvedModel.mockRejectedValue(new Error('resolver down'))
    setup(member())
    await waitFor(() => expect(screen.getByTestId('crew-profile-settings-resolve-error')).toHaveTextContent('resolver down'))
  })

  it('says so when the model list failed to load', () => {
    modelsState.isDegraded = true
    setup(member())
    expect(screen.getByTestId('crew-profile-settings-models-error')).not.toBeEmptyDOMElement()
  })

  it('always says what an effort level trades, under the page', () => {
    setup(member({ model: 'claude-opus-5' }))
    expect(open('reasoning_effort')).toHaveTextContent('Higher levels think longer, answer slower, and cost more.')
  })

  it('hides effort on a model that does not reason', () => {
    setup(member({ model: 'claude-haiku-4' }))
    expect(screen.queryByTestId('crew-profile-setting-reasoning_effort')).toBeNull()
  })

  it('pops the effort page when the row it was open for folds away', async () => {
    // Effort is open for Opus; the roster then hands down Haiku (a pick made
    // elsewhere), which folds the row, and the page cannot stay without it.
    const { rerender } = setup(member({ model: 'claude-opus-5' }))
    open('reasoning_effort')
    expect(push.close).not.toHaveBeenCalled()
    rerender(member({ model: 'claude-haiku-4' }))
    await waitFor(() => expect(push.close).toHaveBeenCalledWith(1))
    expect(screen.queryByTestId('crew-profile-setting-reasoning_effort')).toBeNull()
  })

  it('keeps painting a folded page, held, while the card slides it away', async () => {
    // A stale level on a model that does not reason keeps its row "so it can be
    // cleared"; clearing it from the page folds the row at once. The page must
    // not go blank mid-slide, and its options must not take another pick.
    setup(member({ model: 'claude-haiku-4', reasoning_effort: 'high' }))
    const inherit = within(open('reasoning_effort')).getByRole('radio', { name: 'Inherited' })
    fireEvent.click(inherit)
    await waitFor(() => expect(push.close).toHaveBeenCalledWith(1))
    expect(screen.queryByTestId('crew-profile-setting-reasoning_effort')).toBeNull()
    // The host still holds the page (the card unmounts it when the slide ends).
    const p = page('reasoning_effort')
    expect(p).toHaveAttribute('aria-busy', 'true')
    expect(within(p).getByRole('radio', { name: 'High' })).toHaveAttribute('aria-disabled', 'true')
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { reasoning_effort: '' }))
  })
})

/* The worker pair: what the sessions this mate opens through session_create
 * run on. '' = same as the mate; 'auto' is a REAL pin here (cheap workers
 * under a costly conductor). It writes the record only -- no thread follows. */
describe('CrewProfileSettings worker model and effort', () => {
  it('reads Same as the mate when nothing is set, and names what that runs', async () => {
    setup(member({ model: 'claude-opus-5' }))
    expect(rowValue('worker_model')).toHaveTextContent('Same as oncall')
    expect(open('worker_model')).toHaveTextContent('Workers are the extra chats oncall opens to split up a task. They run on claude-opus-5, the same as oncall.')
  })

  it('names the resolved model when the mate itself inherits', async () => {
    setup(member())
    open('worker_model')
    await waitFor(() => expect(page('worker_model')).toHaveTextContent('They run on claude-opus-5, the same as oncall.'))
  })

  it('offers Auto as a pin and writes only the record', async () => {
    setup(member({ model: 'claude-opus-5' }))
    pick('worker_model', 'Auto (cheaper model)')
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { worker_model: 'auto' }))
    expect(mockApi.chatSlotModel).not.toHaveBeenCalled()
    // Worded apart from "Same as oncall": one says cheaper, the other says same.
    expect(rowValue('worker_model')).toHaveTextContent('Auto (cheaper model)')
    // The caption names the choice against the mate's own model AND says why the
    // effort row folded (the model has no such setting), so it never just vanishes
    // and never reads as a downgrade.
    expect(page('worker_model')).toHaveTextContent("Workers run on an automatically chosen model, usually cheaper than claude-opus-5. Reasoning effort doesn't apply to this model.")
  })

  it('shows a stored pin as stored and sends Same as back as the empty string', async () => {
    setup(member({ model: 'claude-opus-5', worker_model: 'claude-haiku-4' }))
    expect(rowValue('worker_model')).toHaveTextContent('claude-haiku-4')
    expect(open('worker_model')).toHaveTextContent("Workers run on claude-haiku-4 instead of claude-opus-5. Reasoning effort doesn't apply to this model.")
    fireEvent.click(within(page('worker_model')).getByRole('radio', { name: 'Same as oncall' }))
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { worker_model: '' }))
    expect(mockApi.chatSlotModel).not.toHaveBeenCalled()
  })

  it('offers the worker effort when the workers run a model that reasons, record only', async () => {
    setup(member({ model: 'claude-opus-5', reasoning_effort: 'high' }))
    // A LEVEL, worded apart from the model row's "Same as oncall".
    expect(rowValue('worker_reasoning_effort')).toHaveTextContent('Same level as oncall')
    pick('worker_reasoning_effort', 'Low')
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { worker_reasoning_effort: 'low' }))
    expect(mockApi.chatSlotReasoningEffort).not.toHaveBeenCalled()
    expect(rowValue('worker_reasoning_effort')).toHaveTextContent('Low')
  })

  it('hides the worker effort when the workers run Auto and none is stored', () => {
    setup(member({ model: 'claude-opus-5', worker_model: 'auto' }))
    expect(screen.queryByTestId('crew-profile-setting-worker_reasoning_effort')).toBeNull()
  })

  it('keeps a stored worker effort visible even on a model that does not reason', () => {
    setup(member({ model: 'claude-opus-5', worker_model: 'claude-haiku-4', worker_reasoning_effort: 'low' }))
    expect(rowValue('worker_reasoning_effort')).toHaveTextContent('Low')
    // The row is on screen, so the model caption does not say the setting does not apply.
    expect(open('worker_model')).not.toHaveTextContent("doesn't apply to this model")
  })

  it('labels the empty effort as the model default once the workers run another model', () => {
    // Under a different model the mate's own level no longer carries over
    // (worker_session_defaults draws the same line), so '' is not "Same as".
    setup(member({ model: 'claude-opus-5', reasoning_effort: 'high', worker_model: 'claude-sonnet-5' }))
    expect(rowValue('worker_reasoning_effort')).toHaveTextContent('No level set')
    // And the page gets its own note naming the level that is NOT copied.
    const p = open('worker_reasoning_effort')
    expect(p).toHaveTextContent("Workers use their own default, not oncall's (High).")
    expect(p).not.toHaveTextContent('the same as oncall.')
  })

  it('names the mate\'s own level the empty effort resolves to while the workers run its model', () => {
    setup(member({ model: 'claude-opus-5', reasoning_effort: 'high' }))
    expect(open('worker_reasoning_effort')).toHaveTextContent('Reasoning effort High, the same as oncall.')
  })

  it('writes a saved worker pick into the cached roster', async () => {
    setup(member({ model: 'claude-opus-5' }))
    pick('worker_model', 'Auto (cheaper model)')
    await waitFor(() => expect(lastQc.getQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY)?.[0].worker_model).toBe('auto'))
  })
})

/* The person's lock on the mate's dynamic dashboard lives HERE, on the
 * mate's card, as one labelled switch in the group with a line of help as
 * the group's caption. The Dashboard tab only shows a badge
 * (CrewDynamicDashboard.test.tsx pins that it has no control). */
describe('CrewProfileSettings dashboard lock', () => {
  const SWITCH = 'Lock dashboard layout'

  it('offers the switch and reads lock state', async () => {
    setup(member())
    const sw = await screen.findByRole('switch', { name: SWITCH })
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
    expect(screen.getByTestId('crew-profile-dashboard-lock')).toHaveTextContent(SWITCH)
    expect(group()).toHaveTextContent(
      "While locked, oncall can't replace its dashboard with a different layout or restore an older one.",
    )
  })

  it('reads the lock off the dashboard body, so it shows what the gateway holds', async () => {
    mockApi.memberDashboard.mockResolvedValue({ structure_locked: true })
    setup(member())
    const sw = await screen.findByRole('switch', { name: SWITCH })
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    expect(mockApi.memberDashboard).toHaveBeenCalledWith('oncall', 'oncall')
  })

  it('is held until the read answers: a switch that moves first shows a state nothing holds', async () => {
    let answer: (v: { structure_locked: boolean }) => void = () => {}
    mockApi.memberDashboard.mockReturnValue(new Promise((r) => { answer = r }))
    setup(member())
    const sw = await screen.findByRole('switch', { name: SWITCH })
    expect(sw).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(sw)
    expect(mockApi.memberDashboardLock).not.toHaveBeenCalled()
    answer({ structure_locked: false })
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
  })

  it('flips the lock through the owner route, then re-reads every dashboard query for the mate', async () => {
    setup(member())
    const sw = await screen.findByRole('switch', { name: SWITCH })
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    const spy = vi.spyOn(lastQc, 'invalidateQueries')
    fireEvent.click(sw)
    await waitFor(() => expect(mockApi.memberDashboardLock).toHaveBeenCalledWith('oncall', 'oncall', true))
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, 'oncall'] }))
    // The tab's frame reads under the same prefix, so it is refreshed by that one call.
    expect(spy.mock.calls.some(([arg]) => (arg as { queryKey: string[] }).queryKey[0] === MEMBER_DASHBOARD_QUERY_PREFIX)).toBe(true)
  })

  it('says which change failed and what stayed the same', async () => {
    mockApi.memberDashboard.mockResolvedValue({ structure_locked: true })
    mockApi.memberDashboardLock.mockRejectedValue(new Error('403'))
    setup(member())
    const sw = await screen.findByRole('switch', { name: SWITCH })
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(mockApi.memberDashboardLock).toHaveBeenCalledWith('oncall', 'oncall', false))
    expect(await screen.findByTestId('crew-profile-dashboard-lock-error')).toHaveTextContent(
      "Couldn't unlock the dashboard. It's still locked. Try again.",
    )
  })

  it('hides the switch and shows error + hint when the lock cannot be read', async () => {
    mockApi.memberDashboard.mockRejectedValue(new Error('500'))
    setup(member())
    expect(await screen.findByTestId('crew-profile-dashboard-lock-error')).toHaveTextContent(
      "Couldn't read whether the dashboard is locked.",
    )
    // Switch is hidden when the state is unknown (showing a dim off switch
    // for an unknown state would look identical to "unlocked" and mislead),
    // but the caption is still shown so the row is not context-free.
    expect(screen.queryByRole('switch', { name: SWITCH })).toBeNull()
    expect(group()).toHaveTextContent("can't replace")
  })
})

/** Renders the card from the cached roster row, as the profile page does. */
function RosterBacked({ initial }: { initial: MemberRosterRow }) {
  const qc = useQueryClient()
  const rows = useQuery({
    queryKey: MEMBERS_ROSTER_QUERY_KEY,
    // A refetch answers what the server holds: the last value written to the row.
    queryFn: () => qc.getQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY) ?? [initial],
    initialData: [initial],
    staleTime: Infinity,
  })
  const m = rows.data?.find((r) => r.name === initial.name) ?? initial
  return <CrewProfileSettings member={m} slotKey="member-oncall" />
}

function setupRosterBacked(m: MemberRosterRow) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  lastQc = qc
  render(
    <QueryClientProvider client={qc}>
      <Provider store={createTestStore()}>
        <RosterBacked initial={m} />
      </Provider>
    </QueryClientProvider>,
  )
}

describe('CrewProfileSettings Dynamic capabilities preview', () => {
  const sw = () => within(screen.getByTestId('crew-profile-dynamic-capabilities')).getByRole('switch')

  it('follows a later change to the stored flag after its own save', async () => {
    setupRosterBacked(member())
    await screen.findByTestId('crew-profile-dynamic-capabilities')
    fireEvent.click(sw())
    await waitFor(() => expect(sw().getAttribute('aria-checked')).toBe('true'))
    // An undo from elsewhere writes the roster row back to off.
    act(() => {
      lastQc.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, (rows) =>
        rows?.map((r) => ({ ...r, dynamic_capabilities: false })))
    })
    await waitFor(() => expect(sw().getAttribute('aria-checked')).toBe('false'))
  })

  it('is off for a new crewmate and carries a Preview tag', async () => {
    setup(member())
    await screen.findByTestId('crew-profile-dynamic-capabilities')
    expect(sw().getAttribute('aria-checked')).toBe('false')
    expect(screen.getByTestId('crew-profile-dynamic-capabilities-preview')).toHaveTextContent('Preview')
  })

  it('writes only this crewmate\'s record', async () => {
    setupRosterBacked(member())
    await screen.findByTestId('crew-profile-dynamic-capabilities')
    fireEvent.click(sw())
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { dynamic_capabilities: true }))
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(sw().getAttribute('aria-checked')).toBe('true'))
  })

  it('turns an on crewmate back off', async () => {
    setupRosterBacked(member({ dynamic_capabilities: true }))
    await screen.findByTestId('crew-profile-dynamic-capabilities')
    fireEvent.click(sw())
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { dynamic_capabilities: false }))
    await waitFor(() => expect(sw().getAttribute('aria-checked')).toBe('false'))
  })

  it('shows a crewmate\'s stored flag', async () => {
    setup(member({ dynamic_capabilities: true }))
    await screen.findByTestId('crew-profile-dynamic-capabilities')
    expect(sw().getAttribute('aria-checked')).toBe('true')
  })
})

/* The crewmate's own harness (`agents.<name>.acp_backend`): a developer preview row.
 * Its stored value and the harnesses on offer come from the owner-only readout,
 * never the roster row; the model list follows the picked harness. */
describe('CrewProfileSettings harness', () => {
  const readout = (over: Record<string, unknown> = {}) => mockApi.agentResolvedModel.mockResolvedValue({
    model: 'claude-opus-5', acp_backend: '', harness: 'kas', inherited_harness: 'kas',
    harnesses: ['claude', 'kas', 'kiro'], worker_harnesses: ['claude', 'kas'], ...over,
  })

  it('is not offered while the preview is off', async () => {
    readout()
    setup(member())
    await waitFor(() => expect(mockApi.agentResolvedModel).toHaveBeenCalled())
    expect(within(group()).queryByTestId('crew-profile-setting-acp_backend')).toBeNull()
  })

  it('reads Inherited and names what that runs on, with the preview on', async () => {
    localStorage.setItem(PREVIEW_CREWMATE_HARNESS, '1')
    readout()
    setup(member())
    // The row itself says what it controls, and Inherited names what runs.
    await waitFor(() => expect(rowValue('acp_backend')).toHaveTextContent(/^Inherited \(Kiro Agent\)$/))
    expect(within(group()).getByTestId('crew-profile-setting-acp_backend')).toHaveTextContent('The AI coding tool oncall runs on')
    const options = open('acp_backend')
    // Inherited follows the crew default, so it never reads as the same choice
    // as the harness it resolves to today.
    expect(within(options).getByRole('radio', { name: /Inherited/ })).toHaveTextContent('Follows the crew default (now Kiro Agent).')
    // A harness the worker cannot start on says so before it is picked.
    expect(within(options).getByRole('radio', { name: /Kiro CLI/ })).toHaveTextContent('workers cannot start')
    // The caption names the two settings a pick resets.
    expect(options).toHaveTextContent('puts Model and Worker model back to Inherited.')
  })

  it.each([
    ['a live thread', 'member-oncall'],
    ['no thread', null],
  ])('saves a harness pick on the record alone, with %s', async (_label, slotKey) => {
    // The server reloads the live thread onto the new harness and clears its
    // own model and effort picks, so the card posts nothing to the thread.
    localStorage.setItem(PREVIEW_CREWMATE_HARNESS, '1')
    readout({ acp_backend: 'kas' })
    setup(member({ model: 'claude-opus-5', reasoning_effort: 'high', worker_model: 'claude-haiku-4' }), slotKey)
    await waitFor(() => expect(rowValue('acp_backend')).toHaveTextContent('Kiro Agent'))
    pick('acp_backend', /Kiro CLI/)
    const saved = await screen.findByTestId('crew-profile-settings-saved')
    expect(saved).toHaveTextContent("Saved. oncall's next message starts on this harness.")
    // A success is a status line, not an alert.
    expect(saved).toHaveAttribute('role', 'status')
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { acp_backend: 'kiro', model: '', worker_model: '' })
    expect(mockApi.chatSlotModel).not.toHaveBeenCalled()
    expect(mockApi.chatSlotReasoningEffort).not.toHaveBeenCalled()
    expect(screen.queryByTestId('crew-profile-settings-error')).toBeNull()
    expect(rowValue('model')).toHaveTextContent('Inherited')
    expect(rowValue('worker_model')).toHaveTextContent('Same as oncall')
    expect(lastQc.getQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY)?.[0]).toMatchObject({ model: '', reasoning_effort: 'high', worker_model: '' })
  })

  it('shows a refused harness change and puts the stored harness back', async () => {
    localStorage.setItem(PREVIEW_CREWMATE_HARNESS, '1')
    readout({ acp_backend: 'kas' })
    const message = 'oncall is in the middle of a reply. Wait for it to finish, or stop it, then change the agent harness.'
    mockApi.updateKirocrewAgent.mockRejectedValueOnce(
      new ApiError(409, message, JSON.stringify({ error: message, code: 'crewmate_harness_busy' })),
    )
    setup(member({ model: 'claude-opus-5' }))
    await waitFor(() => expect(rowValue('acp_backend')).toHaveTextContent('Kiro Agent'))
    pick('acp_backend', /Kiro CLI/)
    await waitFor(() => expect(within(page('acp_backend')).getByTestId('crew-profile-settings-error')).toHaveTextContent(message))
    expect(checked('acp_backend')).toHaveTextContent('Kiro Agent')
    expect(rowValue('acp_backend')).toHaveTextContent('Kiro Agent')
    expect(rowValue('model')).toHaveTextContent('claude-opus-5')
    expect(screen.queryByTestId('crew-profile-settings-saved')).toBeNull()
    expect(mockApi.chatSlotModel).not.toHaveBeenCalled()
    expect(mockApi.chatSlotReasoningEffort).not.toHaveBeenCalled()
  })

  it('names what Inherited runs on while a harness is pinned', async () => {
    localStorage.setItem(PREVIEW_CREWMATE_HARNESS, '1')
    readout({ acp_backend: 'claude', harness: 'claude', inherited_harness: 'kas' })
    setup(member())
    // A pinned harness reads by its own name alone, never as Inherited.
    await waitFor(() => expect(rowValue('acp_backend')).toHaveTextContent(/^Claude Code$/))
    const options = open('acp_backend')
    expect(within(options).getByRole('radio', { name: /Inherited/ })).toHaveTextContent('Follows the crew default (now Kiro Agent).')
  })

  it('lists the pinned harness models, not the install list', async () => {
    readout({ acp_backend: 'claude', harness: 'claude' })
    setup(member())
    await waitFor(() => expect(mockApi.harnessModels).toHaveBeenCalledWith('claude'))
    const options = open('model')
    await within(options).findByRole('radio', { name: 'sonnet-cc' })
    expect(within(options).queryByRole('radio', { name: 'claude-haiku-4' })).toBeNull()
  })

  it('says so when the pinned harness cannot start, naming it', async () => {
    localStorage.setItem(PREVIEW_CREWMATE_HARNESS, '1')
    readout({ acp_backend: 'codex', harness: '', harness_error: 'crewmate_harness_unavailable: oncall cannot start' })
    setup(member())
    const notice = await screen.findByTestId('crew-profile-settings-harness-error')
    // The harness is named, never its raw id.
    expect(notice).toHaveTextContent('oncall is set to run on OpenAI Codex')
    expect(notice).not.toHaveTextContent('codex,')
    expect(rowValue('acp_backend')).toHaveTextContent('OpenAI Codex')
    // The way out sits beside the notice: it opens the harness picker.
    fireEvent.click(screen.getByTestId('crew-profile-settings-harness-fix'))
    expect(page('acp_backend')).toBeInTheDocument()
    // The pinned option agrees with the notice: it can't start here, and never
    // says it can chat (codex is not a worker harness either).
    const pinned = within(page('acp_backend')).getByRole('radio', { name: /OpenAI Codex/ })
    expect(pinned).toHaveTextContent("Can't start here.")
    expect(pinned).not.toHaveTextContent('can chat')
  })
})
