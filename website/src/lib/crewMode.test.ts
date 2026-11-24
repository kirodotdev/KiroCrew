import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  DEFAULT_CREW_MODE_AGENT, autoOpenRows, crewModeAgent, crewModeView, openingHidesSessionList, isConductorAgent, isDefaultAgentName, runsCrewMode,
  memoryModeAllowsCrewMode, runsConductor, resetCrewModeCatalog, setCrewModeCatalog, withoutConductors, agentCycleTarget,
  gatewayRunsCrewMode, CREW_APPROVAL_SURFACES, crewApprovalDecidableHere, crewModeEnabled, crewModeRoot, crewRootOf,
  liveMateThreads, mateThreadsHidingSessions, busySlotKeys, slackRowHidden, showsAsCrewMode,
} from './crewMode'
import { applyLockedControls } from '../utils/editionLocks'
import { crewMemberKeys } from '../store/dashboard/unread'
import { selectPersonSlots } from '../store/personSlots'
import { selectUnreadByMode } from '../store/dashboardSlice'
import { turnOffCrewModeAsLts } from '../test/crewModeOff'

afterEach(() => resetCrewModeCatalog())

/** The conductors a Crew Mode gateway lists in its catalog today. */
const GATEWAY_CONDUCTORS = ['kirocrew-conductor', 'kirocrew-ledger-conductor', 'kirocrew-pipeline-conductor', 'kirocrew-security-conductor', 'kirocrew-team-lead']

describe('crewMode', () => {
  it('runs on the shipped conductor until the catalog says otherwise', () => {
    expect(DEFAULT_CREW_MODE_AGENT).toBe('kirocrew-conductor')
    expect(crewModeAgent()).toBe('kirocrew-conductor')
  })

  it('treats the default conductor as conductor mode once a Crew Mode catalog lands', () => {
    setCrewModeCatalog(undefined, GATEWAY_CONDUCTORS)
    expect(crewModeAgent()).toBe(DEFAULT_CREW_MODE_AGENT)
    expect(isConductorAgent(DEFAULT_CREW_MODE_AGENT)).toBe(true)
    expect(runsCrewMode(DEFAULT_CREW_MODE_AGENT, 'template')).toBe(true)
    // The team lead stays a conductor too, for a person who set it.
    expect(runsCrewMode('kirocrew-team-lead', 'template')).toBe(true)
  })

  it('swaps the conductor to the one the catalog names', () => {
    setCrewModeCatalog('better-conductor', ['kirocrew-conductor', 'kirocrew-team-lead'])
    expect(crewModeAgent()).toBe('better-conductor')
    // The configured agent is a conductor even when the family list omits it.
    expect(isConductorAgent('better-conductor')).toBe(true)
    expect(isConductorAgent('kirocrew-team-lead')).toBe(true)
    expect(isConductorAgent('kirocrew')).toBe(false)
  })

  it('never treats a crewmate as a conductor, whatever its name', () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])
    expect(runsConductor('kirocrew-conductor', 'template')).toBe(true)
    expect(runsConductor('kirocrew-conductor', undefined)).toBe(true)
    expect(runsConductor('kirocrew-conductor', 'member')).toBe(false)
  })

  it('drops conductor templates from a normal chat picker and keeps crewmates, whatever their name', () => {
    // The composer's Crew Mode switch is the way onto a conductor, so the picker
    // lists none of the catalog's conductor set; a crewmate row stays.
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor', 'kirocrew-pipeline-conductor'])
    const rows = [
      { name: 'kirocrew', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'template' },
      { name: 'kirocrew-pipeline-conductor', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'member' },
    ]
    expect(withoutConductors(rows)).toEqual([
      { name: 'kirocrew', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'member' },
    ])
  })

  it('leaves the roster copy of a conductor template out with it, and keeps a crewmate the person created', () => {
    // `agent.crew_mode_agent` names a custom agent the agents sync copied into
    // the roster: the copy runs the template of its name, so leaving only the
    // template out listed the conductor under Bots.
    setCrewModeCatalog('acme-custom', ['kirocrew-conductor'])
    const plain = { name: 'kirocrew', selection_kind: 'template', kiro_agent: 'kirocrew' }
    const bot = { name: 'research', selection_kind: 'member', kiro_agent: 'research', source: 'kirocrew', memory_store: 'research' }
    const rows = [
      plain,
      { name: 'acme-custom', selection_kind: 'template', kiro_agent: 'acme-custom' },
      { name: 'acme-custom', selection_kind: 'member', kiro_agent: 'acme-custom', source: 'package', memory_store: 'default' },
      bot,
    ]
    expect(withoutConductors(rows)).toEqual([plain, bot])
    // The default-agent pick's copy (`kirocrew`, the shared store) goes the same way;
    // a crewmate the person created on the conductor template, under its name, stays.
    const picked = { name: 'acme-custom', selection_kind: 'member', kiro_agent: 'acme-custom', source: 'kirocrew', memory_store: 'default' }
    const created = { name: 'acme-custom', selection_kind: 'member', kiro_agent: 'acme-custom', source: 'kirocrew', memory_store: 'acme-custom' }
    expect(withoutConductors([plain, picked])).toEqual([plain])
    expect(withoutConductors([plain, created])).toEqual([plain, created])
    // The chord skips the copy too: from `kirocrew` it lands on the Bot, never the copy.
    const folded = [plain, rows[2], bot]
    expect(agentCycleTarget(folded, 'kirocrew', 'template', 1)).toBe('research')
    expect(agentCycleTarget(folded, 'research', 'member', 1)).toBe('kirocrew')
  })

  it('opening a worker the list leaves out hides the sessions list; a root or a plain chat never does', () => {
    // `openingHidesSessionList` reads `crewModeView`, on with this part: a worker
    // is no row of the list, so opening it hides the list beside the Crew panel.
    const rows = [{ key: 'a' }, { key: 'w' }]
    expect(openingHidesSessionList({ crew_root: 'a' }, rows)).toBe(true)
    expect(openingHidesSessionList({}, rows)).toBe(false)
    expect(openingHidesSessionList(undefined, rows)).toBe(false)
    // Its root closed, the worker is a row of the list, so opening it keeps the list.
    expect(openingHidesSessionList({ crew_root: 'gone' }, rows)).toBe(false)
  })

  it('keeps workers out of the person list and can show only roots', () => {
    const rows = [
      { key: 'a', crew_mode_root: true },
      { key: 'b', crew_root: 'a' },
      { key: 'c' },
    ]
    expect(crewModeView(rows).map(r => r.key)).toEqual(['a', 'c'])
    expect(crewModeView(rows, true).map(r => r.key)).toEqual(['a'])
  })

  it('keeps a worker whose root is closed, since no Crew panel lists it', () => {
    const rows = [
      { key: 'w', crew_root: 'closed-root', pending_approval: true },
      { key: 'c' },
    ]
    expect(crewModeView(rows).map(r => r.key)).toEqual(['w', 'c'])
  })

  it('treats the empty name and the configured default as the default agent', () => {
    expect(isDefaultAgentName('', 'default')).toBe(true)
    expect(isDefaultAgentName('default', 'default')).toBe(true)
    expect(isDefaultAgentName('custom-x', 'default')).toBe(false)
    // `kirocrew` is the default only while none is configured.
    expect(isDefaultAgentName('kirocrew', 'default')).toBe(false)
    expect(isDefaultAgentName('kirocrew', '')).toBe(true)
  })

  it('needs a Persistent chat, since an Incognito or Temporary one cannot open sessions', () => {
    expect(memoryModeAllowsCrewMode(undefined)).toBe(true)
    expect(memoryModeAllowsCrewMode('persistent')).toBe(true)
    expect(memoryModeAllowsCrewMode('incognito')).toBe(false)
    expect(memoryModeAllowsCrewMode('temporary')).toBe(false)
  })

  it('cycles a normal chat past every conductor, so a chord never turns Crew Mode on', () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor', 'kirocrew-team-lead'])
    const agents = [
      { name: 'kirocrew', selection_kind: 'template' },
      { name: 'kirocrew-conductor', selection_kind: 'template' },
      { name: 'research', selection_kind: 'template' },
      { name: 'kirocrew-team-lead', selection_kind: 'template' },
    ]
    // The picker is the way onto a conductor; the chord skips them both ways.
    expect(agentCycleTarget(agents, 'kirocrew', 'template', 1)).toBe('research')
    expect(agentCycleTarget(agents, 'research', 'template', 1)).toBe('kirocrew')
    expect(agentCycleTarget(agents, 'kirocrew', 'template', -1)).toBe('research')
    // A chord never takes a chat out of conductor mode, so it does nothing there.
    expect(agentCycleTarget(agents, 'kirocrew-conductor', 'template', 1)).toBeNull()
    expect(agentCycleTarget([], 'kirocrew', 'template', 1)).toBeNull()
  })
})

describe('runsCrewMode', () => {
  it('is every conductor in the catalog list', () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor', 'kirocrew-pipeline-conductor', 'kirocrew-ledger-conductor'])
    expect(runsCrewMode('kirocrew-conductor', 'template')).toBe(true)
    expect(runsCrewMode('kirocrew-ledger-conductor', 'template')).toBe(true)
    expect(runsCrewMode('kirocrew-pipeline-conductor', 'template')).toBe(true)
    expect(isConductorAgent('kirocrew-ledger-conductor')).toBe(true)
    expect(runsCrewMode('kirocrew-conductor', 'member')).toBe(false)
    expect(runsCrewMode('kirocrew', 'template')).toBe(false)
  })

  it('names no conductor before the catalog lands, so a restored conductor chat is an ordinary chat until then', () => {
    resetCrewModeCatalog()
    expect(gatewayRunsCrewMode()).toBe(false)
    for (const name of GATEWAY_CONDUCTORS) expect(runsCrewMode(name, 'template')).toBe(false)
    // The stamp a Crew Mode chat carries is recognized on its own.
    expect(runsCrewMode('kirocrew-conductor', 'template', 'kirocrew-conductor')).toBe(true)
  })

  it('keeps no client copy of the conductor set: a catalog with no list counts its agent alone', () => {
    setCrewModeCatalog('kirocrew-conductor', undefined)
    expect(gatewayRunsCrewMode()).toBe(true)
    expect(runsCrewMode('kirocrew-conductor', 'template')).toBe(true)
    expect(runsCrewMode('kirocrew-team-lead', 'template')).toBe(false)
  })

  it('names no conductor on a gateway without Crew Mode, so nothing is hidden there', () => {
    // A catalog with neither Crew Mode field: the gateway predates Crew Mode.
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])
    setCrewModeCatalog(undefined, undefined)
    expect(gatewayRunsCrewMode()).toBe(false)
    for (const name of GATEWAY_CONDUCTORS) {
      expect(runsCrewMode(name, 'template')).toBe(false)
      expect(isConductorAgent(name)).toBe(false)
    }
    // The picker keeps every template it shows today, conductors included.
    const rows = GATEWAY_CONDUCTORS.map(name => ({ name, selection_kind: 'template' }))
    expect(withoutConductors(rows)).toEqual(rows)
    // The agent chord cycles over the whole list and lands on a conductor as today.
    expect(agentCycleTarget(rows, 'kirocrew-conductor', 'template', 1)).toBe('kirocrew-ledger-conductor')
    // The switch still knows which agent it would turn on.
    expect(crewModeAgent()).toBe(DEFAULT_CREW_MODE_AGENT)
  })

  it('keeps a stamped chat in Crew Mode until the gateway moves it', () => {
    setCrewModeCatalog('new-one', ['new-one'])
    expect(runsCrewMode('old-one', 'template', 'old-one')).toBe(true)
    expect(runsCrewMode('kirocrew', 'template', 'old-one')).toBe(false)
    expect(runsCrewMode('old-one', 'template', '')).toBe(false)
  })
})

describe('showsAsCrewMode -- the composer agrees with the gateway (#39260)', () => {
  it('is a conductor root or a crew member on a conductor, never a chat opened outside any crew', () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])
    const on = { agent: 'kirocrew-conductor', agent_kind: 'template' }
    // The gateway's own root stamp decides, never the client's reading of the tree.
    expect(showsAsCrewMode({ ...on, crew_mode_root: true })).toBe(true)
    expect(showsAsCrewMode({ ...on, crew_mode_root: false, crew_root: 'root-1' })).toBe(true)
    expect(showsAsCrewMode({ ...on, crew_mode_root: false })).toBe(false)
    // No stamp yet (a switch's own answer): keeps the conductor's look.
    expect(showsAsCrewMode(on)).toBe(true)
    expect(showsAsCrewMode({ agent: 'kirocrew-worker', agent_kind: 'template', crew_mode_root: false, crew_root: 'root-1' })).toBe(false)
    expect(showsAsCrewMode(undefined)).toBe(false)
  })
})

describe('agentCycleTarget and the Crew Mode stamp', () => {
  it('does not cycle a chat that stays in Crew Mode on its stamped conductor', () => {
    setCrewModeCatalog('new-one', ['new-one'])
    const agents = [{ name: 'kirocrew' }, { name: 'old-one' }, { name: 'helper' }]
    expect(agentCycleTarget(agents, 'old-one', 'template', 1, 'old-one')).toBeNull()
    expect(agentCycleTarget(agents, 'old-one', 'template', -1, 'old-one')).toBeNull()
    // Without the stamp the same chat is an ordinary one and cycles.
    expect(agentCycleTarget(agents, 'old-one', 'template', 1)).not.toBeNull()
  })
})

describe('autoOpenRows -- what the page may open on its own', () => {
  it('never offers a crew member, so a close or a first visit never lands on one', () => {
    const rows = [{ key: 'plain' }, { key: 'root', crew_mode_root: true }, { key: 'member', crew_root: 'root' }]
    expect(autoOpenRows(rows).map(s => s.key)).toEqual(['plain', 'root'])
    expect(autoOpenRows([{ key: 'member', crew_root: 'root' }])).toEqual([])
  })
})

describe('crewApprovalDecidableHere', () => {
  it('lets a crew member\'s approval be decided on its conductor\'s card alone', () => {
    const worker = { crew_root: 'chat-1-1' }
    for (const surface of CREW_APPROVAL_SURFACES) {
      expect(crewApprovalDecidableHere(worker, surface)).toBe(surface === 'conductor_card')
    }
  })

  it('leaves every other session its usual controls on every surface', () => {
    for (const surface of CREW_APPROVAL_SURFACES) {
      expect(crewApprovalDecidableHere({ crew_root: '' }, surface)).toBe(true)
      expect(crewApprovalDecidableHere({}, surface)).toBe(true)
      expect(crewApprovalDecidableHere(null, surface)).toBe(true)
    }
  })
})

describe('conductor mode, on in every build but LTS', () => {
  const rows = [
    { key: 'root', crew_mode_root: true, agent: 'kirocrew-conductor' },
    { key: 'member', crew_root: 'root' },
    { key: 'plain' },
  ]
  beforeEach(() => {
    setCrewModeCatalog('kirocrew-conductor', GATEWAY_CONDUCTORS)
  })

  it('is on with no preview turned on, and off in an LTS build', () => {
    expect(crewModeEnabled()).toBe(true)
    turnOffCrewModeAsLts()
    expect(crewModeEnabled()).toBe(false)
  })

  it('off (LTS), answers as a gateway without conductor mode: no conductor, no switch, every picker row', () => {
    turnOffCrewModeAsLts()
    expect(gatewayRunsCrewMode()).toBe(false)
    expect(isConductorAgent('kirocrew-conductor')).toBe(false)
    expect(runsCrewMode('kirocrew-conductor', 'template')).toBe(false)
    const picker = [{ name: 'kirocrew' }, { name: 'kirocrew-conductor' }]
    expect(withoutConductors(picker)).toEqual(picker)
  })

  it('off (LTS), the chord never moves a plain chat onto a conductor the gateway would run as Crew Mode', () => {
    turnOffCrewModeAsLts()
    const picker = [{ name: 'kirocrew' }, { name: 'kirocrew-conductor' }, { name: 'research' }]
    expect(agentCycleTarget(picker, 'kirocrew', 'template', 1)).toBe('research')
    expect(agentCycleTarget(picker, 'kirocrew', 'template', -1)).toBe('research')
    expect(agentCycleTarget(picker, 'research', 'template', 1)).toBe('kirocrew')
    expect(agentCycleTarget([{ name: 'kirocrew-conductor' }], 'kirocrew', 'template', 1)).toBeNull()
    // A crewmate is kept whatever its name.
    const withMate = [{ name: 'kirocrew' }, { name: 'kirocrew-conductor', selection_kind: 'member' }]
    expect(agentCycleTarget(withMate, 'kirocrew', 'template', 1)).toBe('kirocrew-conductor')
  })

  it('off (LTS), the chord stops only on a chat the gateway locks: the raw crew_mode_root stamp', () => {
    turnOffCrewModeAsLts()
    const picker = [{ name: 'kirocrew' }, { name: 'kirocrew-conductor' }, { name: 'research' }]
    // A Crew Mode root (the server's own stamp): the gateway refuses every switch.
    expect(agentCycleTarget(picker, 'kirocrew-conductor', 'template', 1, undefined, true)).toBeNull()
    expect(agentCycleTarget(picker, 'kirocrew-conductor', 'template', -1, undefined, true)).toBeNull()
    // A conductor chat that is no root (Incognito, Temporary, dispatched): the gateway
    // lets it switch, so the chord moves it, never onto a conductor.
    expect(agentCycleTarget(picker, 'kirocrew-conductor', 'template', 1)).toBe('kirocrew')
    expect(agentCycleTarget(picker, 'kirocrew-conductor', 'template', -1)).toBe('research')
  })

  it('off (LTS), reads every Crew Mode stamp as absent, so a crew member is an ordinary chat', () => {
    turnOffCrewModeAsLts()
    expect(crewModeRoot(rows[0])).toBe(false)
    expect(crewRootOf(rows[1])).toBe('')
    expect(crewModeView(rows)).toBe(rows)
    expect(crewModeView(rows, true)).toEqual([])
    expect(autoOpenRows(rows).map(r => r.key)).toEqual(['root', 'member', 'plain'])
    expect(openingHidesSessionList(rows[1], rows)).toBe(false)
    for (const surface of CREW_APPROVAL_SURFACES) expect(crewApprovalDecidableHere(rows[1], surface)).toBe(true)
    expect(crewMemberKeys(rows).size).toBe(0)
  })

  it('on, is conductor mode as it was behind the preview', () => {
    expect(gatewayRunsCrewMode()).toBe(true)
    expect(isConductorAgent('kirocrew-conductor')).toBe(true)
    expect(crewModeRoot(rows[0])).toBe(true)
    expect(crewRootOf(rows[1])).toBe('root')
    expect(crewModeView(rows).map(r => r.key)).toEqual(['root', 'plain'])
    expect([...crewMemberKeys(rows)]).toEqual(['member'])
  })

  it('the chat unread count follows a flip without a new slot list', () => {
    const dashboard = { slots: rows, unreadSlots: ['member', 'plain'] }
    const state = { dashboard } as unknown as Parameters<ReturnType<typeof selectUnreadByMode>>[0]
    const select = selectUnreadByMode('')
    // The crew member is its root's Crew panel's to show, so it does not count.
    expect(select(state)).toBe(1)
    turnOffCrewModeAsLts()
    expect(select(state)).toBe(2)
    applyLockedControls([])
    expect(select(state)).toBe(1)
  })

  it('the person\'s list follows a flip without a new slot list', () => {
    const state = { dashboard: { slots: rows }, chat: {} } as unknown as Parameters<typeof selectPersonSlots>[0]
    expect(selectPersonSlots(state).map(r => r.key)).toEqual(['root', 'plain'])
    turnOffCrewModeAsLts()
    expect(selectPersonSlots(state).map(r => r.key)).toEqual(['root', 'member', 'plain'])
  })
})

describe("a Bot's sessions in the person's lists", () => {
  const rows = [
    { key: 'mate', mode: 'member', agent: 'ada' },
    { key: 'mw', mate_root: 'mate' },
    { key: 'deep', mate_root: 'mate', mate_dispatched_by: 'mw' },
    { key: 'orphan', mate_root: 'gone' },
    { key: 'plain' },
  ]
  // A stamp an older gateway sent while a per-Bot switch existed changes nothing.
  const stale = rows.map(r => (r.key === 'mate' ? { ...r, show_sessions_in_list: true } : r))

  it('by default leaves out every session a live mate thread dispatched, and keeps the mate thread', () => {
    expect(crewModeView(rows).map(r => r.key)).toEqual(['mate', 'orphan', 'plain'])
    expect([...crewMemberKeys(rows)].sort()).toEqual(['deep', 'mw'])
    // The page never opens one on its own, whether or not its mate is live.
    expect(autoOpenRows(rows).map(r => r.key)).toEqual(['mate', 'plain'])
  })

  it('leaves them out whatever an old switch stamp on the mate thread says', () => {
    expect(crewModeView(stale).map(r => r.key)).toEqual(['mate', 'orphan', 'plain'])
    expect([...crewMemberKeys(stale)].sort()).toEqual(['deep', 'mw'])
  })

  it("keeps them in when the mate thread says its Bot's sessions are listed", () => {
    const listed = rows.map(r => (r.key === 'mate' ? { ...r, bot_sessions_listed: true } : r))
    expect(crewModeView(listed).map(r => r.key)).toEqual(['mate', 'mw', 'deep', 'orphan', 'plain'])
    expect([...crewMemberKeys(listed)]).toEqual([])
    expect(mateThreadsHidingSessions(listed).size).toBe(0)
    const state = { dashboard: { slots: listed }, chat: {} } as never
    expect(selectPersonSlots(state).map(r => r.key)).toContain('mw')
    // The page still never opens one on its own.
    expect(autoOpenRows(listed).map(r => r.key)).toEqual(['mate', 'plain'])
  })

  it('reads the mate threads from the full store when the list holds none', () => {
    const pageRows = rows.filter(r => r.mode !== 'member')
    expect(crewModeView(pageRows).map(r => r.key)).toEqual(['mw', 'deep', 'orphan', 'plain'])
    expect(crewModeView(pageRows, false, liveMateThreads(rows)).map(r => r.key)).toEqual(['orphan', 'plain'])
  })

  it("the person's list and unread count leave a mate's sessions out", () => {
    const select = selectUnreadByMode('')
    const state = { dashboard: { slots: stale, unreadSlots: ['mw', 'plain'] }, chat: {} } as never
    expect(selectPersonSlots(state).map(r => r.key)).not.toContain('mw')
    expect(select(state)).toBe(1)
  })
})

describe('the Slack session list setting (slack_list_hidden)', () => {
  const rows = [
    { key: 'slack_1.1', slack_list_hidden: true },
    { key: 'slack_D0DM', slack_list_hidden: true },
    { key: 'slack_2.2', slack_list_hidden: true, pending_approval: true },
    { key: 'slack_3.3', slack_list_hidden: true, needs_input: true },
    { key: 'chat-1', slack_linked: true },
    { key: 'chat-code' },
  ]

  it('leaves no row out while no row is stamped (the default)', () => {
    const plain = rows.map(({ key }) => ({ key }))
    expect(crewModeView(plain)).toEqual(plain)
    expect(slackRowHidden(plain[0])).toBe(false)
    expect(autoOpenRows(plain)).toEqual(plain)
    expect(crewMemberKeys(plain).size).toBe(0)
  })

  it('leaves stamped rows out and keeps the ones waiting on the person', () => {
    expect(crewModeView(rows).map(r => r.key)).toEqual(['slack_2.2', 'slack_3.3', 'chat-1', 'chat-code'])
  })

  it('keeps the open session listed, and only that one', () => {
    expect(crewModeView(rows, false, undefined, 'slack_1.1').map(r => r.key))
      .toEqual(['slack_1.1', 'slack_2.2', 'slack_3.3', 'chat-1', 'chat-code'])
    expect(slackRowHidden(rows[0], 'slack_1.1')).toBe(false)
    expect(slackRowHidden(rows[1], 'slack_1.1')).toBe(true)
  })

  it('a hidden conversation waiting on an approval stays where the person sees it', () => {
    // An approval or a question is what needs the person; the row stays listed
    // so its status dot and the Needs you lists still show it.
    expect(slackRowHidden(rows[2])).toBe(false)
    expect(slackRowHidden(rows[3])).toBe(false)
    expect(crewModeView(rows).find(r => r.key === 'slack_2.2')?.pending_approval).toBe(true)
  })

  it('the page never opens a stamped row on its own', () => {
    expect(autoOpenRows(rows).map(r => r.key)).toEqual(['chat-1', 'chat-code'])
  })

  it('hidden rows add nothing to the unread count, and come back with the next slot list', () => {
    const unreadSlots = ['slack_1.1', 'slack_D0DM', 'chat-1']
    const select = selectUnreadByMode('')
    const hidden = { dashboard: { slots: rows, unreadSlots }, chat: {} } as never
    expect([...crewMemberKeys(rows)].sort()).toEqual(['slack_1.1', 'slack_D0DM'])
    expect(select(hidden)).toBe(1)
    const listed = rows.map(r => ({ ...r, slack_list_hidden: undefined }))
    const shown = { dashboard: { slots: listed, unreadSlots }, chat: {} } as never
    expect(select(shown)).toBe(3)
  })

  it('the person\'s list follows the open session without a new slot list', () => {
    const slots = rows.slice(0, 2)
    expect(selectPersonSlots({ dashboard: { slots }, chat: { activeSlot: null } } as never)).toEqual([])
    expect(selectPersonSlots({ dashboard: { slots }, chat: { activeSlot: 'slack_D0DM' } } as never).map(r => r.key))
      .toEqual(['slack_D0DM'])
  })
})

describe('busySlotKeys -- a mate works while any session it dispatched does', () => {
  it('counts a busy worker toward its mate thread, and an idle one toward nothing', () => {
    const keys = busySlotKeys([
      { key: 'mate' },
      { key: 'w1', running: true, mate_root: 'mate' },
      { key: 'w2', mate_root: 'other' },
    ], {})
    expect([...keys].sort()).toEqual(['mate', 'w1'])
  })

  it('reads a running child and a queued child as busy too', () => {
    expect(busySlotKeys([{ key: 'w', subagents_running: true, mate_root: 'm1' }], {}).has('m1')).toBe(true)
    expect(busySlotKeys([{ key: 'w', mate_root: 'm2' }], { w: 1 }).has('m2')).toBe(true)
    expect(busySlotKeys([{ key: 'w', mate_root: 'm3' }], { w: 0 }).size).toBe(0)
  })
})
