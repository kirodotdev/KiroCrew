/**
 * Contract for the dashboard's transcript row set.
 *
 * The registry's own defaults are store-free and therefore draw a REDUCED
 * transcript — a static pill for a tool call, and nothing at all for a thinking
 * trace, a sent file, an auto-nudge turn, a workflow or sub-agent launch, a
 * recovery inject or a workflow completion. This module supplies the
 * store-connected set, so what is pinned here is that every one of those rows
 * resolves to an entry that actually DRAWS something, and that the narrow
 * entries win over the broad ones they refine.
 *
 * The ordering assertions are the load-bearing ones. `mergeRenderers` normally
 * guarantees that a shape-matched default (a stop event, a sub-agent
 * completion) outranks anything keyed only by role. This module REPLACES both
 * of those defaults, so after the merge there are no shape-matched defaults
 * left and that guarantee is carried by this module's own array order instead.
 * Reordering the returned array can therefore silently let a role claim swallow
 * a stop event, which is exactly what these tests exist to catch.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { ReactElement } from 'react'
import type { ChatMessage } from '../types'
import { GROUPED_ROLES, mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import { isWorkflowRunTool } from '../pages/chat/WorkflowRunCard'
import { isSpawnRunTool } from '../pages/chat/SubagentRunCard'
import { isWorkflowCompletionMessage } from '../pages/chat/WorkflowCompletionCard'
import { isSubagentCompletionMessage } from '../pages/chat/subagentCompletion'
import { parseRecoveryMessage } from '../pages/chat/RecoveryCard'

const msg = (role: string, over: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content: '', cls: '', ...over }) as ChatMessage

/** The registry a split-view pane actually renders through. */
const registry = (opts: Parameters<typeof createTranscriptRenderers>[0] = { slot: 's1' }) =>
  mergeRenderers(createTranscriptRenderers(opts))

const idFor = (m: ChatMessage, opts?: Parameters<typeof createTranscriptRenderers>[0]) =>
  resolveRenderer(m, registry(opts))?.id

/** Identity `row`/`wrapper` so a render returns the card element itself. */
const ctx = (over: Partial<MessageRenderContext> = {}): MessageRenderContext => ({
  index: 0,
  messages: [],
  running: false,
  key: 'k0',
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: (children) => children,
  row: (children) => children,
  ...over,
})

function render(m: ChatMessage, opts?: Parameters<typeof createTranscriptRenderers>[0], over?: Partial<MessageRenderContext>) {
  const entry = resolveRenderer(m, registry(opts))
  return entry?.render(m, ctx(over))
}

// Fixtures for the two launch rows, checked against the SHARED predicates the
// grouping logic uses — a fixture that stopped matching would otherwise make
// the ordering assertions below pass for the wrong reason.
const workflowLaunch = msg('tool', {
  content: '🔧 workflow_run',
  meta: { output: 'Started workflow run `wf_abc123`' },
})
const subagentLaunch = msg('tool', {
  content: '🔧 spawn_run',
  meta: { output: 'Spawned 2 subagent(s).\n  1a2b3c4d (kirocrew): read specs\n  5e6f7a8b (kirocrew): read code' },
})

describe('fixtures match the shared launch predicates', () => {
  it('is a workflow launch and a spawn launch respectively', () => {
    expect(isWorkflowRunTool(workflowLaunch)).toBe(true)
    expect(isSpawnRunTool(subagentLaunch)).toBe(true)
  })
})

describe('rows the default registry leaves undrawn', () => {
  it('draws a thinking trace, a sent file and an auto-nudge turn', () => {
    expect(idFor(msg('thinking', { content: 'weighing options' }))).toBe('thinking_block')
    expect(idFor(msg('nudge', { content: '[cycle 3]' }))).toBe('nudge')
    expect(idFor(msg('file', { content: '{"filename":"a.png"}' }))).toBe('file')
  })

  it('actually renders them, rather than resolving to an entry that draws nothing', () => {
    expect(render(msg('thinking', { content: 'weighing options' }))).toBeTruthy()
    expect(render(msg('nudge', { content: '[cycle 3]' }))).toBeTruthy()
    expect(render(msg('file', { content: '{"filename":"a.png"}' }))).toBeTruthy()
  })

  it('draws nothing for a thinking row with no content, matching the single-chat surface', () => {
    expect(render(msg('thinking', { content: '' }))).toBeNull()
  })

  it('survives a file row whose payload is not JSON', () => {
    expect(render(msg('file', { content: 'not json' }))).toBeNull()
  })
})

describe('narrow rows win over the broad row they refine', () => {
  it('routes the two tool launches to their cards, not the generic tool line', () => {
    expect(idFor(workflowLaunch)).toBe('workflow_run_tool')
    expect(idFor(subagentLaunch)).toBe('subagent_run_tool')
    expect(idFor(msg('tool', { content: '🔧 grep' }))).toBe('tool')
  })

  it('routes a recovery inject to its card and leaves a cron inject alone', () => {
    const recovery = msg('inject', { content: '[Stalled turn — automatic recovery]\nplease continue' })
    // Guard the fixture: a parse miss would make this pass as a plain inject.
    expect(parseRecoveryMessage(recovery.content)).not.toBeNull()
    expect(idFor(recovery)).toBe('recovery_inject')
    expect(idFor(msg('inject', { content: 'ordinary injection' }))).toBe('inject')
  })

  it('routes a workflow completion to its card and leaves a plain reply alone', () => {
    const completion = msg('assistant', {
      content: '[Workflow completion event]\nWorkflow `demo` (wf_abc123) → **finished**\nResult: ok\n',
    })
    expect(isWorkflowCompletionMessage(completion)).toBe(true)
    expect(idFor(completion)).toBe('workflow_completion')
    expect(idFor(msg('assistant', { content: 'hello' }))).toBe('assistant')
  })
})

describe('the tool row keeps the deny-sibling guard', () => {
  it('claims only the visible 🔧 message', () => {
    // The hidden 🚫 sibling shares the role and is read for the auto-denied
    // flag — drawing it would double the row.
    expect(idFor(msg('tool', { content: '🚫 denied by policy' }))).toBeUndefined()
    expect(idFor(msg('tool', { content: 'plain text' }))).toBeUndefined()
  })

  it('does not treat a launch-shaped output as a launch without the 🔧 prefix', () => {
    const denied = msg('tool', { content: '🚫 denied', meta: { output: 'Started workflow run `wf_abc123`' } })
    expect(idFor(denied)).toBeUndefined()
  })
})

describe('shape still beats role after the defaults are replaced', () => {
  it('draws a stop event as a stop event whatever role carries it', () => {
    expect(idFor(msg('assistant', { kind: 'stop_event' }))).toBe('stop_event')
    expect(idFor(msg('notice', { meta: { kind: 'stop_event' } }))).toBe('stop_event')
    // The regression this guards: `nudge`, `error` and `file` are claimed by
    // this module BY ROLE, and a stop event can travel on any of them.
    expect(idFor(msg('nudge', { kind: 'stop_event' }))).toBe('stop_event')
    expect(idFor(msg('error', { kind: 'stop_event' }))).toBe('stop_event')
  })

  it('keeps the sub-agent completion card ahead of the role rows', () => {
    const completion = msg('subagent', {
      content: '[Subagent completion event]\nAgent `1a2b3c4d` (kirocrew) ✅ completed\nTask: read specs\n',
    })
    expect(isSubagentCompletionMessage(completion)).toBe(true)
    expect(idFor(completion)).toBe('subagent_completion')
  })
})

describe('the error row offers Continue only where the single-chat surface does', () => {
  const errs = [msg('error', { content: 'first' }), msg('assistant', { content: 'x' }), msg('error', { content: 'last' })]
  const recoverable = { slot: 's1', continuable: true, interrupted: true, onContinue: () => undefined }

  it('offers it on the last error only', () => {
    const last = render(errs[2], recoverable, { index: 2, messages: errs }) as ReactElement
    const first = render(errs[0], recoverable, { index: 0, messages: errs }) as ReactElement
    expect(last.props.onContinue).toBeTypeOf('function')
    expect(first.props.onContinue).toBeUndefined()
  })

  it('withholds it when the turn was not interrupted', () => {
    const el = render(errs[2], { ...recoverable, interrupted: false }, { index: 2, messages: errs }) as ReactElement
    expect(el.props.onContinue).toBeUndefined()
  })

  it('withholds it on a surface that cannot continue a turn', () => {
    const el = render(errs[2], { slot: 's1' }, { index: 2, messages: errs }) as ReactElement
    expect(el.props.onContinue).toBeUndefined()
  })
})

describe('rows the defaults already draw correctly are left to them', () => {
  it('keeps the default entry for the rows this module does not claim', () => {
    expect(idFor(msg('user'))).toBe('user')
    expect(idFor(msg('streaming'))).toBe('assistant')
    expect(idFor(msg('notice'))).toBe('notice')
    expect(idFor(msg('mcp_oauth'))).toBe('mcp_oauth')
    expect(idFor(msg('tool_call'))).toBe('tool_lifecycle')
    expect(idFor(msg('tool_result'))).toBe('tool_lifecycle')
    // Still deliberately undrawn, and still resolving to an ENTRY that says so.
    expect(idFor(msg('queued'))).toBe('undrawn')
    expect(idFor(msg('system'))).toBe('undrawn')
  })
})

describe('drift guard against the single-chat row chain', () => {
  // The single-chat surface still renders from its own inline role chain, so
  // this module is a SECOND row set that has to agree with it. Nothing in the
  // type system notices when they diverge: a branch added to ChatPage lands
  // only there, and panes quietly regress to a reduced transcript again — the
  // exact failure mode this PR exists to fix.
  //
  // So pin the agreement mechanically. Every role ChatPage dispatches on must
  // be CLAIMED by some entry in the registry a pane renders through. This is
  // deliberately a claim check, not a render check: several rows resolve only
  // with a content guard (a tool row needs its 🔧 prefix), and what drift
  // breaks is coverage, not the guard.
  //
  // This guard is PERMANENT. Converging the two row sets by moving ChatPage
  // onto this registry was considered and rejected (#3332, closed not-planned):
  // the single-chat surface has no problem to fix, so migrating it is risk
  // without payoff. The asymmetry is what makes the guard sufficient — ChatPage
  // owns the policy, so drift can only ever degrade a DOWNSTREAM surface, never
  // that one. If a row both sides draw ever diverges in component or props,
  // strengthen this into a static comparison of what each side passes rather
  // than migrating.
  const chatPageSrc = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf8')

  const rolesInChatPage = [...new Set(
    [...chatPageSrc.matchAll(/\.role === '([a-z_]+)'/g)].map(m => m[1]),
  )].sort()

  it('finds the role chain it is guarding (a rename must fail loudly, not silently pass)', () => {
    // If ChatPage stops matching this shape the extraction returns nothing and
    // every assertion below becomes vacuous, so assert the corpus itself.
    expect(rolesInChatPage.length).toBeGreaterThan(8)
    expect(rolesInChatPage).toContain('assistant')
    expect(rolesInChatPage).toContain('user')
    expect(rolesInChatPage).toContain('tool')
  })

  it('claims every role the single-chat chain dispatches on', () => {
    const active = registry()
    // A `'*'` entry carrying a `match` is SHAPE-matched (a stop event, a
    // sub-agent completion) and claims no role — counting it would make this
    // guard vacuous, since every role would look covered by it.
    const claims = (role: string) =>
      active.some(r => r.roles.includes(role) || (r.roles.includes('*') && !r.match))
    // A grouped role is assembled into the collapsible group BEFORE per-row
    // resolution, so no row entry is expected to claim it — the group owns its
    // display. Read from the SDK's own export rather than hardcoded, so a
    // change to what gets grouped moves this exclusion with it.
    const unclaimed = rolesInChatPage
      .filter(role => !GROUPED_ROLES.includes(role))
      .filter(role => !claims(role))
    expect(unclaimed).toEqual([])
  })
})
