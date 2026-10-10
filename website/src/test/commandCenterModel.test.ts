import { describe, expect, it } from 'vitest'
import { FAILED_WORKFLOW_BLOCKED_MS, buildCommandCenter, questionText, runTitle, scopedSlots, slotKey, type CommandCenterSources } from '../pages/chat/command-center/model'
import type { ChatSlot, SubagentActivity } from '../types'

const slot = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({ key, messages: 0, running: false, ...extra })
const agent = (id: string, extra: Partial<SubagentActivity> = {}): SubagentActivity => ({ id, task: id, agent: 'worker', status: 'running', streaming: '', lastTool: '', startedAt: 0, elapsed: 0, ...extra })
const sources = (extra: Partial<CommandCenterSources> = {}): CommandCenterSources => ({
  slots: [slot('root'), slot('child', { created_by: 'root' }), slot('other')],
  root: 'root', subagents: {}, workflows: [], questions: [], approvals: [], ...extra,
})

describe('command center projection', () => {
  it.each(['slack', 'discord', 'telegram', 'whatsapp', 'webex', 'wecom', 'teams', 'weixin', 'imessage', 'feishu', 'unified'])('matches the backend safe key for %s without dropping its namespace', namespace => {
    expect(slotKey(`${namespace}:1785.12/你好 é²-_.`)).toBe(`${namespace}_1785.12_你好_é²-_.`)
    expect(slotKey(`${namespace}_1785.12`)).toBe(`${namespace}_1785.12`)
  })
  it.each(['dashboard:plain', 'plain', 'unknown:1785/12', 'cron:1785/12', 'SLACK:1785/12', 'dashboard:slack:a/b', '1785.12'])('does not fold unknown or already unscoped identities: %s', key => {
    expect(slotKey(key)).toBe(key.startsWith('dashboard:') ? key.slice(10) : key)
  })
  it('matches channel creator, question, approval and workflow feeds but fails closed for absent roots', () => {
    const input = sources({ root: 'slack_1785.12', slots: [slot('slack_1785.12'), slot('child', { created_by: 'slack:1785.12' }), slot('slack_999')],
      questions: [{ slot: 'slack:1785.12', card_id: 'q', questions: [] }, { slot: 'slack:999', card_id: 'other', questions: [] }],
      approvals: [{ id: 'approval', slot: 'slack:1785.12' }, { id: 'foreign', slot: 'slack:999' }],
      workflows: [{ run_id: 'own', session_key: 'slack:1785.12' }, { run_id: 'foreign', session_key: 'slack:999' }],
    })
    const model = buildCommandCenter(input)
    expect(model.nodes.map(n => n.id)).toEqual(['session:slack_1785.12', 'session:child', 'workflow:own'])
    expect(model.attention.map(a => [a.kind, a.slot])).toEqual([['question', 'slack_1785.12'], ['approval', 'slack_1785.12']])
    expect(buildCommandCenter({ ...input, root: 'slack:missing' }).nodes).toEqual([])
    expect(buildCommandCenter({ ...input, root: 'slack:1785.12' }).nodes).toEqual([])
  })
  it('counts a failed workflow as blocked only within the window after it ended', () => {
    const now = Date.parse('2026-10-09T12:00:00Z')
    const iso = (ms: number) => new Date(ms).toISOString()
    const model = buildCommandCenter(sources({ workflows: [
      { run_id: 'fresh', session_key: 'dashboard:root', status: 'failed', ended_at: iso(now - 60_000) },
      { run_id: 'old', session_key: 'dashboard:root', status: 'failed', ended_at: iso(now - FAILED_WORKFLOW_BLOCKED_MS - 60_000) },
      { run_id: 'unknown', session_key: 'dashboard:root', status: 'failed' },
    ] }), now)
    const state = (id: string) => model.nodes.find(n => n.ref === id)?.state
    expect(state('fresh')).toBe('blocked')
    expect(state('old')).toBe('stopped')
    // A gateway that does not report an end time keeps the old behaviour.
    expect(state('unknown')).toBe('blocked')
    expect(model.blocked).toBe(2)
  })

  it('keeps opaque IDs for routing and gives unnamed runs human-readable labels', () => {
    const model = buildCommandCenter(sources({
      slots: [slot('opaque-session', { title: 'opaque-session' })], root: 'opaque-session',
      subagents: { 'opaque-session': { agent: agent('opaque-agent') } },
      workflows: [{ run_id: 'opaque-workflow', session_key: 'dashboard:opaque-session' }],
    }))
    expect(model.nodes.map(runTitle)).toEqual(['Session 1', 'Worker task 2', 'Workflow 3'])
    expect(model.nodes.map(n => n.ref)).toEqual(['opaque-session', 'opaque-agent', 'opaque-workflow'])
  })
  it('includes descendants, never unrelated sessions or their questions', () => {
    const model = buildCommandCenter(sources({
      slots: [slot('root'), slot('child', { created_by: 'root' }), slot('grandchild', { created_by: 'child' }), slot('other')],
      questions: [{ slot: 'child', card_id: 'q1', questions: [] }, { slot: 'other', card_id: 'q2', questions: [] }],
    }))
    expect(model.nodes.map(n => n.id)).toEqual(['session:root', 'session:child', 'session:grandchild'])
    expect(model.attention.map(a => a.id)).toEqual(['question:child:q1'])
  })
  it('lists an idle session\'s trailing [OPTIONS:] ask as a question, unless a real card, live turn or queued answer outranks it', () => {
    const asking = { has_options: true, options: ['Keep the blur', 'Make it clear'] }
    const model = buildCommandCenter(sources({ slots: [slot('root', asking), slot('child', { created_by: 'root', ...asking, running: true }),
      slot('stalled', { created_by: 'root', ...asking, interrupted: true }), slot('queued', { created_by: 'root', ...asking, queue_depth: 1 }),
      slot('carded', { created_by: 'root', ...asking }), slot('other', asking)],
      questions: [{ slot: 'carded', card_id: 'c', questions: [] }] }))
    expect(model.attention.map(a => [a.kind, a.slot, !!a.question?.followUp])).toEqual([['question', 'carded', false], ['question', 'root', true]])
    expect(model.attention[1].question!.questions[0].options.map(o => o.label)).toEqual(['Keep the blur', 'Make it clear'])
    expect(questionText(model.attention[1].question!)).toBe('The session is waiting for your choice.')
    expect(model.settled).toBe(false)
    expect(buildCommandCenter(sources({ slots: [slot('root', { has_options: false, options: ['stale'] })] })).attention).toEqual([])
  })
  it.each(['/clear', '  /model x'])('drops the slash-command follow-up label %j while keeping a safe choice', label => {
    const model = buildCommandCenter(sources({ slots: [slot('root', { has_options: true, options: [label, 'Keep it'] })] }))
    expect(model.attention).toHaveLength(1)
    expect(model.attention[0].question!.questions[0].options).toEqual([{ label: 'Keep it' }])
  })
  it('omits a follow-up question when every option is a slash command', () => {
    const model = buildCommandCenter(sources({ slots: [slot('root', { has_options: true, options: ['/clear'] })] }))
    expect(model.attention).toEqual([])
  })
  it('keys identical options separately for each new reply but keeps the same reply stable', () => {
    const card = (options_ts: string) => buildCommandCenter(sources({ slots: [slot('root', {
      options_ts, has_options: true, options: ['Keep the blur', 'Make it clear'],
    })] })).attention[0]
    const first = '2026-10-01T12:00:00-07:00'
    const second = '2026-10-01T12:01:00-07:00'
    expect(card(first).id).toBe(card(first).id)
    expect(card(first).id).not.toBe(card(second).id)
  })
  it('keeps an options question stable when a passive row advances last_ts', () => {
    const card = (last_ts: string) => buildCommandCenter(sources({ slots: [slot('root', {
      last_ts, options_ts: '2026-10-01T12:00:00-07:00', has_options: true, options: ['Keep the blur', 'Make it clear'],
    })] })).attention[0]
    expect(card('2026-10-01T12:00:00-07:00').id).toBe(card('2026-10-01T12:01:00-07:00').id)
  })
  it('does not loop on cyclic or orphaned creator edges', () => {
    expect(scopedSlots([slot('a', { created_by: 'b' }), slot('b', { created_by: 'a' }), slot('orphan', { created_by: 'missing' })], 'a').map(s => s.key)).toEqual(['a', 'b'])
    expect(scopedSlots([slot('a')], 'missing')).toEqual([])
    expect(scopedSlots([slot('a')], null)).toHaveLength(1)
  })
  it('keeps run identities distinct, including native subagents', () => {
    const model = buildCommandCenter(sources({
      subagents: { root: { same: agent('same'), native: agent('native:1') } },
      workflows: [{ run_id: 'same', session_key: 'dashboard:root', status: 'running' }],
    }))
    expect(new Set(model.nodes.map(n => n.id)).size).toBe(model.nodes.length)
  })
  it('never interprets an idle session or worker done report as accepted work', () => {
    const model = buildCommandCenter(sources({ work: { items: [
      { item_id: 'a', title: 'A', state: 'open', status: 'done' },
      { item_id: 'b', title: 'B', state: 'accepted', status: 'done' },
    ] } }))
    expect(model.progress).toEqual({ done: 1, total: 2, source: 'work' })
    expect(model.nodes[0].state).toBe('idle')
  })
  it('separates external blockers from actionable human questions', () => {
    const model = buildCommandCenter(sources({ work: { items: [
      { item_id: 'b', title: 'Blocked', state: 'open', status: 'blocked', summary: 'Upstream outage' },
      { item_id: 'q', title: 'Decision', state: 'open', status: 'question', summary: 'Ask conductor' },
    ] } }))
    expect(model.attention).toEqual([])
    expect(model.workItems.map(i => i.state)).toEqual(['blocked', 'waiting'])
    expect(model.blocked).toBe(1)
  })
  it('deduplicates approvals exposed by both feeds and preserves exact request IDs', () => {
    const model = buildCommandCenter(sources({
      slots: [slot('root', { pending_approval: true, pending_approval_info: { origin: 'coordinator', request_id: 'req', tool: 'shell', tool_input: 'pwd', tool_kind: 'execute' } })],
      approvals: [{ id: 'req', instance: 'inst-req', slot: 'dashboard:root', tool: 'shell' }],
    }))
    expect(model.attention).toHaveLength(1)
    expect(model.attention[0]).toMatchObject({ id: 'approval:root:req:inst-req', slot: 'root', approvalMode: 'normal', approval: { id: 'req', slot: 'dashboard:root' } })
  })
  it('keys a coordinator replacement request separately from the decided card it replaces', () => {
    const card = (instance: string) => buildCommandCenter(sources({
      slots: [slot('root')],
      approvals: [{ id: 'reused', instance, slot: 'dashboard:root', tool: 'shell' }],
    })).attention[0]
    expect(card('first').id).not.toBe(card('second').id)
    expect(card('second').approval).toMatchObject({ id: 'reused', instance: 'second' })
  })
  it('requires a native request instance and keys replacement cards separately', () => {
    const base = { origin: 'native' as const, request_id: 'reused', tool: 'shell', tool_input: 'pwd', tool_kind: 'execute' }
    const card = (request_mid?: string) => buildCommandCenter(sources({ slots: [slot('root', {
      pending_approval: true, pending_approval_info: { ...base, request_mid },
    })] })).attention[0]
    expect(card().approval).toBeUndefined()
    expect(card('first-row').id).not.toBe(card('second-row').id)
    expect(card('second-row').approval).toMatchObject({ id: 'reused', request_mid: 'second-row' })
  })

  it('keeps simultaneous same-id native and coordinator requests distinct', () => {
    const input = sources({ slots: [slot('root', { pending_approval: true, pending_approval_info: { origin: 'native', request_mid: 'row-same', request_id: 'same', tool: 'native tool', tool_input: 'native command', tool_kind: 'execute', tool_purpose: 'Native purpose' } })] })
    const together = buildCommandCenter({ ...input, approvals: [{ id: 'same', slot: 'dashboard:root', tool: 'coordinator tool', tool_purpose: 'Coordinator purpose' }] }).attention
    expect(together).toHaveLength(2)
    const before = together.find(item => !item.native)!
    const after = buildCommandCenter(input).attention[0]
    expect(before.native).toBe(false)
    expect(after.native).toBe(true)
    expect(before.id).not.toBe(after.id)
    expect(after.approval?.tool).toBe('native tool')
    expect(before.approval?.tool_purpose).toBe('Coordinator purpose')
    expect(after.approval?.tool_purpose).toBe('Native purpose')
    expect(together.find(item => item.native)).toEqual(after)
  })

  it.each([undefined, 'coordinator'] as const)('does not guess native authority for origin=%s without a coordinator inventory record', origin => {
    const model = buildCommandCenter(sources({ slots: [slot('root', { pending_approval: true,
      pending_approval_info: { origin, request_id: 'req', tool: 'shell', tool_input: 'pwd', tool_kind: 'execute' } })] }))
    expect(model.attention).toHaveLength(1)
    expect(model.attention[0].approval).toBeUndefined()
    expect(model.attention[0].native).not.toBe(true)
  })

  it('keeps an unproven legacy slot request separate from a matching coordinator inventory', () => {
    const model = buildCommandCenter(sources({ slots: [slot('root', { pending_approval: true,
      pending_approval_info: { request_id: 'req', tool: 'shell', tool_input: 'pwd', tool_kind: 'execute' } })],
      approvals: [{ id: 'req', slot: 'dashboard:root' }] }))
    expect(model.attention).toHaveLength(2)
    expect(model.attention.filter(item => item.approval)).toHaveLength(1)
    expect(model.attention.find(item => item.approval)?.approval?.slot).toBe('dashboard:root')
  })

  it('keeps colliding native approval IDs isolated by their owning session', () => {
    const approval = { origin: 'native' as const, request_mid: 'row-same', request_id: 'same', tool: 'shell', tool_input: 'pwd', tool_kind: 'execute' }
    const model = buildCommandCenter(sources({ slots: [slot('root', { pending_approval: true, pending_approval_info: approval }), slot('child', { created_by: 'root', pending_approval: true, pending_approval_info: approval, trust_reads: true })] }))
    expect(model.attention.map(a => a.id)).toEqual(['native-approval:root:same:row-same', 'native-approval:child:same:row-same'])
    expect(model.attention.map(a => a.approvalMode)).toEqual(['normal', 'trust_reads'])
  })
  it('counts only explicit failed and stalled runs as blocked', () => {
    const model = buildCommandCenter(sources({ subagents: { root: {
      running: agent('running'), error: agent('error', { status: 'error', error: 'failed' }),
      stalled: agent('stalled', { stalled: true }), done: agent('done', { status: 'done' }),
    } } }))
    expect(model.blocked).toBe(2)
    expect(model.running).toBe(1)
    expect(model.nodes.find(n => n.id === 'subagent:done')?.state).toBe('done')
  })
  it('keeps backend errors separate from ordinary activity details', () => {
    const model = buildCommandCenter(sources({
      subagents: { root: { failed: agent('failed', { status: 'error', error: 'Worker failed', lastTool: 'Reading files' }) } },
      workflows: [{ run_id: 'failed', session_key: 'dashboard:root', status: 'failed', error: 'Workflow failed', last_log: 'Preparing output' }],
    }))
    expect(model.nodes.find(n => n.id === 'subagent:failed')).toMatchObject({ error: 'Worker failed', detail: 'Reading files' })
    expect(model.nodes.find(n => n.id === 'workflow:failed')).toMatchObject({ error: 'Workflow failed', detail: 'Preparing output' })
  })
  it('retains workflow completion and ignores unowned runs', () => {
    const model = buildCommandCenter(sources({ workflows: [
      { run_id: 'done', session_key: 'dashboard:child', status: 'finished' },
      { run_id: 'unowned', session_key: '', status: 'finished' },
      { run_id: 'other', session_key: 'dashboard:other', status: 'failed' },
    ] }))
    expect(model.nodes.filter(n => n.kind === 'workflow').map(n => n.ref)).toEqual(['done'])
    expect(model.nodes.find(n => n.id === 'workflow:done')?.state).toBe('done')
  })
  it('rests a paused workflow but keeps planning workflows and pending workers unsettled', () => {
    const workflow = (status: string) => buildCommandCenter(sources({ workflows: [
      { run_id: status, session_key: 'dashboard:root', status },
    ] }))
    const paused = workflow('paused')
    // Still labelled as waiting, but no tile counts it, so it cannot pin the dock.
    expect(paused.nodes.find(n => n.id === 'workflow:paused')?.state).toBe('waiting')
    expect(paused.settled).toBe(true)
    expect(workflow('pausing').settled).toBe(true)
    expect(workflow('planning').settled).toBe(false)
    expect(workflow('finished').settled).toBe(true)
    // A queued worker also reads `waiting` and is the task's own unfinished work.
    const pending = buildCommandCenter(sources({ subagents: { root: { w: agent('w', { status: 'pending' }) } } }))
    expect(pending.nodes.find(n => n.id === 'subagent:w')?.state).toBe('waiting')
    expect(pending.settled).toBe(false)
  })
  it('uses a real todo denominator when there is no work board', () => {
    const model = buildCommandCenter(sources({ slots: [slot('root', { todo: { description: 'Plan', tasks: [{ id: '1', text: 'Build', completed: true }, { id: '2', text: 'Test', completed: false }], total: 2, completed: 1, current: 'Test' } })] }))
    expect(model.progress).toEqual({ done: 1, total: 2, source: 'todo' })
  })
  it('shows missing approval/question payloads without inventing an executable action', () => {
    const model = buildCommandCenter(sources({ slots: [slot('root', { needs_input: true, pending_approval: true })] }))
    expect(model.nodes[0].state).toBe('needs_input')
    expect(model.attention[0]).toMatchObject({ kind: 'approval', approvalMode: 'normal' })
    expect(model.attention[0].approval).toBeUndefined()
  })
  it('does not hide an approval with missing details behind a question from the same session', () => {
    const model = buildCommandCenter(sources({
      slots: [slot('root', { pending_approval: true })],
      questions: [{ slot: 'root', card_id: 'card', questions: [] }],
    }))
    expect(model.attention.map(a => a.kind)).toEqual(['question', 'approval'])
  })
  it('keeps identical question-card IDs in different sessions distinct', () => {
    const model = buildCommandCenter(sources({ questions: [
      { slot: 'root', card_id: 'card', questions: [] },
      { slot: 'child', card_id: 'card', questions: [] },
    ] }))
    expect(model.attention.map(a => a.id)).toEqual(['question:root:card', 'question:child:card'])
  })

  it.each([
    ['delegated work before live subagent frames arrive', { subagents_running: true }],
    ['a queued message', { queue_depth: 1 }],
  ] satisfies [string, Partial<ChatSlot>][])('keeps an idle turn unsettled while %s remains', (_description, activity) => {
    const model = buildCommandCenter(sources({ slots: [slot('root', activity)] }))
    expect(model.nodes[0].state).toBe('running')
    expect(model.settled).toBe(false)
  })

  it('settles only when every run rests, the plan is complete and the board omitted nothing', () => {
    const done = { item_id: 'a', title: 'a', state: 'accepted' }
    expect(buildCommandCenter(sources({ work: { items: [done] } })).settled).toBe(true)
    // A rejected item rests without counting as done; the board is still finished.
    expect(buildCommandCenter(sources({ work: { items: [done, { item_id: 'r', title: 'r', state: 'rejected' }] } })).settled).toBe(true)
    expect(buildCommandCenter(sources({ work: { items: [done], omitted: 1 } })).settled).toBe(false)
    expect(buildCommandCenter(sources({ work: { items: [done, { item_id: 'b', title: 'b', state: 'dispatched' }] } })).settled).toBe(false)
    expect(buildCommandCenter(sources({ slots: [slot('root', { todo: { tasks: [{ id: '1', text: 'x', completed: false }], total: 1, completed: 0 } }), slot('child', { created_by: 'root' })] })).settled).toBe(false)
    // A board supplies the progress number, but the plan is still tested on its own.
    const halfDone = slot('root', { todo: { tasks: [{ id: '1', text: 'x', completed: true }, { id: '2', text: 'y', completed: false }], total: 2, completed: 1 } })
    expect(buildCommandCenter(sources({ slots: [halfDone, slot('child', { created_by: 'root' })], work: { items: [done] } })).settled).toBe(false)
    expect(buildCommandCenter(sources({ subagents: { root: { w: agent('w') } } })).settled).toBe(false)
    expect(buildCommandCenter(sources({ subagents: { root: { w: agent('w', { status: 'done' }) } } })).settled).toBe(true)
    expect(buildCommandCenter(sources({ approvals: [{ id: 'p', slot: 'child' }] })).settled).toBe(false)
  })
})


describe('effectiveApprovalMode', () => {
  it('reads a live app-armed scoped grant as trust, like the chat header', async () => {
    const { effectiveApprovalMode } = await import('../pages/chat/command-center/model')
    const { slotApprovalMode } = await import('../utils/slotApprovalMode')
    const slot = { key: 'crew', messages: 0, running: false, trust: false, trust_scope: 'app:issue-radar' }
    expect(effectiveApprovalMode('normal', slot)).toBe('trust')
    expect(effectiveApprovalMode('normal', slot)).toBe(slotApprovalMode('normal', slot))
    expect(effectiveApprovalMode('normal', { ...slot, trust_scope: '' })).toBe('normal')
  })
})
