/**
 * `reconcileWorkflowRuns` — the read that keeps a chat's workflow row honest.
 *
 * Live status reaches `chat.workflowRuns` ONLY as one-shot `workflow_run_event`
 * frames, which are never replayed. A tab that was closed, asleep, disconnected,
 * or on the far side of a gateway restart when a run ended therefore holds a row
 * frozen at `running` forever: the spinner is driven purely by stored status, the
 * phase and log lines render because of it, and the terminal-linger cleanup only
 * arms for entries that HAVE reached a terminal status, so nothing drops it.
 * These tests pin that this merge fixes that case without ever inventing state:
 * it may only advance a run running -> terminal, and it treats every ambiguous
 * reading (unknown status, absent row, unreadable authority) as no evidence.
 */
import { createTestStore } from './helpers'
import { reconcileWorkflowRuns, sseWorkflowEvent, isTerminalWorkflowStatus } from '../store/chatSlice'
import type { WorkflowRunProgress } from '../store/chatSlice'

type Store = ReturnType<typeof createTestStore>

const runs = (store: Store): Record<string, WorkflowRunProgress> => store.getState().chat.workflowRuns

/** A hostile wire value, typed as the field's declared type without `any`. The
 *  backend's types promise strings; a workflow script's own values do not. */
const wire = (value: unknown): string => value as string

/** Put a live, mid-flight run in the store the only way the app can: WS frames. */
function liveRun(store: Store, run_id = 'wf_000025', session_key = 'dashboard:chat-1') {
  store.dispatch(sseWorkflowEvent({ run_id, session_key, type: 'run_started', data: { name: 'Kiro Crew perf investigation' } }))
  store.dispatch(sseWorkflowEvent({ run_id, type: 'phase_started', data: { title: 'synthesize' } }))
  store.dispatch(sseWorkflowEvent({ run_id, type: 'log', data: { message: 'Starting Kiro Crew performance investigation' } }))
}

describe('reconcileWorkflowRuns', () => {
  let store: Store
  beforeEach(() => { store = createTestStore() })

  it('clears a spinner left behind by a missed terminal frame', () => {
    // The reported bug: the run finished, the frame never arrived, the row span.
    liveRun(store)
    expect(runs(store)['wf_000025'].status).toBe('running')

    store.dispatch(reconcileWorkflowRuns([
      { run_id: 'wf_000025', name: 'Kiro Crew perf investigation', status: 'finished', session_key: 'dashboard:chat-1' },
    ]))

    expect(runs(store)['wf_000025'].status).toBe('finished')
  })

  it('carries a failure reason so the row can say why', () => {
    liveRun(store)
    store.dispatch(reconcileWorkflowRuns([{ run_id: 'wf_000025', status: 'failed', error: 'authoring error' }]))
    expect(runs(store)['wf_000025']).toMatchObject({ status: 'failed', error: 'authoring error' })
  })

  it('applies a gateway restart\'s interrupted verdict', () => {
    // A run still running when the gateway died is restored as failed; a tab that
    // stayed open across the restart learns it only from this read.
    liveRun(store)
    store.dispatch(reconcileWorkflowRuns([
      { run_id: 'wf_000025', status: 'failed', error: 'interrupted by gateway restart' },
    ]))
    expect(runs(store)['wf_000025'].status).toBe('failed')
  })

  it('never rewinds a run the live stream already ended', () => {
    // The snapshot is a point-in-time read that RACES the stream: a terminal
    // frame can land while the request is in flight. Re-opening the row would
    // resurrect a spinner for a run this client knows is over.
    liveRun(store)
    store.dispatch(sseWorkflowEvent({ run_id: 'wf_000025', type: 'run_finished', data: {} }))
    expect(runs(store)['wf_000025'].status).toBe('finished')

    store.dispatch(reconcileWorkflowRuns([{ run_id: 'wf_000025', status: 'running', phase: 'synthesize' }]))

    expect(runs(store)['wf_000025'].status).toBe('finished')
  })

  it('does not touch an ended run at all, not even to restate its verdict', () => {
    // An older snapshot can disagree about WHICH terminal state a run reached (a
    // cancel racing the run's own failure). The frame this client already applied
    // is the newer reading, so nothing here — verdict, error, progress — may move.
    liveRun(store)
    store.dispatch(sseWorkflowEvent({ run_id: 'wf_000025', type: 'run_cancelled', data: { reason: 'user' } }))
    const before = { ...runs(store)['wf_000025'] }
    expect(before.status).toBe('cancelled')

    store.dispatch(reconcileWorkflowRuns([{
      run_id: 'wf_000025', status: 'failed', error: 'ceiling reached',
      phase: 'discover', last_log: 'an older line', name: 'renamed by an older read',
    }]))

    expect(runs(store)['wf_000025']).toEqual(before)
  })

  it('seeds a run that started before this tab could hear about it', () => {
    // Nothing else seeds this slice, so a reload mid-run leaves the bar empty
    // until the next phase event — and shows nothing at all if the run ends first.
    store.dispatch(reconcileWorkflowRuns([{
      run_id: 'wf_000031',
      name: 'Nightly audit',
      status: 'running',
      phase: 'investigate',
      last_log: 'reading handlers',
      session_key: 'dashboard:chat-2',
    }]))

    expect(runs(store)['wf_000031']).toMatchObject({
      run_id: 'wf_000031',
      name: 'Nightly audit',
      status: 'running',
      phase: 'investigate',
      lastLog: 'reading handlers',
      sessionKey: 'dashboard:chat-2',
    })
  })

  it('does not resurrect a run that is already over', () => {
    // Otherwise every reconnect would paste a wall of ✓ rows above the composer
    // for runs the user finished with hours ago.
    store.dispatch(reconcileWorkflowRuns([
      { run_id: 'wf_000001', status: 'finished' },
      { run_id: 'wf_000002', status: 'failed' },
      { run_id: 'wf_000003', status: 'cancelled' },
    ]))
    expect(Object.keys(runs(store))).toEqual([])
  })

  it('lets the live stream own progress, filling only what never arrived', () => {
    liveRun(store)
    store.dispatch(reconcileWorkflowRuns([{
      run_id: 'wf_000025', status: 'running', phase: 'discover', last_log: 'an older line',
    }]))
    // Stale snapshot values must not overwrite newer frames...
    expect(runs(store)['wf_000025']).toMatchObject({ phase: 'synthesize', lastLog: 'Starting Kiro Crew performance investigation' })

    // ...but a gap this client never received IS filled.
    const bare = createTestStore()
    bare.dispatch(sseWorkflowEvent({ run_id: 'wf_9', type: 'run_started', data: { name: 'Bare' } }))
    bare.dispatch(reconcileWorkflowRuns([{ run_id: 'wf_9', status: 'running', phase: 'discover', last_log: 'first line' }]))
    expect(bare.getState().chat.workflowRuns['wf_9']).toMatchObject({ phase: 'discover', lastLog: 'first line' })
  })

  it('fills a session key the frames never carried, and keeps the one they did', () => {
    // The bar shows a run only in the chat that launched it, so the key decides
    // visibility: a row seeded or corrected without it would render nowhere.
    store.dispatch(sseWorkflowEvent({ run_id: 'wf_7', type: 'run_started', data: { name: 'Keyless' } }))
    store.dispatch(reconcileWorkflowRuns([{ run_id: 'wf_7', status: 'running', session_key: 'dashboard:chat-3' }]))
    expect(runs(store)['wf_7'].sessionKey).toBe('dashboard:chat-3')

    store.dispatch(reconcileWorkflowRuns([{ run_id: 'wf_7', status: 'running', session_key: 'dashboard:chat-OTHER' }]))
    expect(runs(store)['wf_7'].sessionKey).toBe('dashboard:chat-3')
  })

  it('treats an unrecognised status as no evidence', () => {
    // A future backend state must not clear a spinner or seed a row by accident.
    liveRun(store)
    store.dispatch(reconcileWorkflowRuns([
      { run_id: 'wf_000025', status: 'paused' },
      { run_id: 'wf_unseen', status: 'paused' },
      { run_id: 'wf_nostatus' },
    ]))
    expect(runs(store)['wf_000025'].status).toBe('running')
    expect(runs(store)['wf_unseen']).toBeUndefined()
    expect(runs(store)['wf_nostatus']).toBeUndefined()
  })

  it('leaves a row the authority no longer lists alone', () => {
    // The registry evicts old runs, so absence from a successful response is not
    // evidence the run ended — guessing here would clear a genuinely live row.
    liveRun(store)
    store.dispatch(reconcileWorkflowRuns([{ run_id: 'wf_other', status: 'running' }]))
    expect(runs(store)['wf_000025'].status).toBe('running')
  })

  it('ignores a row with no usable id', () => {
    store.dispatch(reconcileWorkflowRuns([
      { run_id: '', status: 'running' },
      { run_id: '__proto__', status: 'running' },
      { run_id: 'constructor', status: 'finished' },
    ]))
    expect(Object.keys(runs(store))).toEqual([])
    expect(Object.getPrototypeOf(runs(store))).toBe(Object.prototype)
  })

  it('accepts an empty list without touching anything', () => {
    liveRun(store)
    store.dispatch(reconcileWorkflowRuns([]))
    expect(runs(store)['wf_000025'].status).toBe('running')
  })
})

describe('workflow run text fields are agent-authored', () => {
  // A workflow script calls `ctx.phase(123)` or logs a dict, and that value rides
  // the event stream and the runs API unchanged. The chat SLICES these fields, so
  // a number reaching the store throws inside render — the whole chat goes blank,
  // not just the row. Both writers into the slice must coerce.
  let store: Store
  beforeEach(() => { store = createTestStore() })

  /** Every field the rendering path slices must really be a string. */
  const assertRenderable = (run: WorkflowRunProgress | undefined) => {
    expect(run).toBeDefined()
    for (const field of ['run_id', 'name', 'phase', 'lastLog'] as const) {
      expect(typeof run![field]).toBe('string')
    }
    if (run!.error !== undefined) expect(typeof run!.error).toBe('string')
    if (run!.sessionKey !== undefined) expect(typeof run!.sessionKey).toBe('string')
    // The real crash: the component slices these.
    expect(() => `${run!.name}`.slice(0, 60) + run!.phase.slice(0, 40) + run!.lastLog.slice(0, 100)).not.toThrow()
  }

  it('seeds a run whose wire fields are not strings', () => {
    store.dispatch(reconcileWorkflowRuns([{
      run_id: 'wf_000041',
      name: wire(7), phase: wire(123), last_log: wire({ note: 'x' }),
      session_key: wire(5), status: 'running',
    }]))
    const run = runs(store)['wf_000041']
    assertRenderable(run)
    // A number is DROPPED, not stringified: "[object Object]" in the chat is
    // worse than the field being absent. The name falls back to the run id.
    expect(run).toMatchObject({ name: 'wf_000041', phase: '', lastLog: '', sessionKey: undefined })
  })

  it('does not fill a running row from non-string progress fields', () => {
    store.dispatch(sseWorkflowEvent({ run_id: 'wf_5', type: 'run_started', data: { name: 'Live' } }))
    store.dispatch(reconcileWorkflowRuns([{
      run_id: 'wf_5', status: 'running', phase: wire(42), last_log: wire([]),
    }]))
    assertRenderable(runs(store)['wf_5'])
    expect(runs(store)['wf_5']).toMatchObject({ phase: '', lastLog: '' })
  })

  it('does not record a non-string failure reason', () => {
    store.dispatch(sseWorkflowEvent({ run_id: 'wf_6', type: 'run_started', data: { name: 'Live' } }))
    store.dispatch(reconcileWorkflowRuns([{ run_id: 'wf_6', status: 'failed', error: wire({ code: 9 }) }]))
    expect(runs(store)['wf_6'].status).toBe('failed')
    assertRenderable(runs(store)['wf_6'])
  })

  it('ignores a row whose id is not a string', () => {
    store.dispatch(reconcileWorkflowRuns([{ run_id: wire(12), status: 'running' }]))
    expect(Object.keys(runs(store))).toEqual([])
  })

  it('coerces the same fields on the LIVE event path', () => {
    // The identical values arrive over the WS stream, so a guard on the read side
    // alone would leave the same crash reachable through the older writer.
    store.dispatch(sseWorkflowEvent({ run_id: 'wf_7', type: 'run_started', data: { name: wire(99) } }))
    store.dispatch(sseWorkflowEvent({ run_id: 'wf_7', type: 'phase_started', data: { title: wire({ t: 1 }) } }))
    store.dispatch(sseWorkflowEvent({ run_id: 'wf_7', type: 'log', data: { message: wire(3.5) } }))
    store.dispatch(sseWorkflowEvent({ run_id: 'wf_7', type: 'run_failed', data: { error: wire([1]) } }))

    const run = runs(store)['wf_7']
    assertRenderable(run)
    // Falls back to the run id for the name, and keeps nothing unrenderable.
    expect(run).toMatchObject({ name: 'wf_7', phase: '', lastLog: '', status: 'failed' })
  })
})

describe('isTerminalWorkflowStatus', () => {
  it('names exactly the ended states', () => {
    expect(['finished', 'failed', 'cancelled'].every(isTerminalWorkflowStatus)).toBe(true)
    expect(isTerminalWorkflowStatus('running')).toBe(false)
    expect(isTerminalWorkflowStatus('paused')).toBe(false)
    expect(isTerminalWorkflowStatus(undefined)).toBe(false)
    expect(isTerminalWorkflowStatus('')).toBe(false)
  })
})
