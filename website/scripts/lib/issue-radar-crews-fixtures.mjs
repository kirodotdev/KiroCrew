/**
 * Shared fixtures for the Issue Radar → Crews harnesses.
 *
 * Two harnesses consume these — `capture-crews.mjs` (screenshots) and
 * `record-crews.mjs` (the roster → crew page → create flow). They live here rather
 * than in either script because `.jscpd.json` sets `threshold: 0` with
 * `minTokens: 180`, and a fixture table this size copied into both scripts is a
 * clone finding that fails the copy/paste gate.
 *
 * Named `issue-radar-crews-fixtures` and not `crews-fixtures`: the latter
 * already exists in this directory for the /capabilities → Crews tab, which is
 * the AGENT-TEMPLATE roster and a different feature entirely.
 */

export const OWNER = 'kirodotdev'
export const REPO = 'KiroCrew' // brand-ok: the repository name
export const REPO_REF = { owner: OWNER, repo: REPO, provider: 'github', host: 'github.com' }

export const SETTINGS = {
  schema: 1,
  claim_ttl_hours: 48,
  needs_human_label: 'crew: needs human',
  commit_trailer: 'Crew: {name} (Kiro Crew Issue Radar)',
}

/** One crew record. `status` is DERIVED BY THE ROUTE (`_crew_status` in
 *  crew_routes.py) and added on the way out — it is not a stored field, so a
 *  fixture built from crew_store's record shape alone renders every status dot
 *  as idle. That mistake cost a full screenshot round here; hence the default. */
export function crew(id, name, over = {}) {
  return {
    schema: 1,
    id,
    name,
    avatar_seed: name,
    avatar_variant: null,
    agent: 'kirocrew',
    model: '',
    extra_prompt: '',
    labels: ['area: dashboard', 'area: gateway'],
    auto_resolve_conflicts: true,
    auto_merge: true,
    unattended: true,
    max_open: 3,
    worktree_root: '~/workplace/oss',
    slot_key: `crew-${id}`,
    enabled: true,
    paused_reason: '',
    created_at: '2026-08-06T09:12:00Z',
    retired_at: null,
    status: 'idle',
    ...over,
  }
}

export function item(crewId, number, phase, over = {}) {
  return {
    schema: 1,
    crew_id: crewId,
    owner: OWNER,
    repo: REPO,
    number,
    phase,
    outcome: null,
    decision: '',
    why: '',
    next: '',
    tried: [],
    worktree: '',
    branch: '',
    base_sha: '',
    pr_number: null,
    ci_state: {},
    claim_comment_id: null,
    labels_applied: [],
    claimed_at: '2026-08-08T18:02:00Z',
    last_progress_at: '2026-08-08T20:44:00Z',
    finished_at: null,
    ...over,
  }
}

export const CREWS = [
  crew('c_7f3a01', 'Andromeda', { status: 'working' }),
  crew('c_7f3a02', 'Whirlpool', { status: 'idle' }),
  crew('c_7f3a03', 'Pinwheel', { labels: ['area: core'], status: 'working' }),
  crew('c_7f3a04', 'Sombrero', { labels: ['area: ci'], status: 'working' }),
  crew('c_7f3a05', 'Cocoon', { labels: ['area: apps'], status: 'idle' }),
  crew('c_7f3a06', 'Triangulum', {
    enabled: false,
    paused_reason: 'Paused by you at 18:40',
    labels: ['area: skills'],
    status: 'paused',
  }),
]

/** Andromeda's detail. `finished` exists because the crew page's "Resolved · 24h"
 *  tile counts work ITEMS with `finished_at` inside 24h — open items alone leave
 *  it reading zero. */
const OPEN_ITEMS = [
  item('c_7f3a01', 2251, 'implementing', {
    next: 'Add the Windows branch to _safe_chmod — the regression test already fails',
    branch: 'crew/andromeda/issue-2251',
    pr_number: 2271,
    ci_state: { state: 'running', passed: 41, total: 47, round: 3, inherited_reds: 6 },
    last_progress_at: '2026-08-08T20:44:00Z',
  }),
  item('c_7f3a01', 2264, 'awaiting-reply', {
    next: 'Asked for the failing command and the OS build',
    last_progress_at: '2026-08-08T15:50:00Z',
  }),
  item('c_7f3a01', 2247, 'awaiting-merge', {
    next: 'PR #2258 green, auto-merge armed, waiting on approval',
    pr_number: 2258,
    last_progress_at: '2026-08-08T18:40:00Z',
  }),
]

const FINISHED_ITEMS = [
  item('c_7f3a01', 2247, 'resolved', {
    outcome: 'merged', pr_number: 2258,
    finished_at: '2026-08-08T18:40:00Z', last_progress_at: '2026-08-08T18:40:00Z',
  }),
  item('c_7f3a01', 2210, 'resolved', {
    outcome: 'merged', pr_number: 2219,
    finished_at: '2026-08-08T10:02:00Z', last_progress_at: '2026-08-08T10:02:00Z',
  }),
  item('c_7f3a01', 2268, 'skipped', {
    outcome: 'duplicate', next: 'duplicate of #2240, already fixed on main',
    finished_at: '2026-08-08T16:30:00Z', last_progress_at: '2026-08-08T16:30:00Z',
  }),
]

export const DETAIL = {
  crew: CREWS[0],
  counts: { open: 3 },
  items: [...OPEN_ITEMS, ...FINISHED_ITEMS],
  events: [
    { id: 'e1', ts: '2026-08-08T20:44:00Z', crew_id: 'c_7f3a01', number: 2251, kind: 'ci', text: 'CI round 3 — 41/47 green, 6 reds inherited from main' },
    { id: 'e2', ts: '2026-08-08T18:40:00Z', crew_id: 'c_7f3a01', number: 2247, kind: 'merge', text: 'All green, armed auto-merge' },
    { id: 'e3', ts: '2026-08-08T16:30:00Z', crew_id: 'c_7f3a01', number: 2268, kind: 'skip', text: 'Duplicate of #2240, already fixed on main — did not claim' },
    { id: 'e4', ts: '2026-08-08T15:50:00Z', crew_id: 'c_7f3a01', number: 2264, kind: 'reply', text: 'Asked the requester for the failing command and OS build' },
    { id: 'e5', ts: '2026-08-08T11:20:00Z', crew_id: 'c_7f3a01', number: 2259, kind: 'skip', text: 'Needs a product decision — said so on the issue, labelled it and moved on' },
    { id: 'e6', ts: '2026-08-08T04:15:00Z', crew_id: 'c_7f3a01', number: 2233, kind: 'conflict', text: 'Resolved catalog conflict — key set is the union, no value changed' },
    { id: 'e7', ts: '2026-08-07T13:05:00Z', crew_id: 'c_7f3a01', number: 2229, kind: 'yield', text: "Yielded — Pinwheel's claim comment had a lower id" },
    { id: 'e8', ts: '2026-08-06T10:02:00Z', crew_id: 'c_7f3a01', number: 2210, kind: 'merge', text: 'Implemented fix, PR #2219 merged' },
  ],
}

export const COUNTS = { on_duty: 6, working: 3, paused: 1 }

/**
 * Routes the shared dashboard stub does not know.
 *
 * Each branch AWAITS json() then returns true: a falsy return means "not
 * handled" and the stub fulfils it itself, so a bare `return json(...)` both
 * double-fulfils ("Route is already handled!") and leaves the fulfilment
 * unawaited.
 */
export function makeExtra(json) {
  return async (path, route) => {
    if (path.endsWith('/issue-radar/repos')) {
      await json(route, {
        repos: [{ ...REPO_REF, enabled: true, permissions: { push: true, triage: true } }],
      })
      return true
    }
    if (path.includes('/issue-radar/crews/settings')) {
      await json(route, { settings: SETTINGS })
      return true
    }
    if (path.includes('/issue-radar/crews/names')) {
      await json(route, {
        suggestions: ['Sombrero', 'Bode', 'Butterfly', 'Carina', 'Draco', 'Fireworks'],
      })
      return true
    }
    if (path.includes('/issue-radar/crews')) {
      await json(route, {
        owner: OWNER, repo: REPO, crews: CREWS, settings: SETTINGS, counts: COUNTS,
      })
      return true
    }
    if (path.includes('/issue-radar/crew')) {
      // The work items and ledger are Andromeda's either way — one hand-built log
      // is enough — but the CREW is swapped to whichever id was asked for, so
      // selecting another row visibly repaints column 3 instead of re-rendering
      // the same header under a different selection. Read off the REQUEST url:
      // `path` is the pathname only, so the query is not in it.
      const id = new URL(route.request().url()).searchParams.get('id')
      const asked = CREWS.find((c) => c.id === id)
      await json(route, asked ? { ...DETAIL, crew: asked } : DETAIL)
      return true
    }
    if (path.includes('/issue-radar/labels')) {
      await json(route, {
        owner: OWNER,
        repo: REPO,
        labels: [
          { name: 'area: dashboard', color: 'C5DEF5' },
          { name: 'area: gateway', color: 'C5DEF5' },
          { name: 'area: core', color: 'C5DEF5' },
        ],
        from_cache: true,
      })
      return true
    }
    return false
  }
}

/** localStorage the app reads to land on the Crews view. `crew-ui` is its own
 *  key because a persisted crew SELECTION cannot be validated on read alone —
 *  see the comment on CREW_UI_KEY in context.tsx. */
export function seedState(crewUi, ui) {
  return {
    'kc:issue-radar:active-repo': JSON.stringify({ owner: OWNER, repo: REPO }),
    // `ui` lets a caller land on another main view — the crew PROTOCOL settings
    // live on the repo settings page, so capturing them needs a different view
    // than the crews surface itself.
    'kc:issue-radar:ui-state': JSON.stringify({ mainView: 'crews', ...(ui ?? {}) }),
    'kc:issue-radar:crew-ui': JSON.stringify(crewUi),
  }
}
