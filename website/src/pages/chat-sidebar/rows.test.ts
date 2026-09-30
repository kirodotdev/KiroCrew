/**
 * The sidebar row model, at its interface: which rows pass the filters and in what
 * order, which lane and folder each lands in, and what each rendered row shows.
 *
 * The property everything here leans on is peer isolation: a live row from a connected
 * crew (`peer_id`) can carry a raw key byte-identical to a LOCAL slot's, and every piece
 * of local sidebar state is indexed by local key. The model checks the origin once, so a
 * peer row must come out the same whatever local state its key happens to collide with.
 */
import v8 from 'node:v8'
import vm from 'node:vm'
import { describe, it, expect, vi } from 'vitest'
import {
  buildSidebarRows, chipLabel, sameRowView,
  type ConductorRowView, type RowPlacement, type RowScene, type RowShell, type SidebarRowInputs, type SidebarRows,
} from './rows'
import type { SessionFilterKey, Slot } from './types'

const NOW = Date.parse('2026-10-05T12:00:00Z')
const MINUTE = 60_000
const DAY = 86_400_000
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString()

const local = (key: string, over: Partial<Slot> = {}): Slot =>
  ({ key, title: `Title ${key}`, running: false, last_turn_ts: iso(MINUTE), ...over }) as Slot
const peer = (key: string, over: Partial<Slot> = {}): Slot =>
  ({ key, title: `Peer ${key}`, running: false, last_turn_ts: iso(MINUTE), peer_id: 'inst-a', peer_name: 'astro', row_identity: `inst-a:${key}`, ...over }) as Slot

function clears() {
  return { tags: vi.fn(), search: vi.fn(), status: vi.fn(), folder: vi.fn(), hides: vi.fn() }
}

/** No crew group shows: a single-machine list, as the facade passes on the board. */
const NO_GROUPS: SidebarRowInputs['crewGroups'] = []

type InputOverrides = {
  local?: Partial<SidebarRowInputs['local']>
  filters?: Partial<{ [K in keyof SidebarRowInputs['filters']]: Partial<SidebarRowInputs['filters'][K]> }>
  sortKey?: SidebarRowInputs['sortKey']
  clock?: () => number
  frozen?: boolean
  clears?: SidebarRowInputs['clears']
  crewGroups?: SidebarRowInputs['crewGroups']
}

/** Inputs with nothing filtering, nothing pinned and no local state, overridden per case. */
function inputs(rows: Slot[], over: InputOverrides = {}): SidebarRowInputs {
  const f = over.filters ?? {}
  return {
    rows,
    local: {
      folderOf: {}, pinned: new Set(), pinnedRank: new Map(), unread: new Set(), running: new Set(), recent: new Set(),
      searchRanks: null,
      ...over.local,
    },
    filters: {
      tags: { resolved: new Set(), raw: new Set(), ...f.tags },
      search: { text: '', folderMatches: null, ...f.search },
      status: { active: new Set(), hidden: new Set(), paused: false, recentWindowMs: 60 * MINUTE, ...f.status },
      folders: { hiddenSubtree: new Set(), active: false, ...f.folders },
    },
    clears: over.clears ?? clears(),
    crewGroups: over.crewGroups ?? NO_GROUPS,
    sortKey: over.sortKey ?? 'date-desc',
    clock: over.clock ?? (() => NOW),
    frozen: over.frozen ?? false,
  }
}

const NO_BOOST = { idlePct: [], activePct: [], hoverPct: [], mutedColors: [] }
/** One shell object, as the facade builds one per change of any of its members. */
const SHELL: RowShell = {
  connected: true, mode: undefined, isMobile: false, colorMode: 'tint', defaultAgent: 'kirocrew',
  installedAgents: [], tagById: {}, paletteColors: [], boost: NO_BOOST, boostFor: () => NO_BOOST, recentTintCount: 3,
  dragInFlight: false, activeDraggedKey: null, activeDraggedPinnedIndex: -1,
}

function scene(over: Partial<Omit<RowScene, 'local'>> & { local?: Partial<RowScene['local']> } = {}): RowScene {
  return {
    activeSlot: null, crewWindow: null, shell: SHELL, drag: null,
    ...over,
    local: {
      recentRank: new Map(), subagentCounts: {}, subagentApprovalCounts: {}, poppedOut: new Set(), digits: null,
      renaming: { key: null, scope: null, value: '' }, revealFlash: null, nativeDragKey: null,
      ...over.local,
    },
  }
}

const AT: RowPlacement = { scope: 'list', navScope: 'list', holdContainer: 'tree:root', showDivider: false, orderStamp: 0, rowAnimEnabled: true }
const keys = (rows: Slot[]) => rows.map(s => s.peer_id ? `${s.peer_id}:${s.key}` : s.key)

// ── peer isolation ───────────────────────────────────────────────────────────

/** Every piece of local state the model and the row read, all set for local key `k`. */
const FULL_LOCAL: SidebarRowInputs['local'] = {
  folderOf: { k: 'F1' }, pinned: new Set(['k']), pinnedRank: new Map([['k', 0]]), unread: new Set(['k']),
  running: new Set(['k']), recent: new Set(['k']), searchRanks: null,
}
const FULL_SCENE_LOCAL: RowScene['local'] = {
  recentRank: new Map([['k', 1]]), subagentCounts: { k: 2 }, subagentApprovalCounts: { k: 1 }, poppedOut: new Set(['k']),
  digits: new Map([['k', '1']]), renaming: { key: 'k', scope: 'list', value: 'draft' },
  revealFlash: { kind: 'session', key: 'k', fading: false }, nativeDragKey: 'k',
}

describe('a peer row never carries local state', () => {
  const theLocal = local('k', { pinned: true, folder_id: 'F1' })
  const thePeer = peer('k', { running: true })

  it('gets the same view and window whatever local state its key collides with', () => {
    const empty = buildSidebarRows(inputs([theLocal, thePeer])).views(scene())
    const full = buildSidebarRows(inputs([theLocal, thePeer], { local: FULL_LOCAL }))
      .views(scene({ activeSlot: 'k', local: FULL_SCENE_LOCAL }))
    expect(full.view(thePeer, AT)).toEqual(empty.view(thePeer, AT))
    expect(full.windowOf(thePeer)).toEqual(empty.windowOf(thePeer))
    expect(full.view(thePeer, AT)).toMatchObject({
      identity: 'inst-a:k', localKey: '', peerId: 'inst-a', peerName: 'astro',
      isActive: false, isOut: false, isPinned: false, isUnread: false,
      recent: undefined, subagentCount: 0, subagentApprovalCount: 0, digitBadge: undefined, isRenaming: false,
      renamingHere: false, renameValue: '', revealFlash: null, pinnedOrderIndex: -1, pinnedReorderEnabled: false,
      opensElsewhere: null,
      // A peer row reports its OWN running flag, not the local set's answer for its key.
      isRunning: true,
    })
    expect(full.windowOf(thePeer).keepMounted).toBe(false)
  })

  it('while the local row it collides with carries every piece of it', () => {
    const views = buildSidebarRows(inputs([theLocal, thePeer], { local: FULL_LOCAL }))
      .views(scene({ activeSlot: 'k', local: FULL_SCENE_LOCAL }))
    expect(views.view(theLocal, AT)).toMatchObject({
      identity: 'k', localKey: 'k', peerId: undefined, peerName: undefined,
      isActive: true, isOut: true, isPinned: true, isUnread: true, isRunning: true,
      recent: 1, subagentCount: 2, subagentApprovalCount: 1, digitBadge: '1', isRenaming: true, renamingHere: true,
      renameValue: 'draft', revealFlash: 'flash', pinnedOrderIndex: 0, pinnedReorderEnabled: true,
    })
    expect(views.windowOf(theLocal).keepMounted).toBe(true)
  })

  it('names its crew by display name, falling back to the crew id', () => {
    const views = buildSidebarRows(inputs([])).views(scene())
    expect(views.view(peer('x', { peer_name: undefined }), AT).peerName).toBe('inst-a')
    // A stray peer_name on a LOCAL row is not an origin.
    expect(views.view(local('y', { peer_name: 'ghost' }), AT)).toMatchObject({ peerId: undefined, peerName: undefined })
  })

  it('is never active, filed, pinned or counted through the local key', () => {
    const rows = buildSidebarRows(inputs([theLocal, thePeer], { local: FULL_LOCAL }))
    const views = rows.views(scene({ activeSlot: 'k' }))
    expect(views.isActive(theLocal)).toBe(true)
    expect(views.isActive(thePeer)).toBe(false)
    expect(views.isActive(null)).toBe(false)
    expect(rows.folderOf(theLocal)).toBe('F1')
    expect(rows.folderOf(thePeer)).toBeUndefined()
    expect(rows.isPinned(theLocal)).toBe(true)
    expect(rows.isPinned(thePeer)).toBe(false)
    expect(rows.isRunning(peer('k', { running: false }))).toBe(false)
    expect(rows.folderTree.rowsIn('F1')).toEqual([theLocal])
    expect(keys(rows.lanes.ungrouped)).toEqual(['inst-a:k'])
    expect(keys(rows.lanes.board)).toEqual(['k'])
    expect(rows.lanes.boardPeerCount).toBe(1)
  })

  it('is never hidden by a local folder hide its key collides with', () => {
    const rows = buildSidebarRows(inputs([theLocal, thePeer], {
      local: FULL_LOCAL, filters: { folders: { hiddenSubtree: new Set(['F1']), active: true } },
    }))
    expect(rows.isRowFolderHidden(theLocal)).toBe(true)
    expect(rows.isRowFolderHidden(thePeer)).toBe(false)
    expect(keys(rows.lanes.flat)).toEqual(['inst-a:k'])
    expect(rows.revealBlockingFilters[3].hides(thePeer)).toBe(false)
  })

  describe('is active only for the crew window open on its own (crew, key) pair', () => {
    // The same raw key `k` on a local slot, on this crew and on another crew.
    const otherCrew = peer('k', { peer_id: 'inst-b', peer_name: 'beta', row_identity: 'inst-b:k' })
    const rows = buildSidebarRows(inputs([theLocal, thePeer, otherCrew]))
    const lit = (over: Parameters<typeof scene>[0]) => {
      const views = rows.views(scene(over))
      return [theLocal, thePeer, otherCrew].map(s => [views.isActive(s), views.view(s, AT).isActive, views.windowOf(s).keepMounted])
    }

    it('lights the peer row its window names, and neither the colliding local nor the other crew', () => {
      expect(lit({ activeSlot: 'k', crewWindow: { instanceId: 'inst-a', key: 'k' } }))
        .toEqual([[false, false, false], [true, true, true], [false, false, false]])
      expect(lit({ activeSlot: 'k', crewWindow: { instanceId: 'inst-b', key: 'k' } }))
        .toEqual([[false, false, false], [false, false, false], [true, true, true]])
    })

    it('lights no row for a window on a key no row carries', () => {
      expect(lit({ activeSlot: 'k', crewWindow: { instanceId: 'inst-a', key: 'other' } }))
        .toEqual([[false, false, false], [false, false, false], [false, false, false]])
    })

    it('lights the local row by the active slot only while no window is open', () => {
      expect(lit({ activeSlot: 'k' })).toEqual([[true, true, true], [false, false, false], [false, false, false]])
    })
  })

  it('does not ride into a search on a local rank, a local folder match or a local pin', () => {
    const rows = buildSidebarRows(inputs([theLocal, peer('k', { title: 'zzz' })], {
      local: { ...FULL_LOCAL, searchRanks: new Map([['k', 0]]) },
      filters: { search: { text: 'nomatch', folderMatches: new Set(['F1']) } },
    }))
    expect(keys(rows.filteredSlots)).toEqual(['k'])
  })

  it('a dragged LOCAL key keeps the colliding peer mounted too, the raw-key quirk the drag id carries', () => {
    const views = buildSidebarRows(inputs([theLocal, thePeer])).views(scene({ drag: { type: 'session', id: 'k' } }))
    expect(views.windowOf(thePeer).keepMounted).toBe(true)
  })
})

// ── filters ──────────────────────────────────────────────────────────────────

describe('every filter dimension answers the three consumers together', () => {
  const kept = local('kept', { tags: ['t1'], pinned: true, folder_id: 'F1', title: 'kept here' })
  const dropped = local('dropped', { tags: ['t2'], folder_id: 'F2', title: 'other' })
  const pinnedSet = new Set(['kept'])
  // Dimension order is the registry's: tags, search, status, folder, status hides.
  const cases: Array<[string, InputOverrides, { filtered: string[]; narrowed: boolean; hides: boolean[]; hidesKept?: boolean[] }]> = [
    ['nothing active', {}, { filtered: ['kept', 'dropped'], narrowed: false, hides: [false, false, false, false, false] }],
    ['tags', { filters: { tags: { resolved: new Set(['t1']), raw: new Set(['t1']) } } },
      { filtered: ['kept'], narrowed: true, hides: [true, false, false, false, false] }],
    ['search', { filters: { search: { text: 'kept' } } },
      { filtered: ['kept'], narrowed: true, hides: [false, true, false, false, false] }],
    ['status', { filters: { status: { active: new Set<SessionFilterKey>(['pinned']) } } },
      { filtered: ['kept'], narrowed: true, hides: [false, false, true, false, false] }],
    // The folder dimension narrows nothing and filters no row: it acts through the
    // lanes (`isRowFolderHidden`) and the folder render, and only a reveal asks it.
    ['folder', { filters: { folders: { hiddenSubtree: new Set(['F2']), active: true } } },
      { filtered: ['kept', 'dropped'], narrowed: false, hides: [false, false, false, true, false] }],
    ['a paused status chip', { filters: { status: { active: new Set<SessionFilterKey>(['pinned']), paused: true } } },
      { filtered: ['kept', 'dropped'], narrowed: false, hides: [false, false, false, false, false] }],
    // A hide answers for the row itself, and exempts no pinned row: `kept` is pinned.
    ['a status hide', { filters: { status: { hidden: new Set<SessionFilterKey>(['pinned']) } } },
      { filtered: ['dropped'], narrowed: true, hides: [false, false, false, false, false], hidesKept: [false, false, false, false, true] }],
    // Raw-vs-resolved: a stored tag the vocabulary cannot resolve filters nothing, yet a
    // reveal still clears it (it could be re-hiding the row mid-load). `kept` is pinned,
    // so the tag filter exempts it: a reveal of it must not clear the tag.
    ['an unresolved stored tag', { filters: { tags: { resolved: new Set(), raw: new Set(['ghost']) } } },
      { filtered: ['kept', 'dropped'], narrowed: false, hides: [true, false, false, false, false], hidesKept: [false, false, false, false, false] }],
  ]
  it.each(cases)('%s', (_name, over, expected) => {
    const rows = buildSidebarRows(inputs([kept, dropped], { ...over, local: { pinned: pinnedSet, folderOf: { kept: 'F1', dropped: 'F2' } } }))
    expect(keys(rows.filteredSlots)).toEqual(expected.filtered)
    expect(rows.listNarrowed).toBe(expected.narrowed)
    expect(rows.revealBlockingFilters.map(d => d.hides(dropped))).toEqual(expected.hides)
    expect(rows.revealBlockingFilters.map(d => d.hides(kept))).toEqual(expected.hidesKept ?? [false, false, false, false, false])
  })

  it.each([0, 1, 2, 3, 4])('reveal clear %i drops only its own dimension, the folder one along the row\'s own folder', index => {
    const c = clears()
    const rows = buildSidebarRows(inputs([kept, dropped], { clears: c, local: { folderOf: { dropped: 'F2' } } }))
    rows.revealBlockingFilters[index].clear(dropped)
    const called = [c.tags, c.search, c.status, c.folder, c.hides].map(fn => fn.mock.calls.length)
    expect(called).toEqual([0, 1, 2, 3, 4].map(i => (i === index ? 1 : 0)))
    if (index === 3) expect(c.folder).toHaveBeenCalledWith('F2')
  })

  it('clears no folder for a peer row whose key collides with a filed local one', () => {
    const c = clears()
    const rows = buildSidebarRows(inputs([local('k'), peer('k')], { clears: c, local: { folderOf: { k: 'F1' } } }))
    rows.revealBlockingFilters[3].clear(peer('k'))
    expect(c.folder).toHaveBeenCalledWith(undefined)
  })

  it('ORs the active status chips, each answered per origin', () => {
    const rows = buildSidebarRows(inputs([
      local('u'), local('p', { pinned: true }), local('r'), local('idle'),
      peer('u'), peer('p', { pinned: true }), peer('pr', { running: true }),
    ], {
      local: { unread: new Set(['u']), running: new Set(['r']) },
      filters: { status: { active: new Set<SessionFilterKey>(['unread', 'pinned', 'running']) } },
    }))
    expect(keys(rows.filteredSlots).sort()).toEqual(['inst-a:pr', 'p', 'r', 'u'])
  })

  it('reads a peer row\'s recency from its own timestamp against the injected clock', () => {
    const recentPeer = peer('fresh', { last_turn_ts: iso(10 * MINUTE) })
    const oldPeer = peer('old', { last_turn_ts: iso(2 * DAY) })
    const clock = vi.fn(() => NOW)
    const rows = buildSidebarRows(inputs([recentPeer, oldPeer], { clock, filters: { status: { active: new Set<SessionFilterKey>(['recent']) } } }))
    expect(keys(rows.filteredSlots)).toEqual(['inst-a:fresh'])
    expect(clock).toHaveBeenCalled()
  })

  it('searches title and source links once ranked, and key and agent before that', () => {
    const titled = local('t', { title: 'deploy plan' })
    const linked = local('l', { title: 'x', source_links: [{ number: 7031, provider: 'github', kind: 'change', url: 'u' }] as Slot['source_links'] })
    const byKey = local('deploy-key', { title: 'y' })
    const byAgent = local('a', { title: 'z', agent: 'deployer' })
    const unranked = buildSidebarRows(inputs([titled, linked, byKey, byAgent], { filters: { search: { text: 'deploy' } } }))
    expect(keys(unranked.filteredSlots).sort()).toEqual(['a', 'deploy-key', 't'])
    const ranked = buildSidebarRows(inputs([titled, linked, byKey, byAgent], {
      local: { searchRanks: new Map([['a', 0]]) }, filters: { search: { text: 'deploy' } },
    }))
    expect(keys(ranked.filteredSlots)).toEqual(['a', 't'])
    expect(keys(buildSidebarRows(inputs([titled, linked], { filters: { search: { text: '703' } } })).filteredSlots)).toEqual(['l'])
    expect(keys(buildSidebarRows(inputs([titled, linked], { filters: { search: { text: '#70' } } })).filteredSlots)).toEqual(['l'])
  })

  it('keeps every row filed in a folder whose NAME the query matched', () => {
    const rows = buildSidebarRows(inputs([local('in', { title: 'q' }), local('out', { title: 'q' })], {
      local: { folderOf: { in: 'F1' } }, filters: { search: { text: 'alpha', folderMatches: new Set(['F1']) } },
    }))
    expect(keys(rows.filteredSlots)).toEqual(['in'])
  })

  it('labels a chip by its serialized label, falling back to #N', () => {
    expect(chipLabel({ number: 5, label: '!5' } as never)).toBe('!5')
    expect(chipLabel({ number: 5 } as never)).toBe('#5')
  })
})

/** A population and a filter grid wide enough that every dimension, alone and
 *  combined, drops some row and keeps another. */
describe('the reveal registry and the narrowing flag agree with the filter pass', () => {
  const POPULATION: Slot[] = [
    local('a', { tags: ['t1'], title: 'alpha deploy' }),
    local('b', { tags: ['t2'], title: 'beta', pinned: true }),
    local('c', { title: 'gamma deploy', last_turn_ts: iso(3 * DAY) }),
    local('d', { tags: ['t1', 't2'], title: 'delta' }),
    peer('a', { tags: ['t1'], title: 'peer deploy', running: true }),
    peer('e', { title: 'epsilon', last_turn_ts: iso(5 * DAY) }),
  ]
  const LOCAL: InputOverrides['local'] = {
    folderOf: { a: 'F1', b: 'F2', d: 'F1' }, pinned: new Set(['b']), unread: new Set(['c']),
    running: new Set(['d']), recent: new Set(['a', 'b', 'd']),
  }
  const TAGS: Array<InputOverrides['filters']> = [{}, { tags: { resolved: new Set(['t1']), raw: new Set(['t1']) } }]
  const SEARCH: Array<InputOverrides['filters']> = [{}, { search: { text: 'deploy' } }]
  const STATUS: Array<InputOverrides['filters']> = [
    {}, { status: { active: new Set<SessionFilterKey>(['running', 'unread']) } },
    { status: { active: new Set<SessionFilterKey>(['recent']), paused: true } },
  ]
  const FOLDER: Array<InputOverrides['filters']> = [{}, { folders: { hiddenSubtree: new Set(['F1']), active: true } }]
  const grid: Array<[string, InputOverrides['filters']]> = []
  TAGS.forEach((t, i) => SEARCH.forEach((s, j) => STATUS.forEach((st, k) => FOLDER.forEach((f, l) => {
    grid.push([`tags${i} search${j} status${k} folder${l}`, { ...t, ...s, ...st, ...f }])
  }))))

  it.each(grid)('%s', (_name, filters) => {
    const rows = buildSidebarRows(inputs(POPULATION, { local: LOCAL, filters }))
    const shown = new Set(keys(rows.filteredSlots))
    const droppedRows = POPULATION.filter(s => !shown.has(keys([s])[0]))
    // Every row the filter pass dropped is hidden by at least one dimension, so a reveal
    // walking the registry can always bring it back.
    for (const slot of droppedRows) expect(rows.revealBlockingFilters.some(d => d.hides(slot)), keys([slot])[0]).toBe(true)
    // A list nothing narrows drops nothing.
    if (!rows.listNarrowed) expect(droppedRows).toEqual([])
    // And a narrowed list really does drop something on this population.
    if (rows.listNarrowed) expect(droppedRows.length).toBeGreaterThan(0)
  })

  it('an empty list is not narrowed by nothing', () => {
    const rows = buildSidebarRows(inputs([]))
    expect(rows.filteredSlots).toEqual([])
    expect(rows.listNarrowed).toBe(false)
  })
})

// ── order ────────────────────────────────────────────────────────────────────

describe('the lane order', () => {
  const a = local('a', { last_turn_ts: iso(1 * MINUTE) })
  const b = local('b', { last_turn_ts: iso(2 * MINUTE) })
  const c = local('c', { last_turn_ts: iso(3 * MINUTE) })
  const p = peer('c', { last_turn_ts: iso(30 * MINUTE) })

  it('puts the local pinned section first in its manual order, then the sort', () => {
    const rows = buildSidebarRows(inputs([a, b, c, p], {
      local: { pinned: new Set(['b', 'c']), pinnedRank: new Map([['c', 0], ['b', 1]]) },
    }))
    // The peer row's key collides with pinned `c`, but it sorts with the unpinned rows.
    expect(keys(rows.filteredSlots)).toEqual(['c', 'b', 'a', 'inst-a:c'])
  })

  it('orders by search relevance while ranked, a peer row and an unranked one last', () => {
    const rows = buildSidebarRows(inputs([a, b, c, p], {
      local: { pinned: new Set(['a']), searchRanks: new Map([['c', 0], ['b', 1]]) },
      filters: { search: { text: 'Title' } },
    }))
    expect(keys(rows.filteredSlots).slice(0, 2)).toEqual(['c', 'b'])
    expect(rows.laneOrder(p, c)).toBeGreaterThan(0)
  })

  it('follows the sort key outside the pinned section', () => {
    const rows = buildSidebarRows(inputs([local('m', { title: 'beta' }), local('n', { title: 'alpha' })], { sortKey: 'name-asc' }))
    expect(keys(rows.filteredSlots)).toEqual(['n', 'm'])
  })
})

// ── lanes and folders ───────────────────────────────────────────────────────

describe('a pinned session is exempt from the property filters', () => {
  // `pin` is locally pinned and carries the Pinned chip's own field; `peerPin` says
  // `pinned` too, but a peer row is never pinned here, so it is never exempt.
  const pin = local('pin', { pinned: true, tags: ['t1'], title: 'alpha' })
  const plain = local('plain', { tags: ['t2'], title: 'beta' })
  const peerPin = peer('pp', { pinned: true, tags: ['t1'], title: 'gamma' })
  const build = (filters: InputOverrides['filters']) =>
    buildSidebarRows(inputs([pin, plain, peerPin], { local: { pinned: new Set(['pin']) }, filters }))

  it('passes a tag filter it does not carry, and the filter still hides the rest', () => {
    const rows = build({ tags: { resolved: new Set(['t2']), raw: new Set(['t2']) } })
    expect(keys(rows.filteredSlots)).toEqual(['pin', 'plain'])
    expect(rows.listNarrowed).toBe(true)
    // A reveal of the pinned row must not clear a tag filter that never hid it.
    expect(rows.revealBlockingFilters[0].hides(pin)).toBe(false)
    expect(rows.revealBlockingFilters[0].hides(peerPin)).toBe(true)
  })

  it('passes a status chip that excludes it, and the chip still hides the rest', () => {
    const rows = build({ status: { active: new Set<SessionFilterKey>(['running']) } })
    expect(keys(rows.filteredSlots)).toEqual(['pin'])
    expect(rows.listNarrowed).toBe(true)
  })

  it('is dropped by the search, and a reveal then clears only the search', () => {
    const rows = build({ search: { text: 'beta' }, status: { active: new Set<SessionFilterKey>(['running']) } })
    expect(keys(rows.filteredSlots)).toEqual([])
    const [, search, status] = rows.revealBlockingFilters
    expect(search.hides(pin)).toBe(true)
    expect(status.hides(pin)).toBe(false)
  })

  it('is concealed by a folder hide like any row', () => {
    const rows = buildSidebarRows(inputs([pin, plain], {
      local: { pinned: new Set(['pin']), folderOf: { pin: 'F1' } }, filters: { folders: { hiddenSubtree: new Set(['F1']), active: true } },
    }))
    expect(keys(rows.lanes.flat)).toEqual(['plain'])
  })

  it('still counts on the chip badges only where it matches', () => {
    const rows = build({ status: { active: new Set<SessionFilterKey>(['running']) } })
    expect(rows.statusCounts.running).toBe(0)
    expect(rows.statusCounts.pinned).toBe(1)
  })
})

describe('the status hides', () => {
  const running = local('run', { title: 'alpha' })
  const pinnedRunning = local('pinrun', { pinned: true, title: 'beta' })
  const stopped = local('stop', { title: 'gamma' })
  const hideRunning = { hidden: new Set<SessionFilterKey>(['running']) }
  const build = (filters: InputOverrides['filters'], c = clears()) =>
    buildSidebarRows(inputs([running, pinnedRunning, stopped], {
      clears: c, local: { pinned: new Set(['pinrun']), running: new Set(['run', 'pinrun']) }, filters,
    }))
  const reveal = (rows: SidebarRows, slot: Slot) => {
    for (const d of rows.revealBlockingFilters) if (d.hides(slot)) d.clear(slot)
  }

  it('drops every row the hidden chip matches, a pinned one included', () => {
    const rows = build({ status: hideRunning })
    expect(keys(rows.filteredSlots)).toEqual(['stop'])
    expect(rows.listNarrowed).toBe(true)
    expect([running, pinnedRunning, stopped].map(rows.isRowStatusHidden)).toEqual([true, true, false])
  })

  it('subtracts from what the include chips keep', () => {
    const rows = buildSidebarRows(inputs([running, stopped], {
      local: { running: new Set(['run']), unread: new Set(['run', 'stop']) },
      filters: { status: { active: new Set<SessionFilterKey>(['unread']), ...hideRunning } },
    }))
    expect(keys(rows.filteredSlots)).toEqual(['stop'])
  })

  it('keeps hiding while the include chips are paused', () => {
    const rows = build({ status: { active: new Set<SessionFilterKey>(['pinned']), paused: true, ...hideRunning } })
    expect(keys(rows.filteredSlots)).toEqual(['stop'])
  })

  it('a reveal of a row the hide matches clears the hide and nothing else', () => {
    const c = clears()
    reveal(build({ status: hideRunning }, c), running)
    expect(c.hides).toHaveBeenCalledTimes(1)
    for (const other of [c.tags, c.search, c.status, c.folder]) expect(other).not.toHaveBeenCalled()
  })

  it('a reveal of a row the search dropped leaves the hides alone', () => {
    const c = clears()
    reveal(build({ search: { text: 'alpha' }, status: hideRunning }, c), stopped)
    expect(c.search).toHaveBeenCalledTimes(1)
    expect(c.hides).not.toHaveBeenCalled()
  })

  it('never hides a peer row by the local state its key collides with', () => {
    const rows = buildSidebarRows(inputs([local('k'), peer('k')], {
      local: { unread: new Set(['k']) }, filters: { status: { hidden: new Set<SessionFilterKey>(['unread']) } },
    }))
    expect(keys(rows.filteredSlots)).toEqual(['inst-a:k'])
  })
})

describe('the per-machine crew groups', () => {
  const a = local('a', { last_turn_ts: iso(MINUTE) })
  const runsThere = local('b', { executor: 'remote', instance_id: 'inst-a', last_turn_ts: iso(2 * MINUTE) } as Partial<Slot>)
  const peerA = peer('k', { last_turn_ts: iso(3 * MINUTE) })
  const peerB = peer('m', { peer_id: 'inst-b', peer_name: 'beta', row_identity: 'inst-b:m', last_turn_ts: iso(4 * MINUTE) })
  const all = [a, runsThere, peerA, peerB]

  it('moves a crew\'s peer rows and the local slots running there under its group, in lane order', () => {
    const rows = buildSidebarRows(inputs(all, { crewGroups: [{ id: 'inst-a' }] }))
    expect(keys(rows.lanes.crew.get('inst-a') ?? [])).toEqual(['b', 'inst-a:k'])
    expect(rows.lanes.crewRowCount).toBe(2)
    // A crew with no group stays in the Local lanes.
    expect(keys(rows.lanes.main)).toEqual(['a', 'inst-b:m'])
    expect(keys(rows.lanes.flat)).toEqual(['a', 'inst-b:m'])
    expect(keys(rows.lanes.ungrouped)).toEqual(['a', 'inst-b:m'])
    expect(keys(rows.lanes.board)).toEqual(['a'])
    expect(rows.lanes.boardPeerCount).toBe(1)
    // The reveal registry still reads the whole filtered list: a grouped row is shown.
    expect(rows.revealBlockingFilters[1].hides(peerA)).toBe(false)
  })

  it('keys every shown group, an empty one included', () => {
    const rows = buildSidebarRows(inputs([a], { crewGroups: [{ id: 'inst-a' }, { id: 'inst-c' }] }))
    expect([...rows.lanes.crew.keys()]).toEqual(['inst-a', 'inst-c'])
    expect(rows.lanes.crew.get('inst-c')).toEqual([])
    expect(rows.lanes.crewRowCount).toBe(0)
  })

  it('applies the folder hide to a group\'s rows, and keeps them out of the folder index', () => {
    const rows = buildSidebarRows(inputs(all, {
      crewGroups: [{ id: 'inst-a' }], local: { folderOf: { b: 'F1', a: 'F1' } },
      filters: { folders: { hiddenSubtree: new Set(['F1']), active: true } },
    }))
    expect(keys(rows.lanes.crew.get('inst-a') ?? [])).toEqual(['inst-a:k'])
    expect(keys(rows.folderTree.rowsIn('F1'))).toEqual(['a'])
  })

  it('draws every row in the Local lanes while no group shows', () => {
    const rows = buildSidebarRows(inputs(all))
    expect(rows.lanes.main).toBe(rows.filteredSlots)
    expect(rows.lanes.crew.size).toBe(0)
  })
})

describe('the lanes and the folder tree', () => {
  const filed = local('filed', { last_turn_ts: iso(MINUTE) })
  const nested = local('nested', { last_turn_ts: iso(2 * MINUTE) })
  const loose = local('loose', { last_turn_ts: iso(3 * MINUTE) })
  const folderOf = { filed: 'F1', nested: 'F2' }

  it('drops a concealed folder\'s rows from the flat lane and the board, unless a search suspends the hide', () => {
    const hidden = buildSidebarRows(inputs([filed, nested, loose], {
      local: { folderOf }, filters: { folders: { hiddenSubtree: new Set(['F2']), active: true } },
    }))
    expect(keys(hidden.lanes.flat)).toEqual(['filed', 'loose'])
    expect(keys(hidden.lanes.board)).toEqual(['filed', 'loose'])
    expect(hidden.isRowFolderHidden(nested)).toBe(true)
    // The tree lane decides per folder block, so its population keeps the row.
    expect(keys(hidden.folderTree.rowsIn('F2'))).toEqual(['nested'])
    const searching = buildSidebarRows(inputs([filed, nested, loose], {
      local: { folderOf }, filters: { folders: { hiddenSubtree: new Set(['F2']), active: false } },
    }))
    expect(keys(searching.lanes.flat)).toEqual(['filed', 'nested', 'loose'])
    expect(keys(searching.lanes.board)).toEqual(['filed', 'nested', 'loose'])
    expect(searching.isRowFolderHidden(nested)).toBe(false)
  })

  it('files each row directly under its own folder, in lane order', () => {
    const rows = buildSidebarRows(inputs([filed, nested, loose, local('also', { last_turn_ts: iso(30 * MINUTE) })], {
      local: { folderOf: { ...folderOf, also: 'F1' } },
    }))
    expect(keys(rows.folderTree.rowsIn('F1'))).toEqual(['filed', 'also'])
    expect(keys(rows.folderTree.rowsIn('F2'))).toEqual(['nested'])
    expect(rows.folderTree.rowsIn('nope')).toEqual([])
    expect(keys(rows.lanes.ungrouped)).toEqual(['loose'])
  })

  it('counts each status chip over every row, peers included where the chip reads their own fields', () => {
    const rows = buildSidebarRows(inputs([
      local('u', { pinned: true }), local('r'), peer('u', { running: true, pinned: true }), peer('old', { last_turn_ts: iso(2 * DAY) }),
    ], { local: { unread: new Set(['u']), running: new Set(['r']), recent: new Set(['u', 'r']) } }))
    expect(rows.statusCounts).toEqual({ unread: 1, running: 2, pinned: 1, recent: 3 })
  })
})

describe('a drag freezes the filtered list', () => {
  const a = local('a', { last_turn_ts: iso(MINUTE) })
  const b = local('b', { last_turn_ts: iso(2 * MINUTE) })

  it('keeps the list it had when the drag started, while the filters stay live', () => {
    const before = buildSidebarRows(inputs([a, b]))
    const during = buildSidebarRows(inputs([b], { frozen: true, filters: { search: { text: 'x' } } }), before)
    expect(during.filteredSlots).toBe(before.filteredSlots)
    expect(during.listNarrowed).toBe(true)
    const later = buildSidebarRows(inputs([b], { frozen: true, local: { pinned: new Set(['b']) } }), during)
    expect(later.filteredSlots).toBe(before.filteredSlots)
  })

  it('thaws onto the live inputs when the drag ends', () => {
    const before = buildSidebarRows(inputs([a, b]))
    const during = buildSidebarRows(inputs([b], { frozen: true }), before)
    const after = buildSidebarRows(inputs([b]), during)
    expect(keys(after.filteredSlots)).toEqual(['b'])
  })

  it('holds nothing when no list came before it', () => {
    expect(buildSidebarRows(inputs([a, b], { frozen: true })).filteredSlots).toEqual([])
  })
})

describe('the dormant split', () => {
  const fresh = local('fresh', { last_turn_ts: iso(MINUTE) })
  const dormant = local('dormant', { last_turn_ts: iso(10 * DAY) })
  const exemptDormant = local('exempt', { last_turn_ts: iso(10 * DAY) })
  const stale = { collapseMs: 7 * DAY, isExempt: (s: Slot) => s.key === 'exempt' }

  it('folds rows idle past the window, except exempt ones, measured on the clock at call time', () => {
    let now = NOW
    const rows = buildSidebarRows(inputs([fresh, dormant, exemptDormant], { clock: () => now }))
    const split = rows.staleSplit(rows.filteredSlots, stale)
    expect(keys(split.fresh)).toEqual(['fresh', 'exempt'])
    expect(keys(split.stale)).toEqual(['dormant'])
    now = NOW - 5 * DAY
    expect(rows.staleSplit(rows.filteredSlots, stale).stale).toEqual([])
  })

  it('is inert while the list is narrowed and under any sort but newest first', () => {
    const narrowed = buildSidebarRows(inputs([fresh, dormant], { filters: { search: { text: 'Title' } } }))
    expect(narrowed.staleSplit([fresh, dormant], stale).stale).toEqual([])
    const byName = buildSidebarRows(inputs([fresh, dormant], { sortKey: 'name-asc' }))
    expect(byName.staleSplit([fresh, dormant], stale).stale).toEqual([])
  })
})

// ── identity ─────────────────────────────────────────────────────────────────

/** Everything a consumer can observe of a build, as plain data. */
function observe(rows: SidebarRows, population: Slot[]) {
  return {
    filtered: keys(rows.filteredSlots),
    narrowed: rows.listNarrowed,
    counts: rows.statusCounts,
    main: keys(rows.lanes.main),
    crew: [...rows.lanes.crew].map(([id, list]) => [id, keys(list)]),
    crewRows: rows.lanes.crewRowCount,
    flat: keys(rows.lanes.flat),
    ungrouped: keys(rows.lanes.ungrouped),
    board: keys(rows.lanes.board),
    boardPeers: rows.lanes.boardPeerCount,
    tree: ['F1', 'F2'].map(id => keys(rows.folderTree.rowsIn(id))),
    hides: population.map(s => rows.revealBlockingFilters.map(d => d.hides(s))),
    folderHidden: population.map(rows.isRowFolderHidden),
    statusHidden: population.map(rows.isRowStatusHidden),
    order: keys([...population].sort(rows.laneOrder)),
    folderOf: population.map(rows.folderOf),
    pinned: population.map(rows.isPinned),
    running: population.map(rows.isRunning),
  }
}

describe('each output keeps its identity while its own inputs hold still', () => {
  // Every dimension active at once, so a change to any single leaf input reaches
  // something a consumer can see.
  const POPULATION: Slot[] = [
    local('a', { tags: ['t1'], title: 'alpha', pinned: true, last_turn_ts: iso(MINUTE) }),
    local('b', { tags: ['t1', 't2'], title: 'beta alpha', last_turn_ts: iso(2 * MINUTE) }),
    local('c', { tags: ['t2'], title: 'gamma', last_turn_ts: iso(3 * DAY) }),
    local('d', { tags: ['t1'], title: 'delta alpha', last_turn_ts: iso(4 * MINUTE) }),
    local('f', { tags: ['t1'], title: 'foxtrot', last_turn_ts: iso(5 * MINUTE) }),
    peer('a', { tags: ['t1'], title: 'alpha peer', running: true, last_turn_ts: iso(MINUTE) }),
    peer('e', { tags: ['t1'], title: 'epsilon alpha', last_turn_ts: iso(5 * DAY) }),
    // A local slot running on a crew whose group shows, so the crew lanes are live too.
    // Its crew is not the peers' crew, which keeps the peer rows in the Local lanes.
    local('r', { tags: ['t1'], title: 'romeo alpha', executor: 'remote', instance_id: 'inst-r', last_turn_ts: iso(6 * MINUTE) } as Partial<Slot>),
  ]
  const CLEARS = clears()
  const ACTIVE = inputs(POPULATION, {
    clears: CLEARS,
    crewGroups: [{ id: 'inst-r' }],
    local: {
      folderOf: { a: 'F2', b: 'F1', d: 'F1', f: 'F1', r: 'F1' }, pinned: new Set(['a', 'd']), pinnedRank: new Map([['a', 0], ['d', 1]]),
      unread: new Set(['d', 'f']), running: new Set(['b', 'r']), recent: new Set(['a', 'b']),
    },
    filters: {
      tags: { resolved: new Set(['t1']), raw: new Set(['t1']) },
      search: { text: 'alpha' },
      status: { active: new Set<SessionFilterKey>(['running', 'unread']) },
      folders: { hiddenSubtree: new Set(['F2']), active: true },
    },
  })
  /** The same leaf values in fresh outer objects, the way the facade builds them per render. */
  const rebuilt = (): SidebarRowInputs => ({
    ...ACTIVE,
    local: { ...ACTIVE.local },
    filters: {
      tags: { ...ACTIVE.filters.tags }, search: { ...ACTIVE.filters.search },
      status: { ...ACTIVE.filters.status }, folders: { ...ACTIVE.filters.folders },
    },
    clears: { ...ACTIVE.clears },
  })
  type Filters = SidebarRowInputs['filters']
  const withLocal = <K extends keyof SidebarRowInputs['local']>(key: K, value: SidebarRowInputs['local'][K]) =>
    (i: SidebarRowInputs): SidebarRowInputs => ({ ...i, local: { ...i.local, [key]: value } })
  const withFilter = <D extends keyof Filters, K extends keyof Filters[D]>(dim: D, key: K, value: Filters[D][K]) =>
    (i: SidebarRowInputs): SidebarRowInputs => ({ ...i, filters: { ...i.filters, [dim]: { ...i.filters[dim], [key]: value } } })

  it('reuses every memoized output when nothing changed', () => {
    const first = buildSidebarRows(ACTIVE)
    const second = buildSidebarRows(rebuilt(), first)
    const same: Array<keyof SidebarRows> = ['filteredSlots', 'revealBlockingFilters', 'statusCounts', 'laneOrder', 'isRowFolderHidden', 'isRowStatusHidden', 'folderTree', 'folderOf', 'isPinned', 'isRunning']
    for (const key of same) expect(second[key], key).toBe(first[key])
    expect(second.lanes.main).toBe(first.lanes.main)
    expect(second.lanes.crew).toBe(first.lanes.crew)
    expect(second.lanes.flat).toBe(first.lanes.flat)
    expect(second.lanes.ungrouped).toBe(first.lanes.ungrouped)
    expect(second.lanes.board).toBe(first.lanes.board)
  })

  it('drops a crew row while the folder hide covers it and restores it when the hide switches off, built on the previous result', () => {
    const first = buildSidebarRows(ACTIVE)
    expect(keys(first.lanes.crew.get('inst-r') ?? [])).toEqual(['r'])
    expect(first.lanes.crewRowCount).toBe(1)
    const hideF1 = withFilter('folders', 'hiddenSubtree', new Set(['F2', 'F1']))(rebuilt())
    const second = buildSidebarRows(hideF1, first)
    expect(second.lanes.crew.get('inst-r')).toEqual([])
    expect(second.lanes.crewRowCount).toBe(0)
    // Switching the folder filter off moves no filter dimension and so leaves the
    // filtered list as it was: only the folder hide itself brings the row back.
    const third = buildSidebarRows(withFilter('folders', 'active', false)(hideF1), second)
    expect(third.filteredSlots).toBe(second.filteredSlots)
    expect(keys(third.lanes.crew.get('inst-r') ?? [])).toEqual(['r'])
    expect(third.lanes.crewRowCount).toBe(1)
  })

  it('keeps the lane order through a change it does not read', () => {
    const first = buildSidebarRows(ACTIVE)
    const second = buildSidebarRows(withLocal('unread', new Set(['c']))(rebuilt()), first)
    expect(second.laneOrder).toBe(first.laneOrder)
    expect(second.isRowFolderHidden).toBe(first.isRowFolderHidden)
  })

  // Exactly ONE leaf input changes per row. Built incrementally from the previous
  // result, it must observe exactly what a build from scratch observes: a dependency an
  // output forgot would leave it stale here. Each row first proves its change is
  // visible, so no row passes by changing nothing. (`frozen` is the one input whose
  // incremental answer differs on purpose: see the drag-freeze cases.)
  const leaves: Array<[string, (i: SidebarRowInputs) => SidebarRowInputs]> = [
    ['rows', i => ({ ...i, rows: [...i.rows, peer('z', { tags: ['t1'], title: 'zeta alpha', running: true })] })],
    ['local.folderOf', withLocal('folderOf', { a: 'F2', b: 'F2', d: 'F1', f: 'F1' })],
    ['local.pinned', withLocal('pinned', new Set(['a', 'b']))],
    ['local.pinnedRank', withLocal('pinnedRank', new Map([['d', 0], ['a', 1]]))],
    ['local.unread', withLocal('unread', new Set(['d', 'f', 'c']))],
    ['local.running', withLocal('running', new Set(['b', 'c']))],
    ['local.recent', withLocal('recent', new Set(['a']))],
    ['local.searchRanks', withLocal('searchRanks', new Map([['b', 0], ['d', 1]]))],
    ['filters.tags.resolved', withFilter('tags', 'resolved', new Set(['t2']))],
    ['filters.tags.raw', withFilter('tags', 'raw', new Set(['t2']))],
    ['filters.search.text', withFilter('search', 'text', 'beta')],
    ['filters.search.folderMatches', withFilter('search', 'folderMatches', new Set(['F1']))],
    ['filters.status.active', withFilter('status', 'active', new Set<SessionFilterKey>(['running']))],
    ['filters.status.paused', withFilter('status', 'paused', true)],
    ['filters.status.hidden', withFilter('status', 'hidden', new Set<SessionFilterKey>(['unread']))],
    ['filters.status.recentWindowMs', withFilter('status', 'recentWindowMs', 10 * DAY)],
    ['filters.folders.hiddenSubtree', withFilter('folders', 'hiddenSubtree', new Set(['F1']))],
    ['filters.folders.active', withFilter('folders', 'active', false)],
    ['sortKey', i => ({ ...i, sortKey: 'name-asc' })],
    ['clock', i => ({ ...i, clock: () => NOW + 10 * DAY })],
    ['crewGroups', i => ({ ...i, crewGroups: [] })],
  ]
  it.each(leaves)('a change of %s alone is never served stale', (_name, change) => {
    const next = change(rebuilt())
    const population = next.rows
    expect(observe(buildSidebarRows(next), population)).not.toEqual(observe(buildSidebarRows(ACTIVE), population))
    const incremental = buildSidebarRows(next, buildSidebarRows(ACTIVE))
    expect(observe(incremental, population)).toEqual(observe(buildSidebarRows(next), population))
  })

  it.each(['tags', 'search', 'status', 'folder', 'hides'] as const)('a new %s clear is the one a reveal calls', name => {
    const fresh = vi.fn()
    const next = { ...rebuilt(), clears: { ...ACTIVE.clears, [name]: fresh } }
    const incremental = buildSidebarRows(next, buildSidebarRows(ACTIVE))
    const index = ['tags', 'search', 'status', 'folder', 'hides'].indexOf(name)
    incremental.revealBlockingFilters[index].clear(POPULATION[1])
    expect(fresh).toHaveBeenCalledTimes(1)
    expect(CLEARS[name]).not.toHaveBeenCalled()
  })
})

describe('a build holds no earlier build alive', () => {
  // The facade hands each build the previous one. An output reused across builds that
  // kept its creating build reachable would chain every earlier build behind the
  // current one, a leak that grows with every slots frame.
  it('lets an old build be collected while later builds reuse its memoized outputs', async () => {
    v8.setFlagsFromString('--expose-gc')
    const gc = vm.runInNewContext('gc') as () => void
    const unchanged = inputs([])
    let rows: SidebarRows | null = null
    let tenth: WeakRef<Slot[]> | null = null
    for (let build = 0; build < 20; build++) {
      const fresh = Array.from({ length: 50 }, (_, n) => local(`s${build}-${n}`))
      rows = buildSidebarRows({ ...unchanged, rows: fresh }, rows)
      // Each render also reads its views under a crew window, as the facade does; the
      // window must not ride into any memo either.
      rows.views(scene({ crewWindow: { instanceId: 'inst-a', key: `s${build}-0` } })).view(fresh[0], AT)
      if (build === 10) tenth = new WeakRef(rows.filteredSlots)
    }
    // A WeakRef target stays alive until the current job ends.
    await new Promise(resolve => setImmediate(resolve))
    gc()
    gc()
    expect(tenth?.deref()).toBeUndefined()
    expect(rows?.filteredSlots).toHaveLength(50)
  })
})

// ── views ────────────────────────────────────────────────────────────────────

describe('row views compare by value', () => {
  const a = local('a')
  const b = local('b')
  const rows = buildSidebarRows(inputs([a, b]))

  it('two views of the same row and placement are the same row', () => {
    expect(sameRowView(rows.views(scene()).view(a, AT), rows.views(scene()).view(a, { ...AT }))).toBe(true)
  })

  it('a sibling\'s local state leaves a row\'s view equal', () => {
    const before = rows.views(scene()).view(a, AT)
    const after = rows.views(scene({ local: { subagentCounts: { b: 3 }, poppedOut: new Set(['b']), digits: new Map([['b', '2']]) } })).view(a, AT)
    expect(sameRowView(before, after)).toBe(true)
  })

  it('a moved paint position is a different row, so the layout spring re-renders it', () => {
    expect(sameRowView(rows.views(scene()).view(a, AT), rows.views(scene()).view(a, { ...AT, orderStamp: 1 }))).toBe(false)
  })

  it('shares the one shell object, and a new shell is a different row for every row', () => {
    const view = rows.views(scene()).view(a, AT)
    expect(view.shell).toBe(SHELL)
    const moved = rows.views(scene({ shell: { ...SHELL, connected: false } }))
    expect(sameRowView(view, moved.view(a, AT))).toBe(false)
    expect(sameRowView(rows.views(scene()).view(b, AT), moved.view(b, AT))).toBe(false)
  })

  const EXTRAS: ConductorRowView = { depth: 1, childCount: 2, subtreeCount: 3, subtreeRunning: 0, expanded: false, aggregate: { needsYou: 1, running: 0 }, orphanOf: null, citesParent: null, anchorOnly: false }
  const withExtras = (c: ConductorRowView | undefined) => rows.views(scene()).view(a, { ...AT, conductor: c })

  it('compares the conductor extras by value', () => {
    expect(sameRowView(withExtras(EXTRAS), withExtras({ ...EXTRAS, aggregate: { ...EXTRAS.aggregate! } }))).toBe(true)
    expect(sameRowView(withExtras(EXTRAS), withExtras({ ...EXTRAS, aggregate: { needsYou: 2, running: 0 } }))).toBe(false)
    expect(sameRowView(withExtras(EXTRAS), withExtras({ ...EXTRAS, aggregate: { needsYou: 1, running: 1 } }))).toBe(false)
    expect(sameRowView(withExtras(EXTRAS), withExtras({ ...EXTRAS, aggregate: null }))).toBe(false)
    expect(sameRowView(withExtras(EXTRAS), withExtras(undefined))).toBe(false)
  })

  const fieldChanges: Array<[keyof ConductorRowView, ConductorRowView[keyof ConductorRowView]]> = [
    ['depth', 2], ['childCount', 3], ['expanded', true], ['orphanOf', 'gone'], ['citesParent', 'parent'], ['anchorOnly', true],
  ]
  it.each(fieldChanges)('a changed conductor %s is a different row', (field, value) => {
    expect(sameRowView(withExtras(EXTRAS), withExtras({ ...EXTRAS, [field]: value }))).toBe(false)
  })

  it('hands the rename draft to the one placement being renamed', () => {
    const views = rows.views(scene({ local: { renaming: { key: 'a', scope: 'col-1', value: 'new name' } } }))
    expect(views.view(a, { ...AT, scope: 'col-1' })).toMatchObject({ isRenaming: true, renamingHere: true, renameValue: 'new name' })
    expect(views.view(a, AT)).toMatchObject({ isRenaming: true, renamingHere: false, renameValue: '' })
    expect(views.view(b, AT)).toMatchObject({ isRenaming: false, renameValue: '' })
  })

  it('opens a local row of another page where it lives', () => {
    const member = local('m', { mode: 'member', agent: 'bob smith' })
    const views = buildSidebarRows(inputs([member])).views(scene())
    expect(views.view(member, AT).opensElsewhere).toBe('/members?member=bob%20smith')
    expect(views.view(local('x', { mode: 'member' }), AT).opensElsewhere).toBe('/members')
    expect(views.view(a, AT).opensElsewhere).toBeNull()
  })

  it('fades a reveal flash, and lights it only for a session reveal', () => {
    const fading = rows.views(scene({ local: { revealFlash: { kind: 'session', key: 'a', fading: true } } }))
    expect(fading.view(a, AT).revealFlash).toBe('fade')
    expect(fading.view(b, AT).revealFlash).toBeNull()
    const folderFlash = rows.views(scene({ local: { revealFlash: { kind: 'folder', key: 'a', fading: false } } }))
    expect(folderFlash.view(a, AT).revealFlash).toBeNull()
  })

  it('carries the placement\'s own animation gate and the local pin rank', () => {
    const pinnedRows = buildSidebarRows(inputs([local('p'), b], { local: { pinnedRank: new Map([['p', 3]]) } }))
    expect(pinnedRows.views(scene()).view(local('p'), { ...AT, rowAnimEnabled: false }))
      .toMatchObject({ pinnedOrderIndex: 3, rowAnimEnabled: false })
  })
})

describe('the window a row stubs into', () => {
  const a = local('a')
  const b = local('b')
  const rows = buildSidebarRows(inputs([a, b]))
  const mounted = (over: Parameters<typeof scene>[0], slot: Slot = a) => rows.views(scene(over)).windowOf(slot).keepMounted

  it('titles the stub by the session title, falling back to its key', () => {
    const views = rows.views(scene())
    expect(views.windowOf(a).title).toBe('Title a')
    expect(views.windowOf(local('k', { title: '' })).title).toBe('k')
    expect(views.windowOf(local('k', { title: 'k' })).title).toBe('k')
  })

  it('stubs a row only when nothing on it would be lost', () => {
    expect(mounted({})).toBe(false)
    expect(mounted({ activeSlot: 'a' })).toBe(true)
    expect(mounted({ local: { revealFlash: { kind: 'session', key: 'a', fading: true } } })).toBe(true)
    expect(mounted({ local: { revealFlash: { kind: 'folder', key: 'a', fading: false } } })).toBe(false)
    // Renaming in ANY placement keeps every copy of the row mounted.
    expect(mounted({ local: { renaming: { key: 'a', scope: 'elsewhere', value: '' } } })).toBe(true)
    expect(mounted({ drag: { type: 'session', id: 'a' } })).toBe(true)
    expect(mounted({ drag: { type: 'folder', id: 'a' } })).toBe(false)
    expect(mounted({ local: { nativeDragKey: 'a' } })).toBe(true)
    expect(mounted({ activeSlot: 'b', local: { nativeDragKey: 'b' } })).toBe(false)
  })
})
