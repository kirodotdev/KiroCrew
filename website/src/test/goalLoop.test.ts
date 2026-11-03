/**
 * Section goals as one loop on the conductor: the page arms its own ungated loop
 * through `/api/autonudge`, over the gateway's default patrol if one is there;
 * deletes it when the last goal goes; and the transcript reads goal wakes as their
 * own thread.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { GOALS_BANNER, goalInstruction, isGoalWake, syncGoalLoop } from '../pages/chat/page/goalLoop'
import { GOALS_THREAD, buildAskTurn, deriveThreads, goalCheckPending, latestGoalReport, splitGoalBlocks } from '../pages/chat/page/docThreads'
import { boardHasGoals, boardGoalEvery, parseBoard, serializeBoard } from '../pages/chat/page/board/boardModel'
import type { ChatMessage } from '../types'

type Call = { url: string; method: string; body?: Record<string, unknown> }
type Stored = { id: string; message: string; idle_secs: number; active: boolean; gate: boolean; banner: string; defaultPatrol?: boolean }

/**
 * A stub of the loop routes: one stored loop per slot, and a record of every call.
 * GET answers the stored record as the route does, and an arm over a loop is
 * refused unless that loop is the gateway's default patrol.
 */
function stubLoops(initial: Stored | null) {
  let loop = initial
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : undefined
    calls.push({ url, method, body })
    const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
    if (method === 'GET') {
      return json({ loop: loop && { id: loop.id, message: loop.message, idle_secs: loop.idle_secs, active: loop.active, gate: loop.gate, banner: loop.banner } })
    }
    if (method === 'POST' && url === '/api/autonudge') {
      if (loop && !(loop.defaultPatrol && loop.active)) return json({ error: 'a loop is already armed on this session' }, 409)
      loop = { id: 'L1', message: String(body?.message), idle_secs: Number(body?.idle_secs), active: true, gate: body?.gate === true, banner: String(body?.banner) }
      return json({ ok: true, loop })
    }
    if (method === 'PATCH' && loop) {
      loop = { ...loop, ...(body as object) }
      return json({ ok: true, loop })
    }
    if (method === 'DELETE') loop = null
    return json({ ok: true })
  }))
  return { calls, current: () => loop }
}

afterEach(() => { vi.unstubAllGlobals() })

const DOC = { slotKey: 'doc-slot', kind: 'doc' as const, docId: '0123456789abcdef0123456789abcdef', title: 'Plan' }
const PATROL: Stored = { id: 'P1', message: 'Conductor patrol', idle_secs: 3600, active: true, gate: true, banner: '', defaultPatrol: true }
const summary = (calls: Call[]) => calls.map(c => `${c.method} ${c.url}`)

describe('the goal loop', () => {
  it('arms an ungated loop at the page rate when the first goal appears, and runs its first check now', async () => {
    const { calls, current } = stubLoops(null)
    expect(await syncGoalLoop({ ...DOC, every: 30, active: true })).toBe('armed')
    expect(current()).toMatchObject({ idle_secs: 1800, banner: GOALS_BANNER, gate: false })
    expect(current()?.message).toContain('docs/0123456789abcdef0123456789abcdef.md')
    expect(summary(calls)).toEqual(['GET /api/autonudge/slot/doc-slot', 'POST /api/autonudge', 'POST /api/autonudge/L1/fire'])
    expect(calls[1].body).toMatchObject({ slot_key: 'doc-slot', max_cycles: 0, max_runtime_secs: 0, gate: false })
  })

  it('takes over the gateway\'s gated patrol, since a gated loop would skip the goal checks, and fires at once', async () => {
    const { calls, current } = stubLoops(PATROL)
    expect(await syncGoalLoop({ ...DOC, every: 10, active: true })).toBe('armed')
    expect(current()).toMatchObject({ id: 'L1', gate: false, idle_secs: 600 })
    expect(summary(calls).slice(1)).toEqual(['POST /api/autonudge', 'POST /api/autonudge/L1/fire'])
  })

  it('rewrites its own loop on a rate change without a second arm, and deletes it when the goals go', async () => {
    const { calls, current } = stubLoops(null)
    await syncGoalLoop({ ...DOC, every: 10, active: true })
    expect(await syncGoalLoop({ ...DOC, every: 30, active: true })).toBe('updated')
    expect(current()?.idle_secs).toBe(1800)
    expect(calls.filter(c => c.method === 'POST' && c.url === '/api/autonudge')).toHaveLength(1)
    expect(await syncGoalLoop({ ...DOC, every: 30, active: false })).toBe('removed')
    expect(current()).toBeNull()
    // A goal set again after the last one went arms afresh rather than colliding.
    expect(await syncGoalLoop({ ...DOC, every: 30, active: true })).toBe('armed')
  })

  it('revives its own paused loop and runs a check then', async () => {
    const { calls } = stubLoops({ id: 'L1', message: 'x', idle_secs: 600, active: false, gate: false, banner: GOALS_BANNER })
    expect(await syncGoalLoop({ ...DOC, every: 10, active: true })).toBe('updated')
    expect(summary(calls).slice(1)).toEqual(['PATCH /api/autonudge/L1', 'POST /api/autonudge/L1/fire'])
  })

  it('leaves a loop the conductor armed itself alone: the arm is refused and thrown, and removal touches nothing', async () => {
    const own: Stored = { id: 'W1', message: 'Read your work ledger.', idle_secs: 3600, active: true, gate: true, banner: '' }
    const { calls, current } = stubLoops(own)
    await expect(syncGoalLoop({ ...DOC, every: 10, active: true })).rejects.toThrow('already armed')
    expect(await syncGoalLoop({ ...DOC, every: 10, active: false })).toBe('unchanged')
    expect(current()).toEqual(own)
    expect(calls.filter(c => c.method === 'PATCH' || c.method === 'DELETE')).toEqual([])
  })

  it('throws a refused write, so the page can say the checks are not scheduled', async () => {
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => (init?.method === 'POST'
      ? new Response(JSON.stringify({ error: 'auto-nudge disabled' }), { status: 503 })
      : new Response(JSON.stringify({ loop: null })))))
    await expect(syncGoalLoop({ ...DOC, every: 10, active: true })).rejects.toThrow('auto-nudge disabled')
  })

  it('the instruction is a goal wake on its own, and the board one names its own file', () => {
    expect(isGoalWake(goalInstruction(DOC))).toBe(true)
    expect(goalInstruction(DOC)).toContain('read your work ledger')
    expect(goalInstruction({ ...DOC, kind: 'board' })).toContain('kind "section" with a "goal"')
  })
})

describe('goal wakes in the transcript', () => {
  it('split per goal: each [goal id] block goes to that goal\'s thread; writes apply once from the check', () => {
    const messages = [
      { role: 'user', content: '[Doc ask q1]\nwhy?\n\n---\ncontext' },
      { role: 'assistant', content: 'Because.' },
      { role: 'nudge', content: `[auto-nudge cycle 3]\n${GOALS_BANNER}` },
      { role: 'assistant', content: 'Checked both.\n[goal gabc1]\nMarked #26162 done.\n[goal gxyz2]\nhi\n```doc-append\n- checked\n```' },
    ] as ChatMessage[]
    const threads = deriveThreads(messages)
    expect(threads.q1.messages.map(m => m.text)).toEqual(['why?', 'Because.'])
    expect(threads[GOALS_THREAD].actions).toHaveLength(1)
    expect(threads[GOALS_THREAD].messages.map(m => m.text)).toEqual(['Checked both.'])
    expect(latestGoalReport(threads, 'gabc1')).toBe('Marked #26162 done.')
    expect(latestGoalReport(threads, 'gxyz2')).toBe('hi')
    expect(goalCheckPending(threads)).toBe(false)
    expect(isGoalWake('[auto-nudge cycle 1]\nsomething else')).toBe(false)
  })

  it('a check still running shows every goal working; a reply on a goal\'s pin continues its thread', () => {
    const messages = [
      { role: 'nudge', content: `[auto-nudge cycle 1]\n${GOALS_BANNER}` },
      { role: 'assistant', content: '[goal gabc1]\nTwo tickets moved.' },
      { role: 'user', content: '[Doc ask goal-gabc1]\nwhich two?\n\n---\ncontext' },
      { role: 'assistant', content: '#1 and #2.' },
      { role: 'nudge', content: `[auto-nudge cycle 2]\n${GOALS_BANNER}` },
    ] as ChatMessage[]
    const threads = deriveThreads(messages)
    expect(threads['goal-gabc1'].messages.map(m => `${m.who}: ${m.text}`)).toEqual(['conductor: Two tickets moved.', 'you: which two?', 'conductor: #1 and #2.'])
    expect(goalCheckPending(threads)).toBe(true)
    expect(splitGoalBlocks('no blocks here')).toEqual({ blocks: [], rest: 'no blocks here' })
  })

  it('a reply on a goal\'s pin tells the conductor it is about that goal\'s section', () => {
    const turn = buildAskTurn({ id: 'goal-gabc1', request: 'which two?', quote: '## Tickets\n- one', doc: 'x', title: null, copy: null })
    expect(turn.startsWith('[Doc ask goal-gabc1]\nwhich two?')).toBe(true)
    expect(turn).toContain("The goal's section:")
    expect(turn).toContain('## Tickets\n- one')
  })

  it('the check asks for one tagged block per goal and an overlay item on each', () => {
    const doc = goalInstruction(DOC)
    expect(doc).toContain('[goal <id>]')
    expect(doc).toContain('data-anchor="<the heading\'s exact text>" data-place="margin"')
    expect(goalInstruction({ ...DOC, kind: 'board' })).toContain('data-anchor="shape:<section id>"')
  })
})

describe('board goal rate', () => {
  it('keeps one rate for the board and knows when it has a goal', () => {
    const b = parseBoard(JSON.stringify({ goalEvery: 30, shapes: [{ id: 's', kind: 'section', x: 0, y: 0, w: 300, h: 300, text: 'Todo', goal: { text: 'check', every: 10 } }], connectors: [] }))
    expect(boardGoalEvery(b)).toBe(30)
    expect(boardHasGoals(b)).toBe(true)
    expect(parseBoard(serializeBoard(b)).goalEvery).toBe(30)
    expect(boardGoalEvery(parseBoard(JSON.stringify({ goalEvery: 7, shapes: [] })))).toBe(10)
  })
})
