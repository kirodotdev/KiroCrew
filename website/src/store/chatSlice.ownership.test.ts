/**
 * Who owns what in the chat state.
 *
 * `chatSlice.ts` is the one `createSlice` wiring point and the module every
 * consumer imports chat state through. The reducer families, thunks and
 * selectors it composes live in `store/chat/*`. These pins keep that shape:
 *
 * - the owners never import the facade, and their runtime imports form no
 *   cycle, so each one loads on its own and the facade stays the only root;
 * - code outside `src/store` (the app, the integration tests and the capture
 *   harnesses) reaches the owners only through the facade, so a
 *   `vi.mock('../store/chatSlice', ...)` factory still replaces every export;
 * - each reducer family owns a disjoint set of action names, and together with
 *   the few reducers wired inline they are exactly the slice's action creators;
 * - every name the facade re-exports is the owner's own binding, not a copy.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import * as facade from './chatSlice'
import * as activity from './chat/activity'
import * as automations from './chat/automations'
import * as composerCards from './chat/composerCards'
import * as lifecycle from './chat/lifecycle'
import * as mcpApps from './chat/mcpApps'
import * as messages from './chat/messages'
import * as paging from './chat/paging'
import * as queue from './chat/queue'
import * as runState from './chat/runState'
import * as selectors from './chat/selectors'
import * as side from './chat/side'
import * as slotCache from './chat/slotCache'
import * as slotRefresh from './chat/slotRefresh'
import * as slotSwitch from './chat/slotSwitch'
import * as subagents from './chat/subagents'
import * as transcript from './chat/transcript'
import * as wire from './chat/wire'
import * as windowWalk from './chat/windowWalk'
import * as workflows from './chat/workflows'

const CHAT_DIR = join(__dirname, 'chat')
const WEBSITE = join(__dirname, '..', '..')
/** Every tree whose modules import chat state: the app and the two test/capture harnesses beside it. */
const IMPORTING_ROOTS = ['src', 'integration', 'capture']

/** Every `import`/`export ... from` in a module: its specifier and whether it is type-only. */
function importsOf(text: string): Array<{ spec: string; typeOnly: boolean }> {
  const out: Array<{ spec: string; typeOnly: boolean }> = []
  const re = /^(?:import|export)\s+(type\s+)?[^'";]*?\bfrom\s+'([^']+)'/gm
  for (const m of text.matchAll(re)) out.push({ spec: m[2], typeOnly: Boolean(m[1]) })
  return out
}

const ownerFiles = readdirSync(CHAT_DIR).filter(n => n.endsWith('.ts') && !n.includes('.test.'))
const ownerSource = new Map(ownerFiles.map(n => [n.replace(/\.ts$/, ''), readFileSync(join(CHAT_DIR, n), 'utf8')]))

describe('store/chat owners', () => {
  it('never import the chatSlice facade', () => {
    const hits = [...ownerSource].flatMap(([name, text]) =>
      importsOf(text).filter(i => /(^|\/)chatSlice$/.test(i.spec)).map(i => `${name} -> ${i.spec}`))
    expect(hits).toEqual([])
  })

  it('form an acyclic runtime import graph', () => {
    const edges = new Map<string, string[]>()
    for (const [name, text] of ownerSource) {
      edges.set(name, importsOf(text)
        .filter(i => !i.typeOnly && i.spec.startsWith('./'))
        .map(i => i.spec.slice(2)))
    }
    const done = new Set<string>()
    const cycles: string[] = []
    const visit = (node: string, path: string[]) => {
      if (path.includes(node)) { cycles.push([...path.slice(path.indexOf(node)), node].join(' -> ')); return }
      if (done.has(node)) return
      for (const next of edges.get(node) ?? []) visit(next, [...path, node])
      done.add(node)
    }
    for (const name of edges.keys()) visit(name, [])
    expect(cycles).toEqual([])
  })

  it('are reached from outside src/store only through the facade', () => {
    const hits: string[] = []
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const p = join(dir, name)
        if (statSync(p).isDirectory()) { walk(p); continue }
        if (!/\.(ts|tsx)$/.test(name)) continue
        const rel = relative(WEBSITE, p).split('\\').join('/')
        if (rel.startsWith('src/store/')) continue
        const text = readFileSync(p, 'utf8')
        if (/['"][^'"]*\bstore\/chat\/[^'"]+['"]/.test(text)) hits.push(rel)
      }
    }
    for (const root of IMPORTING_ROOTS) walk(join(WEBSITE, root))
    expect(hits).toEqual([])
  })
})

/** Reducers the facade wires inline next to the families: small UI flags and the live frame reducer. */
const INLINE_REDUCERS = [
  'setPendingInput', 'stageToMainComposer', 'setAgentSwitchNotice', 'setVoicePlaying', 'setVoiceAudio',
  'requestSlotReveal', 'clearSlotReveal', 'requestFolderReveal', 'sseChatMessage', 'setCloseRefused',
]

const FAMILIES: Record<string, Record<string, unknown>> = {
  runState: runState.runStateReducers,
  slotCache: slotCache.slotCacheReducers,
  composerCards: composerCards.composerCardReducers,
  messages: messages.messageReducers,
  queue: queue.queueReducers,
  activity: activity.activityReducers,
  subagents: subagents.subagentReducers,
  automations: automations.automationReducers,
  side: side.sideReducers,
  workflows: workflows.workflowReducers,
  mcpApps: mcpApps.mcpAppReducers,
  lifecycle: lifecycle.historyNoticeReducers,
}

describe('reducer families', () => {
  it('own disjoint action names', () => {
    const owner = new Map<string, string>()
    const clashes: string[] = []
    for (const [family, map] of Object.entries(FAMILIES)) {
      for (const key of Object.keys(map)) {
        if (owner.has(key)) clashes.push(`${key}: ${owner.get(key)} and ${family}`)
        owner.set(key, family)
      }
    }
    for (const key of INLINE_REDUCERS) if (owner.has(key)) clashes.push(`${key}: ${owner.get(key)} and inline`)
    expect(clashes).toEqual([])
  })

  it('with the inline reducers are exactly the slice action creators the facade exports', () => {
    const wired = [...Object.values(FAMILIES).flatMap(m => Object.keys(m)), ...INLINE_REDUCERS].sort()
    const surface = facade as unknown as Record<string, { type?: unknown; typePrefix?: unknown }>
    const exported = Object.keys(facade)
      .filter(n => typeof surface[n]?.type === 'string' && surface[n]?.typePrefix === undefined)
      // The switch-notice pair is a createAction owned by slotSwitch, handled in its extra reducers.
      .filter(n => n !== 'clearSwitchSlotGone')
      .sort()
    expect(wired).toEqual(exported)
    for (const name of wired) expect(surface[name].type, name).toBe(`chat/${name}`)
  })

  it('handle the switch-notice pair slotSwitch owns outside the reducer map', () => {
    const init = facade.default(undefined, { type: '@@INIT' })
    const set = facade.default(init, { type: 'chat/setSwitchSlotGone', payload: { name: 'x', kind: 'gone' } })
    expect(set.switchSlotGone).toEqual({ name: 'x', kind: 'gone' })
    expect(facade.default(set, facade.clearSwitchSlotGone()).switchSlotGone).toBeNull()
  })
})

/** owner module -> the names the facade re-exports from it. */
const REEXPORTS: Array<[string, Record<string, unknown>, string[]]> = [
  ['wire', wire, ['clampToolOutput', 'TOOL_OUTPUT_MAX_CHARS', 'queueEntryAttachments', 'queueEntryQuote']],
  ['transcript', transcript, ['floorForGen', 'hasUnidentifiedDurableRow', 'raiseChunkSeq', 'snapshotChunkGen', 'snapshotChunkSeq', 'transcriptTsMs']],
  ['paging', paging, [
    'OLDER_PAGE_LIMIT', 'OLDER_WALK_PAGE_LIMIT', 'SLOT_DETAIL_MAX_LIMIT', 'PANE_HYDRATE_LIMIT', 'REFRESH_LIMIT_CEILING',
    'slotSwitchFetchLimit', 'slotCoverageShortfall', 'countMatchedFetchLimit', 'isSupersededPagingRejection', 'abortActiveOlderFetch',
  ]],
  ['composerCards', composerCards, ['FOLDER_SUGGESTION_MAX_TURNS', 'capturePendingAskId', 'isAnsweredQuestionEnding', 'pendingQuestionFor', 'shouldResolveAskOnSend']],
  ['mcpApps', mcpApps, ['mcpAppKey']],
  ['subagents', subagents, [
    'isAwaitingSpawnApproval', 'selectSidebarApprovalCounts', 'selectSidebarStartedSubagentCounts', 'selectSidebarSubagentCounts', 'selectSlotPendingSpawnApprovals',
    'selectSlotSubagents', 'selectSlotSubagentsActive', 'selectSubagentActivityCount',
  ]],
  ['workflows', workflows, ['WORKFLOW_TERMINAL_STATUSES', 'isTerminalWorkflowStatus', 'selectSidebarWorkflowActive', 'selectSidebarWorkflowActiveKeys']],
  ['automations', automations, ['selectAutomationForSlot', 'selectSidebarAutomationRunningKeys']],
  ['side', side, ['queueEditBroadcastAt']],
  ['selectors', selectors, [
    'selectActiveSlotProject', 'selectComposerBusy', 'selectContinuable', 'selectSendConfirmed', 'selectSlotMessages',
    'selectSlotPendingApproval', 'selectSlotRunEpoch', 'selectSlotStreamState', 'selectSlotToolLog', 'selectTrailingSendUnconfirmed',
    'selectTurnInterrupted', 'QUIET_END_SERVER', 'QUIET_END_TOOL',
  ]],
  ['slotSwitch', slotSwitch, ['clearSwitchSlotGone', 'switchSlot', 'switchSlotNoticeCopy']],
  ['slotRefresh', slotRefresh, ['refreshSlot', 'warmSlotCache']],
  ['windowWalk', windowWalk, ['WINDOW_WALK_MAX_PAGES']],
  ['lifecycle', lifecycle, ['createSlot', 'deleteHistorySession', 'fetchHistory', 'forkSlot', 'resumeFromHistory']],
]

/** Names the facade itself defines rather than re-exports. */
const FACADE_OWN = ['batchedTextAboveFloor', 'default', 'deleteSlot', 'loadOlderMessages', 'loadOlderSlotMessages', 'missedChunkMarker', 'requestStop']

describe('facade re-exports', () => {
  const surface = facade as unknown as Record<string, unknown>

  it('are the owner bindings themselves', () => {
    for (const [owner, mod, names] of REEXPORTS) {
      for (const name of names) expect(surface[name], `${owner}.${name}`).toBe(mod[name])
    }
  })

  it('together with the slice actions and the facade own names cover every export', () => {
    const reexported = new Set(REEXPORTS.flatMap(([, , names]) => names))
    const actions = new Set([...Object.values(FAMILIES).flatMap(m => Object.keys(m)), ...INLINE_REDUCERS])
    const unaccounted = Object.keys(facade).filter(n => !reexported.has(n) && !actions.has(n) && !FACADE_OWN.includes(n))
    expect(unaccounted).toEqual([])
  })
})
