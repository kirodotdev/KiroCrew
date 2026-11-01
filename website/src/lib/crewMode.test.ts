import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  DEFAULT_CREW_MODE_AGENT, autoOpenRows, crewModeAgent, crewModeView, openingHidesSessionList, isConductorAgent, isDefaultAgentName, runsCrewMode,
  memoryModeAllowsCrewMode, runsConductor, resetCrewModeCatalog, setCrewModeCatalog, withoutConductors, agentCycleTarget,
  gatewayRunsCrewMode, CREW_APPROVAL_SURFACES, crewApprovalDecidableHere, crewModeEnabled, crewModeRoot, crewRootOf,
} from './crewMode'
import { PREVIEW_CREW_MODE, setPreviewFlag } from '../utils/previewFlags'
import { crewMemberKeys } from '../store/dashboard/unread'
import { selectPersonSlots } from '../store/personSlots'
import { selectUnreadByMode } from '../store/dashboardSlice'
import { crewModePreviewOn } from '../test/crewModePreview'

afterEach(() => resetCrewModeCatalog())

/** The conductors a Crew Mode gateway lists in its catalog today. */
const GATEWAY_CONDUCTORS = ['kirocrew-conductor', 'kirocrew-ledger-conductor', 'kirocrew-pipeline-conductor', 'kirocrew-security-conductor', 'kirocrew-team-lead']

describe('crewMode', () => {
  it('runs on the shipped conductor until the catalog says otherwise', () => {
    expect(DEFAULT_CREW_MODE_AGENT).toBe('kirocrew-conductor')
    expect(crewModeAgent()).toBe('kirocrew-conductor')
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
    // A chat already on a conductor has no picker to mirror (the server refuses
    // to move a Crew Mode root), so the chord does nothing there.
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

describe('the Crew Mode preview', () => {
  const rows = [
    { key: 'root', crew_mode_root: true, agent: 'kirocrew-conductor' },
    { key: 'member', crew_root: 'root' },
    { key: 'plain' },
  ]
  // The file turns the preview on for every test; these turn it off again.
  beforeEach(() => {
    localStorage.removeItem(PREVIEW_CREW_MODE)
    setCrewModeCatalog('kirocrew-conductor', GATEWAY_CONDUCTORS)
  })

  it('is off by default', () => {
    expect(crewModeEnabled()).toBe(false)
  })

  it('off, answers as a gateway without Crew Mode: no conductor, no switch, every picker row', () => {
    expect(gatewayRunsCrewMode()).toBe(false)
    expect(isConductorAgent('kirocrew-conductor')).toBe(false)
    expect(runsCrewMode('kirocrew-conductor', 'template')).toBe(false)
    const picker = [{ name: 'kirocrew' }, { name: 'kirocrew-conductor' }]
    expect(withoutConductors(picker)).toEqual(picker)
  })

  it('off, the chord never moves a plain chat onto a conductor the gateway would run as Crew Mode', () => {
    const picker = [{ name: 'kirocrew' }, { name: 'kirocrew-conductor' }, { name: 'research' }]
    expect(agentCycleTarget(picker, 'kirocrew', 'template', 1)).toBe('research')
    expect(agentCycleTarget(picker, 'kirocrew', 'template', -1)).toBe('research')
    expect(agentCycleTarget(picker, 'research', 'template', 1)).toBe('kirocrew')
    expect(agentCycleTarget([{ name: 'kirocrew-conductor' }], 'kirocrew', 'template', 1)).toBeNull()
    // A crewmate is kept whatever its name.
    const withMate = [{ name: 'kirocrew' }, { name: 'kirocrew-conductor', selection_kind: 'member' }]
    expect(agentCycleTarget(withMate, 'kirocrew', 'template', 1)).toBe('kirocrew-conductor')
  })

  it('off, the chord stops only on a chat the gateway locks: the raw crew_mode_root stamp', () => {
    const picker = [{ name: 'kirocrew' }, { name: 'kirocrew-conductor' }, { name: 'research' }]
    // A Crew Mode root (the server's own stamp): the gateway refuses every switch.
    expect(agentCycleTarget(picker, 'kirocrew-conductor', 'template', 1, undefined, true)).toBeNull()
    expect(agentCycleTarget(picker, 'kirocrew-conductor', 'template', -1, undefined, true)).toBeNull()
    // A conductor chat that is no root (Incognito, Temporary, dispatched): the gateway
    // lets it switch, so the chord moves it, never onto a conductor.
    expect(agentCycleTarget(picker, 'kirocrew-conductor', 'template', 1)).toBe('kirocrew')
    expect(agentCycleTarget(picker, 'kirocrew-conductor', 'template', -1)).toBe('research')
  })

  it('off, reads every Crew Mode stamp as absent, so a crew member is an ordinary chat', () => {
    expect(crewModeRoot(rows[0])).toBe(false)
    expect(crewRootOf(rows[1])).toBe('')
    expect(crewModeView(rows)).toBe(rows)
    expect(crewModeView(rows, true)).toEqual([])
    expect(autoOpenRows(rows).map(r => r.key)).toEqual(['root', 'member', 'plain'])
    expect(openingHidesSessionList(rows[1], rows)).toBe(false)
    for (const surface of CREW_APPROVAL_SURFACES) expect(crewApprovalDecidableHere(rows[1], surface)).toBe(true)
    expect(crewMemberKeys(rows).size).toBe(0)
  })

  it('on, is Crew Mode exactly as before the flag', () => {
    setPreviewFlag(PREVIEW_CREW_MODE, true)
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
    expect(select(state)).toBe(2)
    setPreviewFlag(PREVIEW_CREW_MODE, true)
    // The crew member is its root's Crew panel's to show, so it no longer counts.
    expect(select(state)).toBe(1)
    setPreviewFlag(PREVIEW_CREW_MODE, false)
    expect(select(state)).toBe(2)
  })

  it('the person\'s list follows a flip without a new slot list', () => {
    const state = { dashboard: { slots: rows }, chat: {} } as unknown as Parameters<typeof selectPersonSlots>[0]
    expect(selectPersonSlots(state).map(r => r.key)).toEqual(['root', 'member', 'plain'])
    setPreviewFlag(PREVIEW_CREW_MODE, true)
    expect(selectPersonSlots(state).map(r => r.key)).toEqual(['root', 'plain'])
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
