/**
 * `groupDisplayItems` + `applyRunningState` — the transcript grouping pass, split
 * out of ChatPage so the `slotRunning` flag stops re-running an O(N) pass.
 *
 * Grouping decides what the user actually SEES, so these tests pin the semantics
 * of the split rather than its performance: the same messages must produce the
 * same items, and the running flag must land on exactly one element.
 */
import { describe, it, expect } from 'vitest'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import type { ChatMessage } from '../types'
import type { DisplayItem } from '../pages/chat/types'

const msg = (role: string, content = ''): ChatMessage =>
  ({ role, content, cls: '' } as ChatMessage)

/** A turn long enough to collapse: needs a working step and > 2 items. */
const workingTurn = () => [msg('assistant', 'a'), msg('tool', 't'), msg('assistant', 'b')]

/** A sub-agent completion event SubagentCompletionCard can parse. */
const COMPLETION = [
  '[Subagent completion event]',
  'Agent `53e3e5eb` (kirocrew) completed ✅',
  'Task: map the picker',
  '',
  'done',
].join('\n')

/** The synthesis injection `_run_pending_synthesis` appends before the turn that
 *  folds every sub-agent result into one answer. */
const synthesisRow = (): ChatMessage =>
  ({ role: 'inject', content: '[SYSTEM] Sub-agent synthesis: …', cls: '',
     meta: { injectKind: 'synthesis' } } as unknown as ChatMessage)

const isTurn = (d: DisplayItem): d is { kind: 'turn'; items: never[]; complete: boolean; interim?: boolean } =>
  d.kind === 'turn'

describe('groupDisplayItems', () => {
  it('drops permission messages and unparseable subagent injections from the transcript', () => {
    const { turns } = groupDisplayItems([
      msg('user', 'u'), msg('permission', 'p'), msg('subagent', 's'), msg('assistant', 'a'),
    ])
    const singles = turns.filter(t => t.kind === 'single')
    expect(singles.map(t => (t as { msg: ChatMessage }).msg.role)).toEqual(['user', 'assistant'])
  })

  it('keeps a subagent completion the card can render, and opens a turn on it', () => {
    // The event IS the next turn's input, so the agent's reply must group BELOW
    // the card rather than beside it.
    const completion = msg('subagent', COMPLETION)
    const { turns } = groupDisplayItems([
      msg('user', 'u'), ...workingTurn(), completion, ...workingTurn(),
    ])
    const singles = turns.filter(t => t.kind === 'single') as { msg: ChatMessage }[]
    expect(singles.map(t => t.msg.role)).toEqual(['user', 'subagent'])
    // Two collapsible turns: one under the user message, one under the card.
    expect(turns.filter(isTurn)).toHaveLength(2)
  })

  it('folds the fan-out region into one interim turn when a synthesis row follows', () => {
    // The synthesis injection is the proof that everything since the user's
    // prompt was interim: the per-completion summaries it is about to restate.
    const completion = { ...msg('subagent', COMPLETION), meta: { subagentCompletion: {} } }
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      msg('assistant', 'spawned'),
      completion as ChatMessage,
      msg('assistant', 'per-completion summary'),
      synthesisRow(),
      msg('assistant', 'the synthesis answer'),
    ])
    // user row, the folded interim turn, then the synthesis turn.
    const interim = turns.filter(t => isTurn(t) && t.interim)
    expect(interim).toHaveLength(1)
    // The completion card and both interim messages are INSIDE the fold — a
    // short reply is normally spread loose, so flattening is what makes this work.
    const folded = (interim[0] as { items: { msg: ChatMessage }[] }).items.map(i => i.msg.content)
    expect(folded).toContain('spawned')
    expect(folded).toContain('per-completion summary')
    expect(folded).toContain(COMPLETION)
    // Nothing is dropped: the synthesis answer stays outside the fold.
    const loose = turns
      .filter(t => t.kind === 'single')
      .map(t => (t as { msg: ChatMessage }).msg.content)
    expect(loose).toContain('the synthesis answer')
    expect(loose).not.toContain('per-completion summary')
  })

  it('leaves the fan-out unfolded when no synthesis row follows', () => {
    // A single-agent spawn, or a blocking spawn_sub_agents, never emits one —
    // that must read exactly as it does today.
    const completion = msg('subagent', COMPLETION)
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      completion,
      msg('assistant', 'keep this'),
    ])
    expect(turns.filter(t => isTurn(t) && t.interim)).toHaveLength(0)
    const assistants = turns
      .filter(t => t.kind === 'single' && (t as { msg: ChatMessage }).msg.role === 'assistant')
      .map(t => (t as { msg: ChatMessage }).msg.content)
    expect(assistants).toContain('keep this')
  })

  it('starts a fresh interim region at the next user message', () => {
    // A later synthesis must not reach back past the user's own prompt and fold
    // an answer they already read.
    const { turns } = groupDisplayItems([
      msg('user', 'first'), msg('assistant', 'answer to first'),
      msg('user', 'second'), msg('assistant', 'spawning'),
      synthesisRow(), msg('assistant', 'synthesis'),
    ])
    const interim = turns.filter(t => isTurn(t) && t.interim) as { items: { msg: ChatMessage }[] }[]
    expect(interim).toHaveLength(1)
    const folded = interim[0].items.map(i => i.msg.content)
    expect(folded).toEqual(['spawning'])
  })

  it('folds a real fan-out transcript down to the prompt, one toggle, and the answer', () => {
    // The row sequence of an actual session (dashboard_chat-275): the user asks,
    // the agent spawns three agents, replies to completions twice as they land —
    // and NO `subagent` row exists, because a completion delivered while the
    // slot is idle is injected straight into a turn without one. That is why the
    // fold anchors on the synthesis row, which is always written.
    const { turns } = groupDisplayItems([
      msg('user', 'detect the root cause then raise the suggestion'),
      msg('assistant', 'Delegating three independent investigations.'),
      msg('tool', '🔧 Running: @kirocrew-core/spawn_run'),
      msg('assistant', 'Spawned 3 agents — waiting for results.'),
      msg('tool', '🔧 Reading result.txt:1'),
      msg('assistant', 'Root cause identified — investigation B is still running.'),
      msg('tool', '🔧 Reading result.txt:1'),
      msg('assistant', 'All three in. The three investigations agree…'),
      synthesisRow(),
      msg('assistant', '# Renderer crash: root cause and what to do'),
    ])
    const interim = turns.filter(t => isTurn(t) && t.interim) as { items: { msg: ChatMessage }[] }[]
    expect(interim).toHaveLength(1)
    // Both per-completion summaries are behind the one fold.
    const folded = interim[0].items.map(i => i.msg.content)
    expect(folded).toContain('Root cause identified — investigation B is still running.')
    expect(folded).toContain('All three in. The three investigations agree…')
    // What is left at top level: the prompt, the fold, then the synthesis row
    // and its answer (a two-item batch is spread loose, so they render plainly).
    const loose = turns.filter(t => t.kind === 'single') as { msg: ChatMessage }[]
    expect(loose.map(t => t.msg.role)).toEqual(['user', 'inject', 'assistant'])
    expect(loose.map(t => t.msg.content))
      .toContain('# Renderer crash: root cause and what to do')
  })

  it('leaves the region unfolded when an unrelated cron turn landed inside it', () => {
    // The slot's queue is shared: a cron notification can drain while a wave is
    // still landing. Synthesis does not restate a cron reply, so folding it
    // would hide content behind a toggle that promises a repeat below.
    const inject = (kind: string): ChatMessage =>
      ({ role: 'inject', content: `[${kind}]`, cls: '', meta: { injectKind: kind } } as unknown as ChatMessage)
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      msg('assistant', 'spawned'),
      inject('cron'),
      msg('assistant', 'cron answer'),
      synthesisRow(),
      msg('assistant', 'synthesis'),
    ])
    expect(turns.filter(t => isTurn(t) && t.interim)).toHaveLength(0)
    // Present and not behind a fan-out toggle (it lands in an ordinary turn).
    const allContent = turns.flatMap(t =>
      isTurn(t)
        ? (t as unknown as { items: { msg: ChatMessage }[] }).items.map(i => i.msg.content)
        : [(t as unknown as { msg: ChatMessage }).msg.content])
    expect(allContent).toContain('cron answer')
  })

  it('still folds when the only injection inside the region is a stall recovery', () => {
    // A tool-stall recovery IS a continuation of the work being folded, so it
    // must not disqualify the region the way a cron prompt does.
    const recovery = ({ role: 'inject', content: '[Tool stall — automatic recovery]', cls: '',
                        meta: { injectKind: 'recovery' } } as unknown as ChatMessage)
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      msg('assistant', 'spawned'),
      recovery,
      msg('assistant', 'resumed and summarised agent 1'),
      synthesisRow(),
      msg('assistant', 'synthesis'),
    ])
    const interim = turns.filter(t => isTurn(t) && t.interim) as { items: { msg: ChatMessage }[] }[]
    expect(interim).toHaveLength(1)
    expect(interim[0].items.map(i => i.msg.content))
      .toContain('resumed and summarised agent 1')
  })

  it('folds a SECOND wave in the same user turn, keeping round one\'s answer out', () => {
    // The reported bug: every wave after the first rendered its per-completion
    // prose in full, right beside a synthesis that restated it. The region a
    // synthesis row opens used to be disqualified wholesale to protect that
    // row's answer; now the answer is split off at its turn boundary (the
    // `turn_stats` meta the runner stamps) and only it stays outside the fold.
    const answer = (content: string): ChatMessage =>
      ({ role: 'assistant', content, cls: '',
         meta: { turn_stats: { elapsed_ms: 1200 } } } as unknown as ChatMessage)
    const { turns } = groupDisplayItems([
      msg('user', 'judge both stacks'),
      msg('assistant', 'spawning wave 1'),
      synthesisRow(),
      answer('ANSWER ONE'),
      msg('assistant', 'spawning wave 2'),
      msg('tool', '🔧 @kirocrew-core/spawn_run'),
      msg('assistant', 'wave 2 per-completion prose'),
      synthesisRow(),
      answer('ANSWER TWO'),
    ])
    const interim = turns.filter(t => isTurn(t) && t.interim) as { items: { msg: ChatMessage }[] }[]
    // One fold per wave.
    expect(interim).toHaveLength(2)
    const folded = interim.flatMap(t => t.items.map(i => i.msg.content))
    expect(folded).toContain('spawning wave 1')
    expect(folded).toContain('wave 2 per-completion prose')
    // Neither synthesis answer is behind a toggle.
    expect(folded).not.toContain('ANSWER ONE')
    expect(folded).not.toContain('ANSWER TWO')
  })

  it('folds a second wave reached through a queue-drained completion opener', () => {
    // A completion delivered while the slot is busy drains from the queue as a
    // `subagent` row, which opens a turn and flushes the open batch WITHOUT
    // resetting the region. Round one's answer is in that batch, so it has to be
    // settled at this site too: without it the next synthesis reads wave two's
    // own per-completion reply as the answer boundary and re-anchors past the
    // whole wave, leaving it unfolded.
    const answer = (content: string): ChatMessage =>
      ({ role: 'assistant', content, cls: '',
         meta: { turn_stats: { elapsed_ms: 900 } } } as unknown as ChatMessage)
    const completion = ({ ...msg('subagent', COMPLETION), meta: { subagentCompletion: {} } } as ChatMessage)
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      msg('assistant', 'spawning wave 1'),
      synthesisRow(),
      answer('ANSWER ONE'),
      msg('assistant', 'spawning wave 2'),
      completion,
      answer('wave 2 per-completion prose'),
      synthesisRow(),
      answer('ANSWER TWO'),
    ])
    const interim = turns.filter(t => isTurn(t) && t.interim) as { items: { msg: ChatMessage }[] }[]
    const folded = interim.flatMap(t => t.items.map(i => i.msg.content))
    expect(folded).toContain('wave 2 per-completion prose')
    expect(folded).not.toContain('ANSWER ONE')
    expect(folded).not.toContain('ANSWER TWO')
  })

  it('lets a later wave fold even though a cron reply disqualified an earlier one', () => {
    // `regionHasForeign` is scoped to ONE region. A cron notification that
    // drained during wave 1 must not suppress wave 2's fold as well, or a single
    // unrelated inject silently disables the feature for the rest of the turn.
    const answer = (content: string): ChatMessage =>
      ({ role: 'assistant', content, cls: '',
         meta: { turn_stats: { elapsed_ms: 700 } } } as unknown as ChatMessage)
    const cron = ({ role: 'inject', content: '[cron]', cls: '',
                    meta: { injectKind: 'cron' } } as unknown as ChatMessage)
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      msg('assistant', 'spawning wave 1'),
      cron,
      msg('assistant', 'cron answer'),
      synthesisRow(),
      answer('ANSWER ONE'),
      msg('assistant', 'wave 2 per-completion prose'),
      synthesisRow(),
      answer('ANSWER TWO'),
    ])
    const folded = turns
      .filter(t => isTurn(t) && t.interim)
      .flatMap(t => (t as unknown as { items: { msg: ChatMessage }[] }).items.map(i => i.msg.content))
    // Wave 1 stays unfolded (the cron reply is in it), wave 2 folds.
    expect(folded).toContain('wave 2 per-completion prose')
    expect(folded).not.toContain('cron answer')
    expect(folded).not.toContain('ANSWER ONE')
  })

  it('keeps a cron reply that landed AFTER a synthesis answer out of the next fold', () => {
    // The foreign flag must be RE-DERIVED when the region is re-anchored, not
    // cleared: the retained batch can still hold the very row the flag was set
    // for. Clearing it here would fold an unrelated prompt's reply behind the
    // fan-out toggle, which promises a repeat below that never comes.
    const answer = (content: string): ChatMessage =>
      ({ role: 'assistant', content, cls: '',
         meta: { turn_stats: { elapsed_ms: 800 } } } as unknown as ChatMessage)
    const cron = ({ role: 'inject', content: '[cron]', cls: '',
                    meta: { injectKind: 'cron' } } as unknown as ChatMessage)
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      msg('assistant', 'spawning wave 1'),
      synthesisRow(),
      answer('ANSWER ONE'),
      cron,
      msg('assistant', 'cron answer'),
      msg('assistant', 'wave 2 per-completion prose'),
      synthesisRow(),
      answer('ANSWER TWO'),
    ])
    const folded = turns
      .filter(t => isTurn(t) && t.interim)
      .flatMap(t => (t as unknown as { items: { msg: ChatMessage }[] }).items.map(i => i.msg.content))
    expect(folded).not.toContain('cron answer')
    expect(folded).not.toContain('ANSWER ONE')
    expect(folded).not.toContain('ANSWER TWO')
  })

  it('leaves a second wave unfolded when round one\'s answer has no turn boundary', () => {
    // No `turn_stats` anywhere (a turn that errored, an older transcript, or an
    // answer still streaming): the answer cannot be separated, so the region
    // degrades to the pre-fix rendering instead of risking swallowing it.
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      msg('assistant', 'spawning wave 1'),
      synthesisRow(),
      msg('assistant', 'ANSWER ONE'),
      msg('assistant', 'spawning wave 2'),
      synthesisRow(),
      msg('assistant', 'ANSWER TWO'),
    ])
    const folded = turns
      .filter(t => isTurn(t) && t.interim)
      .flatMap(t => (t as unknown as { items: { msg: ChatMessage }[] }).items.map(i => i.msg.content))
    expect(folded).not.toContain('ANSWER ONE')
    expect(folded).not.toContain('ANSWER TWO')
  })

  it('never folds an emitted synthesis answer behind a later wave toggle', () => {
    // A synthesis turn can itself spawn a wave, so two synthesis rows can land
    // in ONE user turn. Round one's answer is a real answer that round two's
    // synthesis does not restate, so it must not end up behind round two's fold.
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      msg('assistant', 'spawning wave 1'),
      synthesisRow(),
      msg('assistant', 'ANSWER ONE'),
      msg('assistant', 'spawning wave 2'),
      synthesisRow(),
      msg('assistant', 'ANSWER TWO'),
    ])
    const folded = turns
      .filter(t => isTurn(t) && t.interim)
      .flatMap(t => (t as unknown as { items: { msg: ChatMessage }[] }).items.map(i => i.msg.content))
    expect(folded).not.toContain('ANSWER ONE')
    expect(folded).not.toContain('ANSWER TWO')
    // Wave 1's own interim work is still folded — the guard is scoped to the
    // region a synthesis row opens, not to the whole turn.
    expect(folded).toContain('spawning wave 1')
  })

  it('leaves the region unfolded when an App Kit note landed inside it', () => {
    // A note is appended as `inject` with `meta.noteSession` and NO injectKind
    // (slot_buffers.py / chat_handlers.py), so a kind-only guard misses it. It is
    // deliberate content the synthesis does not restate.
    const note = ({ role: 'inject', content: 'a pinned note', cls: 'reconcile-note',
                    meta: { noteSession: 'chat-1' } } as unknown as ChatMessage)
    const { turns } = groupDisplayItems([
      msg('user', 'u'),
      msg('assistant', 'spawned'),
      note,
      synthesisRow(),
      msg('assistant', 'synthesis'),
    ])
    expect(turns.filter(t => isTurn(t) && t.interim)).toHaveLength(0)
  })

  it('preserves the original message index on singles', () => {
    // idx must be the index into the INPUT array, not into the filtered output —
    // callers map display rows back to messages with it.
    const { turns } = groupDisplayItems([msg('permission'), msg('user', 'u')])
    const single = turns.find(t => t.kind === 'single') as { idx: number }
    expect(single.idx).toBe(1)
  })

  it('opens a new turn on a user message', () => {
    const { turns } = groupDisplayItems([
      msg('user', 'first'), ...workingTurn(), msg('user', 'second'), ...workingTurn(),
    ])
    const users = turns.filter(t => t.kind === 'single' && (t as { msg: ChatMessage }).msg.role === 'user')
    expect(users).toHaveLength(2)
  })

  it('opens a new turn on a nudge, same as a user message', () => {
    const { turns } = groupDisplayItems([
      msg('user', 'u'), ...workingTurn(), msg('nudge', 'keep going'), ...workingTurn(),
    ])
    // Two collapsed turns, one per prompt — the nudge must not be swallowed into
    // the previous turn's step group.
    expect(turns.filter(isTurn)).toHaveLength(2)
  })

  it('marks every NON-trailing turn complete regardless of running state', () => {
    const { turns, trailingTurnIdx } = groupDisplayItems([
      msg('user', 'u1'), ...workingTurn(), msg('user', 'u2'), ...workingTurn(),
    ])
    const allTurns = turns.filter(isTurn)
    expect(allTurns).toHaveLength(2)
    expect(allTurns[0].complete).toBe(true)
    // The last one is the trailing turn, and grouping always emits it complete.
    expect(trailingTurnIdx).toBeGreaterThanOrEqual(0)
    expect(turns[trailingTurnIdx]).toBe(allTurns[1])
  })

  it('reports trailingTurnIdx = -1 when the trailing group does not collapse', () => {
    // Two items only — below the > 2 threshold, so flushTurn spreads them as
    // loose items and there is no `complete` flag for the running state to touch.
    const { turns, trailingTurnIdx } = groupDisplayItems([msg('user', 'u'), msg('assistant', 'a')])
    expect(trailingTurnIdx).toBe(-1)
    expect(turns.every(t => !isTurn(t))).toBe(true)
  })

  it('reports trailingTurnIdx = -1 for an empty list', () => {
    expect(groupDisplayItems([])).toEqual({ turns: [], trailingTurnIdx: -1 })
  })

  it('does not collapse a turn with no working steps', () => {
    const { turns } = groupDisplayItems([msg('user', 'a'), msg('user', 'b'), msg('user', 'c')])
    expect(turns.filter(isTurn)).toHaveLength(0)
  })

  // #6376: two or more reasoning bursts must be wrapped as a {kind:'turn'} so
  // TurnBlock's mergeTurnThinking can fold them into ONE "Thought process" row.
  // Left as loose singles, each burst renders as its own duplicate row.
  it('wraps a reasoning-only trailing turn into a collapsible turn (no answer/tool yet)', () => {
    // A monitor/nudge cycle that has only emitted reasoning so far: no tool or
    // assistant row, so hasWorkingSteps is false — but the bursts must still be
    // grouped for the dedup, not spread as N loose "Thought process" rows.
    const { turns, trailingTurnIdx } = groupDisplayItems([
      msg('nudge', 'keep going'),
      msg('thinking', 'burst 1'),
      msg('thinking', 'burst 2'),
      msg('thinking', 'burst 3'),
    ])
    const turnObjs = turns.filter(isTurn)
    expect(turnObjs).toHaveLength(1)
    expect(turnObjs[0].items).toHaveLength(3)
    // The reasoning-only turn is the trailing turn, so the running state can
    // land on it.
    expect(trailingTurnIdx).toBeGreaterThanOrEqual(0)
  })

  it('wraps a two-burst reasoning-only batch that the working-steps rule would NOT catch', () => {
    // Two reasoning bursts, no tool/assistant row: hasWorkingSteps is false and
    // there are only 2 items, so the `hasWorkingSteps && length > 2` clause
    // does NOT fire — this batch wraps ONLY on the burst-count rule, so the test
    // fails if that rule is removed or its threshold raised (a real
    // discriminator, unlike a batch that already carries an assistant).
    const { turns } = groupDisplayItems([
      msg('thinking', 'first thought'),
      msg('thinking', 'second thought'),
    ])
    expect(turns.filter(isTurn)).toHaveLength(1)
  })

  it('leaves a SINGLE reasoning burst loose (nothing to dedup)', () => {
    // One burst renders as exactly one row whether loose or wrapped, so it must
    // not synthesize a turn — that would needlessly re-home a lone reasoning row
    // (and regress ChatPage's renderMessage dispatch).
    const { turns } = groupDisplayItems([msg('thinking', 'just one'), msg('assistant', 'answer')])
    expect(turns.filter(isTurn)).toHaveLength(0)
  })

  it('does NOT count empty (placeholder) thinking rows toward the wrap threshold', () => {
    // A bare "Thinking…" placeholder carries no content; two of them (or one
    // content burst + a placeholder) must not synthesize a turn — mirrors
    // mergeTurnThinking, which ignores empty bursts.
    const { turns } = groupDisplayItems([
      msg('user', 'u'), msg('thinking', 'one real'), msg('thinking', ''),
    ])
    expect(turns.filter(isTurn)).toHaveLength(0)
  })
})

describe('applyRunningState', () => {
  const grouped = () => groupDisplayItems([msg('user', 'u'), ...workingTurn()])

  it('returns the grouped array UNCHANGED by identity when not running', () => {
    const g = grouped()
    // Identity matters: a new array here would cascade into the display-index
    // maps and the virtualizer, which is the cost this split exists to avoid.
    expect(applyRunningState(g, false)).toBe(g.turns)
  })

  it('marks the trailing turn incomplete while running', () => {
    const g = grouped()
    const out = applyRunningState(g, true)
    expect(out[g.trailingTurnIdx]).toMatchObject({ kind: 'turn', complete: false })
  })

  it('leaves every other element identity-stable while running', () => {
    const g = groupDisplayItems([msg('user', 'u1'), ...workingTurn(), msg('user', 'u2'), ...workingTurn()])
    const out = applyRunningState(g, true)
    for (let i = 0; i < out.length; i++) {
      if (i === g.trailingTurnIdx) continue
      expect(out[i]).toBe(g.turns[i])
    }
  })

  it('does not mutate the grouped input', () => {
    const g = grouped()
    const trailingBefore = g.turns[g.trailingTurnIdx]
    applyRunningState(g, true)
    expect(g.turns[g.trailingTurnIdx]).toBe(trailingBefore)
    expect((trailingBefore as { complete: boolean }).complete).toBe(true)
  })

  it('is a no-op when running but nothing collapsed', () => {
    const g = groupDisplayItems([msg('user', 'u'), msg('assistant', 'a')])
    expect(applyRunningState(g, true)).toBe(g.turns)
  })

  it('reproduces the pre-split behaviour: trailing complete === !slotRunning', () => {
    // Grouping always emits `complete: true` and this function applies the flag,
    // so the trailing turn's `complete` must equal `!slotRunning` in both
    // directions.
    const g = grouped()
    for (const slotRunning of [true, false]) {
      const out = applyRunningState(g, slotRunning)
      const trailing = out[g.trailingTurnIdx] as { complete: boolean }
      expect(trailing.complete).toBe(!slotRunning)
    }
  })
})
