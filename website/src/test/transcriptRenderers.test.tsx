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
 * The ordering assertions are the load-bearing ones. `mergeRenderers`
 * guarantees that a shape-matched default (a stop event, a sub-agent
 * completion) outranks anything keyed only by role. This module still REPLACES
 * the sub-agent completion, so for that row the guarantee is carried by this
 * module's own array order instead, and reordering the returned array can
 * silently let a role claim swallow it. The stop event is no longer overridden
 * — the default entry draws the same StopEventCard — so it keeps the merge's
 * own guarantee. Both are pinned below.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { ReactElement } from 'react'
import type { ChatMessage } from '../types'
import { mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers, featureRequestRefusalIsNewest } from '../pages/chat/transcriptRenderers'
import { FEATURE_REQUEST_FORM_URL, FEATURE_REQUEST_ROW_META_KEY } from '../prompts/featureRequest'
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

  it('routes a gateway-stamped inject to the card and leaves speech-bearing ones alone', () => {
    // This registry serves ChatPane / SideChat / ChatEmbed. It previously gated on
    // a recognised recovery marker alone, so every other injected shape fell
    // through to the SDK default and painted machine prose as a bubble. The shared
    // resolver closes that on every surface at once.
    const synthesis = msg('inject', {
      content: '[SYSTEM] Sub-agent synthesis: produce the consolidated write-up.',
      meta: { injectKind: 'synthesis' },
    })
    expect(idFor(synthesis)).toBe('recovery_inject')

    // A cron row's scheduled output is the user's own and owns a labelled bubble.
    expect(idFor(msg('inject', {
      content: 'nightly report: nothing regressed',
      meta: { injectKind: 'cron', cronLabel: 'nightly' },
    }))).toBe('inject')

    // build_recovery_requeue replays the user's ORIGINAL message verbatim when the
    // turn emitted nothing. That is speech and must never fold into a note.
    expect(idFor(msg('inject', {
      content: 'run the backend gates on the changed modules',
      meta: { injectKind: 'user_replay' },
    }))).toBe('inject')
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
  it('draws only the visible 🔧 message; the completion sibling resolves to a row that draws nothing', () => {
    // The hidden 🚫 / ✅ sibling shares the role and is read for the auto-denied
    // flag -- drawing it would double the row. It is CLAIMED (by
    // `tool_completion`) rather than left unclaimed, so no surface's
    // unclaimed-role fallback can print it.
    expect(idFor(msg('tool', { content: '🚫 denied by policy' }))).toBe('tool_completion')
    expect(idFor(msg('tool', { content: 'plain text' }))).toBe('tool_completion')
    const entry = resolveRenderer(msg('tool', { content: '✅ done' }), registry())!
    expect(entry.render(msg('tool', { content: '✅ done' }), ctx())).toBeNull()
  })

  it('does not treat a launch-shaped output as a launch without the 🔧 prefix', () => {
    const denied = msg('tool', { content: '🚫 denied', meta: { output: 'Started workflow run `wf_abc123`' } })
    expect(idFor(denied)).toBe('tool_completion')
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

  it('leaves the stop row to the SDK default instead of keeping a second copy', () => {
    // The default entry already draws StopEventCard, so a host copy would only
    // be a second place for the same card to be wired — the drift this pins
    // shut. Resolution above proves the row still reaches the card.
    expect(createTranscriptRenderers({ slot: 's1' }).some(r => r.id === 'stop_event')).toBe(false)
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

describe('a usage-limit error row that refuses the seeded feature-request turn offers the issue form (#13342)', () => {
  const limitRow = msg('error', {
    content: '❌ The monthly usage limit has been reached. Retrying will not help until the limit resets.',
    meta: { kind: 'usage_limit' },
  })
  // The row the pill's flow seeded and sent. The flow stamps the marker on the
  // send's `meta` beside its `sendId`; the gateway persists a send's `meta`
  // verbatim on the user row and echoes it, so the row reads the same before
  // and after a reload and in a second tab -- the host passes nothing.
  const seedRow = msg('user', { content: 'I’d like to request a feature!', meta: { sendId: 's-fr-seed', mid: 'u-1', [FEATURE_REQUEST_ROW_META_KEY]: true } })
  const rows = [seedRow, limitRow]
  const recoverable = { slot: 's1', continuable: true, interrupted: true, onContinue: () => undefined }

  it('hands the card the form route and withholds Continue, which would replay the rejection', () => {
    const el = render(limitRow, recoverable, { index: 1, messages: rows }) as ReactElement
    expect(el.props.featureRequestFormUrl).toBe(FEATURE_REQUEST_FORM_URL)
    expect(el.props.onContinue).toBeUndefined()
  })

  it('reads the kind from the rebuilt carrier too', () => {
    const rebuilt = msg('error', { content: limitRow.content, kind: 'usage_limit' })
    const el = render(rebuilt, { slot: 's1' }, { index: 1, messages: [seedRow, rebuilt] }) as ReactElement
    expect(el.props.featureRequestFormUrl).toBe(FEATURE_REQUEST_FORM_URL)
  })

  it('still claims the refusal across the rows a turn writes between the seed and the error', () => {
    // Tool activity, an inject, a permission card: none of them is a message the
    // user composed, so the seed is still the nearest user row above the error.
    const between = [msg('tool_call', { content: 'read_file' }), msg('permission', { content: 'run x?' }), msg('inject', { content: 'continue' })]
    const messages = [seedRow, ...between, limitRow]
    const el = render(limitRow, recoverable, { index: messages.length - 1, messages }) as ReactElement
    expect(el.props.featureRequestFormUrl).toBe(FEATURE_REQUEST_FORM_URL)
  })

  it('offers nothing on the same row when the user row above carries no marker (not the pill’s turn)', () => {
    const typed = msg('user', { content: 'I’d like to request a feature!', meta: { sendId: 's-typed', mid: 'u-1' } })
    const el = render(limitRow, recoverable, { index: 1, messages: [typed, limitRow] }) as ReactElement
    expect(el.props.featureRequestFormUrl).toBeUndefined()
    // Today's behaviour, untouched: the newest error row of an interrupted turn still resumes.
    expect(el.props.onContinue).toBeTypeOf('function')
  })

  it('reads the marker only as the literal `true`: a truthy look-alike is not a claim', () => {
    // The key is client-stamped, so a shape check is the whole gate: a string
    // or an object under the key is not the flow's stamp.
    for (const value of ['true', 1, {}, 'feature-request']) {
      const odd = msg('user', { content: 'x', meta: { sendId: 's-odd', [FEATURE_REQUEST_ROW_META_KEY]: value } })
      const el = render(limitRow, recoverable, { index: 1, messages: [odd, limitRow] }) as ReactElement
      expect(el.props.featureRequestFormUrl).toBeUndefined()
    }
  })

  // ONE table for the boundary both scans walk, over the transcript's own
  // row-kind vocabulary rather than a role list: a row that OPENS a turn stops
  // the scan (the limit below it is THAT turn's, so the form would misdescribe
  // it and Resume is the retry that helps), and a row appended INTO the seeded
  // turn is walked past (the limit is still the request's own refusal, so the
  // form stays and Resume -- a retry that replays the rejection -- stays
  // withheld). Openers are `TURN_OPENER_ROLES` (typed row, auto-nudge cycle,
  // drained sub-agent completion) plus the inject kinds `INJECT_KIND_OPENS_TURN`
  // classifies as a prompt of their own: a cron notification is an unrelated
  // prompt with its own reply, a synthesis row leads the turn that folds a
  // fan-out. Continuations: a steer (a `user` row with `meta.steer`, persisted by
  // chat_delivery.py and appended optimistically by ChatPage `steer()` -- a
  // role-only scan stops at it and loses the form), a stall `recovery`, and a
  // `user_replay` (build_recovery_requeue re-queues the SAME request verbatim
  // when a turn emitted nothing -- an opener here would hand Resume back on the
  // runtime's own retry of the request). An inject row with no stamped kind is
  // passive: a /note rides the next turn's context, the policy-block notice and
  // the hook-halt marker are display-only rows that dispatch nothing.
  const OPENS = 'opens a turn, so a limit below it gets Resume back and no form'
  const CONTINUES = 'continues the seeded turn, so a limit below it keeps the form and Resume stays withheld'
  it.each<[string, typeof OPENS | typeof CONTINUES, ChatMessage]>([
    ['a typed user row', OPENS, msg('user', { content: 'now refactor the parser', meta: { sendId: 's-typed', mid: 'u-2' } })],
    ['an auto-nudge cycle', OPENS, msg('nudge', { content: '[auto-nudge cycle 3] check the PR' })],
    ['a drained sub-agent completion', OPENS, msg('subagent', { content: '[Subagent completion event]\nagent 1a2b3c4d finished' })],
    ['a cron notification (inject, injectKind cron)', OPENS, msg('inject', { content: '[Cron notification from "nightly"]\nSweep.\n[End of cron notification]', meta: { injectKind: 'cron', cronLabel: 'nightly' } })],
    ['a fan-out synthesis (inject, injectKind synthesis)', OPENS, msg('inject', { content: '[SYSTEM] Sub-agent synthesis: produce the consolidated write-up.', meta: { injectKind: 'synthesis' } })],
    ['a steer the user injected (user row, meta.steer)', CONTINUES, msg('user', { content: 'also mention dark mode', meta: { steer: true, steerState: 'consumed', sendId: 's-steer' } })],
    ['the optimistic steer bubble before its echo', CONTINUES, msg('user', { content: 'also mention dark mode', meta: { steer: true, optimistic: true, sendId: 's-steer' } })],
    ['a stall continuation (inject, injectKind recovery)', CONTINUES, msg('inject', { content: '[Interrupted turn — automatic recovery]\ncontinue from the last committed step', meta: { injectKind: 'recovery' } })],
    ['the runtime replaying the request verbatim (inject, injectKind user_replay)', CONTINUES, msg('inject', { content: 'I’d like to request a feature!', meta: { injectKind: 'user_replay' } })],
    ['a /note from another session (inject, meta.noteSession)', CONTINUES, msg('inject', { content: 'fyi: main moved', cls: 'reconcile-note', meta: { noteSession: 'chat-2' } })],
    ['the display-only policy-block notice (inject, no stamped kind)', CONTINUES, msg('inject', { content: 'a safety policy blocked the call\nthe reason was steered to the agent' })],
  ])('%s %s', (_label, verdict, row) => {
    const opens = verdict === OPENS
    // The row lands after the seeded turn's activity; the limit lands below it.
    const messages = [seedRow, msg('tool_call', { content: 'read_file' }), row, limitRow]
    const el = render(limitRow, recoverable, { index: 3, messages }) as ReactElement
    expect(el.props.featureRequestFormUrl).toBe(opens ? undefined : FEATURE_REQUEST_FORM_URL)
    if (opens) expect(el.props.onContinue).toBeTypeOf('function')
    else expect(el.props.onContinue).toBeUndefined()
    // The composer reads the same boundary, so the screen never argues with itself.
    expect(featureRequestRefusalIsNewest(messages)).toBe(!opens)
    // A continuation never rescues a limit in a LATER typed turn: the scan stops
    // at that turn's own opener first.
    if (!opens) {
      const typed = msg('user', { content: 'now refactor the parser', meta: { sendId: 's-typed', mid: 'u-2' } })
      const later = [seedRow, msg('assistant', { content: 'Filed as #13342.' }), typed, row, limitRow]
      const el2 = render(limitRow, recoverable, { index: 4, messages: later }) as ReactElement
      expect(el2.props.featureRequestFormUrl).toBeUndefined()
    }
  })

  it('offers nothing when the marked row is not in the window (paged out): evidence, never the slot alone', () => {
    const el = render(limitRow, recoverable, { index: 0, messages: [limitRow] }) as ReactElement
    expect(el.props.featureRequestFormUrl).toBeUndefined()
  })

  it('offers nothing on an ordinary failure in the feature-request slot (#4198 shapes keep their rows)', () => {
    // A refused send has no structural kind: the send never went out, so a
    // retry CAN help, and the fallback would misdescribe it as a capacity problem.
    const refused = msg('error', { content: 'Message could not be sent: slot agent mismatch' })
    const el = render(refused, recoverable, { index: 1, messages: [seedRow, refused] }) as ReactElement
    expect(el.props.featureRequestFormUrl).toBeUndefined()
    expect(el.props.onContinue).toBeTypeOf('function')
  })

  describe('featureRequestRefusalIsNewest — the composer stands down with the card', () => {
    it('is true when the seeded turn’s refusal is the newest row', () => {
      expect(featureRequestRefusalIsNewest(rows)).toBe(true)
    })
    it('is false once a later user or assistant row follows the refusal', () => {
      expect(featureRequestRefusalIsNewest([...rows, msg('user', { content: 'ok, plain text then', meta: { sendId: 's-typed' } })])).toBe(false)
    })
    it('is false for a limit hit in a later, user-composed turn', () => {
      const later = msg('user', { content: 'now refactor the parser', meta: { sendId: 's-typed' } })
      expect(featureRequestRefusalIsNewest([seedRow, later, limitRow])).toBe(false)
    })
    it('is false once any turn opener follows the refusal -- a typed row, a nudge, a cron notification, a synthesis -- and stays true past a passive note', () => {
      // Same boundary as the card's scan, walked forward: a later turn has begun
      // either way and the composer's Resume is that turn's. A /note dispatches
      // nothing, so the refusal is still the newest turn's terminal row.
      const openers = [
        msg('user', { content: 'ok, plain text then', meta: { sendId: 's-typed' } }),
        msg('nudge', { content: '[auto-nudge cycle 3] check the PR' }),
        msg('inject', { content: '[Cron notification from "nightly"]\nSweep.\n[End of cron notification]', meta: { injectKind: 'cron', cronLabel: 'nightly' } }),
        msg('inject', { content: '[SYSTEM] Sub-agent synthesis: produce the consolidated write-up.', meta: { injectKind: 'synthesis' } }),
      ]
      for (const opener of openers) expect(featureRequestRefusalIsNewest([...rows, opener]), opener.role + ':' + String(opener.meta?.injectKind ?? '')).toBe(false)
      expect(featureRequestRefusalIsNewest([...rows, msg('inject', { content: 'fyi: main moved', cls: 'reconcile-note', meta: { noteSession: 'chat-2' } })])).toBe(true)
    })
    it('stays true across a steer injected into the seeded turn: the composer must not urge a Resume that replays the rejection', () => {
      const steer = msg('user', { content: 'also mention dark mode', meta: { steer: true, sendId: 's-steer' } })
      expect(featureRequestRefusalIsNewest([seedRow, steer, limitRow])).toBe(true)
    })
    it('is false for a #4198 refused-send row and for a transcript with no error row', () => {
      expect(featureRequestRefusalIsNewest([seedRow, msg('error', { content: 'Message could not be sent: x' })])).toBe(false)
      expect(featureRequestRefusalIsNewest([seedRow])).toBe(false)
    })
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

describe('the single-chat surface renders from THIS row set', () => {
  // Until chat-core P5-b the single-chat surface rendered from its own inline
  // role chain, so this module was a SECOND row set that had to agree with it,
  // and the guard here pinned that agreement role by role. ChatPage now
  // dispatches through the app-sdk registry and SPREADS this factory into its
  // host list (RFC chat-core extraction, P5), so agreement is by construction:
  // a row added here reaches the page and every pane at once, and a page-only
  // row is an explicit entry AFTER the spread. What can still drift is the
  // spread itself -- so pin that, not the roles.
  const chatPageSrc = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf8')

  it('ChatPage spreads createTranscriptRenderers into its host list, ahead of its page-only rows', () => {
    expect(chatPageSrc).toMatch(/import \{ createTranscriptRenderers \} from '\.\/chat\/transcriptRenderers'/)
    const list = chatPageSrc.indexOf('const renderers = mergeRenderers([')
    const spread = chatPageSrc.indexOf('...shared,', list)
    const bubble = chatPageSrc.indexOf('\n      bubble,\n    ])', list)
    expect(list).toBeGreaterThanOrEqual(0)
    expect(spread).toBeGreaterThan(list)
    expect(bubble).toBeGreaterThan(spread)
    expect(chatPageSrc).toMatch(/const shared = createTranscriptRenderers\(\{/)
  })

  it('ChatPage keeps no private copy of a row this set draws', () => {
    // A page entry reusing one of this set's ids would shadow the shared row
    // on the page only -- the fork the spread exists to end. Zero exceptions:
    // the page's one behavioural difference (an unparseable file row falls to
    // its bubble) is a factory OPTION, not a shadowing entry.
    const list = chatPageSrc.indexOf('const renderers = mergeRenderers([')
    const end = chatPageSrc.indexOf('return { renderers, fallback: bubble }', list)
    const pageIds = [...chatPageSrc.slice(list, end).matchAll(/^\s+id: '([a-z_]+)',?$/gm)].map(m => m[1])
    const shared = createTranscriptRenderers({ slot: 's1' }).map(r => r.id)
    const duplicated = pageIds.filter(id => shared.includes(id))
    expect(duplicated).toEqual([])
  })
})
