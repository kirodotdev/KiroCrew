import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import RecoveryCard, { parseRecoveryMessage, resolveInjectCard } from '../pages/chat/RecoveryCard'

// Verbatim prefixes from src/kiro_crew/dashboard/state.py. The separator is an
// em dash, not a hyphen — a mismatch is exactly the drift this suite guards.
const REFUSAL = '[Tool refusal — automatic recovery]'
const STALLED = '[Stalled turn — automatic recovery]'
const TOOL_STALL = '[Tool stall — automatic recovery]'
const CONNECTION = '[Connection lost — automatic recovery]'
const POSTTOKEN = '[Interrupted turn — automatic recovery]'
const EMPTY = '[Empty response — automatic recovery]'
const BUSY = '[Session busy — automatic recovery]'
const HOOK = '[Hook continuation — automatic]'
const HALT = '[Stop-hook nudge cap reached]'
const PROMISE_ONLY = '[Unfinished action — automatic recovery]'
const COMPACTION = '[Context compacted — automatic recovery]'

/** A refusal body shaped the way build_refusal_recovery_prompt() emits it. */
function refusalBody(items: string[]): string {
  return [
    REFUSAL,
    'One or more tool calls in your previous turn were blocked by a Kiro Crew safety policy, which ended the turn early. This was NOT a user action — do not treat it as a cancellation or interruption by the user.',
    '',
    'Blocked:',
    ...items.map(i => `  - ${i}`),
    '',
    'Decide how to proceed: use an allowed alternative (for a shell command, a read-only variant), a different tool, or — if the block is correct and you genuinely cannot proceed — say so and stop. Otherwise continue the task where you left off.',
  ].join('\n')
}

describe('parseRecoveryMessage', () => {
  it('returns null for ordinary injected content', () => {
    expect(parseRecoveryMessage('Just some injected text')).toBeNull()
    expect(parseRecoveryMessage('')).toBeNull()
    // A hyphen instead of an em dash is NOT a recovery row.
    expect(parseRecoveryMessage('[Tool refusal - automatic recovery]\nbody')).toBeNull()
  })

  it('titles a single refusal as the event, not as a completed recovery', () => {
    const p = parseRecoveryMessage(
      refusalBody(['Running: mypy src/…: Blocked by security policy: .*env.*grep.*AWS.*']),
    )
    expect(p?.kind).toBe('refusal')
    expect(p?.title).toBe('Tool call blocked')
    // The attempt is hedged in the detail line; the title never claims success.
    expect(p?.title).not.toMatch(/recover/i)
    expect(p?.detail).toBe('safety policy blocked the call · continuation sent automatically')
  })

  it('surfaces the single deny pattern as the chip', () => {
    const p = parseRecoveryMessage(
      refusalBody(['Running: mypy src/…: Blocked by security policy: .*env.*grep.*AWS.*']),
    )
    expect(p?.chip).toBe('.*env.*grep.*AWS.*')
  })

  it('counts multiple blocked calls in the title', () => {
    const p = parseRecoveryMessage(
      refusalBody([
        'Running: a: Blocked by security policy: .*env.*grep.*AWS.*',
        'Running: b: Blocked by security policy: .*env.*grep.*AWS.*',
        'Running: c: Blocked by security policy: .*env.*grep.*AWS.*',
      ]),
    )
    expect(p?.title).toBe('3 tool calls blocked')
    // One distinct pattern across three calls still shows the pattern itself.
    expect(p?.chip).toBe('.*env.*grep.*AWS.*')
  })

  it('collapses several distinct patterns to a count', () => {
    const p = parseRecoveryMessage(
      refusalBody([
        'Running: a: Blocked by security policy: .*env.*grep.*AWS.*',
        'Running: b: Blocked by security policy: rm -rf /.*',
      ]),
    )
    expect(p?.chip).toBe('2 patterns')
  })

  it('omits the chip when the refusal carries no policy pattern', () => {
    // The read-only bash gate refuses without naming a deny pattern.
    const p = parseRecoveryMessage(refusalBody(['Running: git push: read-only mode']))
    expect(p?.chip).toBe('')
    expect(p?.title).toBe('Tool call blocked')
  })

  it('labels a stalled turn and a stalled tool distinctly', () => {
    const stalled = parseRecoveryMessage(`${STALLED}\nYour previous turn was interrupted…`)
    expect(stalled?.kind).toBe('stalled')
    expect(stalled?.title).toBe('Turn stalled')
    expect(stalled?.chip).toBe('')

    const tool = parseRecoveryMessage(`${TOOL_STALL}\nA tool call stopped producing output…`)
    expect(tool?.kind).toBe('tool_stall')
    expect(tool?.title).toBe('Tool stopped responding')
  })

  it('strips the prefix line from the body', () => {
    const p = parseRecoveryMessage(`${STALLED}\nYour previous turn was interrupted.`)
    expect(p?.body).toBe('Your previous turn was interrupted.')
    expect(p?.body.startsWith('[')).toBe(false)
  })

  it('labels connection loss as a routine interrupted-turn recovery', () => {
    const connection = parseRecoveryMessage(
      `${CONNECTION}\nYour previous turn was interrupted by a lost backend connection.`,
    )
    expect(connection?.kind).toBe('connection')
    expect(connection?.title).toBe('Turn interrupted')
    expect(connection?.detail).toBe('backend error · continuation sent automatically')
    expect(connection?.chip).toBe('')
  })

  it('names a busy session as the cause without attributing it to a backend error', () => {
    // Same shape as the connection card and the same routine severity, but the detail
    // must name the cause that DID happen and not one that did not: nothing was lost
    // or errored, the session was occupied. Distinguishing the two is why this kind
    // exists, so the collapsed card carries it rather than only the expandable body.
    const busy = parseRecoveryMessage(
      `${BUSY}\nYour previous turn was interrupted because the backend session was still busy.`,
    )
    expect(busy?.kind).toBe('busy')
    expect(busy?.title).toBe('Turn interrupted')
    expect(busy?.detail).toBe('session busy · continuation sent automatically')
    expect(busy?.detail).not.toContain('backend error')
    expect(busy?.chip).toBe('')
    expect(busy?.body).toContain('still busy')
  })

  it('labels a transient-backend interruption and an empty generation', () => {
    // Verbatim bodies from chat_utils._POSTTOKEN_RECOVER_MSG /
    // _EMPTY_AUTO_CONTINUE_MSG. Without the recovery card both render as a
    // full-width bubble of machine prose.
    const interrupted = parseRecoveryMessage(
      `${POSTTOKEN}\nThe previous response was interrupted partway through by a transient backend error.`,
    )
    expect(interrupted?.kind).toBe('posttoken')
    expect(interrupted?.title).toBe('Turn interrupted')
    expect(interrupted?.detail).toBe('backend error · continuation sent automatically')
    expect(interrupted?.chip).toBe('')
    expect(interrupted?.body.startsWith('[')).toBe(false)

    const empty = parseRecoveryMessage(
      `${EMPTY}\nYour previous turn produced no output (the model returned an empty response twice).`,
    )
    expect(empty?.kind).toBe('empty')
    expect(empty?.title).toBe('No response returned')
    expect(empty?.detail).toBe('agent returned no output · continuation sent automatically')
  })

  it('names the hook as the cause and does not claim the turn was interrupted', () => {
    // A Stop hook returned {"decision":"block","reason":...}; the reason becomes
    // the body. The turn RAN TO COMPLETION, so any interruption or recovery
    // wording here would describe an event that never happened.
    const hook = parseRecoveryMessage(
      `${HOOK}\nYour previous turn ended by offering a trivial read-only option. Perform it now.`,
    )
    expect(hook?.kind).toBe('hook')
    expect(hook?.title).toBe('Continued by a hook')
    expect(hook?.detail).toBe('requested by a hook · continuing')
    expect(hook?.chip).toBe('')
    expect(hook?.body.startsWith('[')).toBe(false)
    expect(hook?.body).toContain('trivial read-only option')

    for (const text of [hook?.title, hook?.detail]) {
      expect(text?.toLowerCase()).not.toContain('interrupt')
      expect(text?.toLowerCase()).not.toContain('recover')
      expect(text?.toLowerCase()).not.toContain('stall')
    }
  })

  it('does not attribute a hook continuation to the user', () => {
    // `manual` is the user-pressed Continue row and reads "Continued at your
    // request". A hook continuation is machine-driven, so it must not borrow it.
    const hook = parseRecoveryMessage(`${HOOK}\nKeep going.`)
    const manual = parseRecoveryMessage('[Continue — requested by the user]\nKeep going.')

    expect(hook?.kind).toBe('hook')
    expect(manual?.kind).toBe('manual')
    expect(hook?.title).not.toBe(manual?.title)
  })

  it('surfaces the nudge-cap halt as its own card with the reached depth', () => {
    // The run hit agent.max_stop_hook_nudges; no turn was dispatched. The card
    // reports the halt and carries the depth as a #N chip; the marker line and
    // its count are stripped from the body.
    const halt = parseRecoveryMessage(
      `${HALT} #100\nA Stop hook asked to continue, but this run already took 100 consecutive hook nudges.`,
    )
    expect(halt?.kind).toBe('hook_halted')
    expect(halt?.chip).toBe('#100')
    expect(halt?.title).toBeTruthy()
    expect(halt?.detail).toBeTruthy()
    expect(halt?.body.startsWith('[')).toBe(false)
    expect(halt?.body).not.toContain('#100')
    expect(halt?.body).toContain('already took 100')
  })

  it('labels a promise-only turn (announced an action, never made the call)', () => {
    // Verbatim opener from chat_utils._PROMISE_ONLY_CONTINUE_MSG (#2686).
    const promise = parseRecoveryMessage(
      `${PROMISE_ONLY}\nYour previous turn ended right after you said you would perform an action immediately.`,
    )
    expect(promise?.kind).toBe('promise_only')
    expect(promise?.title).toBe('Action not taken')
    expect(promise?.detail).toBe('announced but not done · continuation sent automatically')
    expect(promise?.chip).toBe('')
    expect(promise?.body.startsWith('[')).toBe(false)
  })

  it('labels a post-compaction continuation without blaming a backend fault', () => {
    // Verbatim opener from chat_utils._COMPACTION_CONTINUE_MSG.
    const compaction = parseRecoveryMessage(
      `${COMPACTION}\nThe conversation above was summarized mid-turn because the context window filled up.`,
    )
    expect(compaction?.kind).toBe('compaction')
    expect(compaction?.title).toBe('Context compacted')
    // Its own copy rather than the posttoken pair: nothing errored and nothing
    // was lost, so a "backend error" detail would send the reader hunting a
    // fault that does not exist.
    expect(compaction?.detail).toBe('summarized mid-turn · continuation sent automatically')
    expect(compaction?.detail).not.toContain('error')
    expect(compaction?.chip).toBe('')
    expect(compaction?.body.startsWith('[')).toBe(false)
  })

  it('does not claim an ordinary mention of compacting', () => {
    // The ACP layer matches the adapter's notice on an exact literal for the
    // same reason: model prose about compaction is not a control frame.
    expect(parseRecoveryMessage('The context was compacted, so here is a summary.')).toBeNull()
    // Hyphen instead of em dash — not the wire value.
    expect(parseRecoveryMessage('[Context compacted - automatic recovery]\nbody')).toBeNull()
  })
})

describe('RecoveryCard', () => {
  const parsed = parseRecoveryMessage(
    refusalBody(['Running: mypy src/…: Blocked by security policy: .*env.*grep.*AWS.*']),
  )!

  it('renders collapsed with the title, detail and pattern chip', () => {
    render(<RecoveryCard parsed={parsed} />)
    expect(screen.getByTestId('recovery-card')).toHaveAttribute('data-kind', 'refusal')
    expect(screen.getByText('Tool call blocked')).toBeInTheDocument()
    expect(screen.getByText('safety policy blocked the call · continuation sent automatically')).toBeInTheDocument()
    expect(screen.getByTestId('recovery-card-chip')).toHaveTextContent('.*env.*grep.*AWS.*')
    // The machine-facing prose is folded away until asked for.
    expect(screen.queryByTestId('recovery-card-body')).toBeNull()
    expect(screen.getByTestId('recovery-card-toggle')).toHaveAttribute('aria-expanded', 'false')
  })

  it('expands to the verbatim injected prompt and collapses again', async () => {
    const user = userEvent.setup()
    render(<RecoveryCard parsed={parsed} />)
    const toggle = screen.getByTestId('recovery-card-toggle')

    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const body = screen.getByTestId('recovery-card-body')
    expect(body).toHaveTextContent('Decide how to proceed')
    expect(body).toHaveTextContent('Blocked:')

    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('recovery-card-body')).toBeNull()
  })

  it('names the toggle with the card content, not a generic label', () => {
    // An aria-label here would REPLACE the accessible name, leaving AT users
    // with "Show recovery details" and no way to learn what was blocked without
    // expanding the raw machine prose. Pin the announced name to the digest.
    render(<RecoveryCard parsed={parsed} />)
    const toggle = screen.getByTestId('recovery-card-toggle')
    expect(toggle).not.toHaveAttribute('aria-label')
    const name = screen.getByRole('button', { name: /Tool call blocked/ })
    expect(name).toBe(toggle)
    expect(toggle).toHaveAccessibleName(/safety policy/)
    expect(toggle).toHaveAccessibleName(/env/)
  })

  it('exposes the toggle as a keyboard-reachable button', async () => {
    const user = userEvent.setup()
    render(<RecoveryCard parsed={parsed} />)
    const toggle = screen.getByRole('button', { name: /Tool call blocked/ })
    await user.tab()
    expect(toggle).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(screen.getByTestId('recovery-card-body')).toBeInTheDocument()
  })

  it('drops the chip for kinds that have no deny pattern', () => {
    const stalled = parseRecoveryMessage(`${STALLED}\nInterrupted by a system stall.`)!
    render(<RecoveryCard parsed={stalled} />)
    expect(screen.queryByTestId('recovery-card-chip')).toBeNull()
    expect(screen.getByTestId('recovery-card')).toHaveAttribute('data-kind', 'stalled')
  })

  it('marks infrastructure hiccups routine and blocks/stalls as needing attention', () => {
    // A deny-pattern block may need the user to act; a transient 5xx or an empty
    // generation is noise the gateway absorbs on its own. The severity split is
    // what keeps the routine case from reading as urgently as the blocked case.
    const { unmount } = render(
      <RecoveryCard parsed={parseRecoveryMessage(`${POSTTOKEN}\nContinue from where it stopped.`)!} />,
    )
    expect(screen.getByTestId('recovery-card')).toHaveAttribute('data-severity', 'routine')
    unmount()

    render(<RecoveryCard parsed={parseRecoveryMessage(`${EMPTY}\nRespond now.`)!} />)
    expect(screen.getByTestId('recovery-card')).toHaveAttribute('data-severity', 'routine')
    expect(screen.getByText('No response returned')).toBeInTheDocument()
  })

  it('marks a busy-session reset routine, like the other transient causes', () => {
    render(<RecoveryCard parsed={parseRecoveryMessage(`${BUSY}\nContinue from where it stopped.`)!} />)
    const card = screen.getByTestId('recovery-card')
    expect(card).toHaveAttribute('data-kind', 'busy')
    expect(card).toHaveAttribute('data-severity', 'routine')
  })

  it('keeps the attention severity for a refusal', () => {
    render(<RecoveryCard parsed={parsed} />)
    expect(screen.getByTestId('recovery-card')).toHaveAttribute('data-severity', 'attention')
  })
})

/**
 * ChatPage's transcript is driven by the custom virtualizer, which mounts an
 * empty window under jsdom (no layout engine), so a full-page render produces no
 * message DOM. These are source-contract assertions in the same style as
 * ChatPage.mcpOAuth.test.tsx: they lock in the wiring, while the card's own
 * behaviour is covered above.
 */
describe('ChatPage – recovery card wiring', () => {
  const src = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../pages/ChatPage.tsx'), 'utf8')

  it('imports the card and its shared resolver', () => {
    expect(src).toMatch(
      /import\s+RecoveryCard\s*,\s*\{\s*resolveInjectCard\s*\}\s*from\s*['"][^'"]*RecoveryCard['"]/,
    )
  })

  it('routes inject rows through the shared resolver to the card', () => {
    expect(src).toMatch(/resolveInjectCard\s*\(\s*m\s*\)/)
    expect(src).toMatch(/<RecoveryCard\s/)
  })

  it('checks for a card BEFORE the generic inject bubble renders', () => {
    // The generic `isInject` branch paints any injected text as a full-width
    // warning bubble. If the resolver check lands after it, the card is dead
    // code and the raw prompt reappears.
    const card = src.indexOf('resolveInjectCard(m)')
    const generic = src.indexOf("const isInject = m.role === 'inject'")
    expect(card).toBeGreaterThanOrEqual(0)
    expect(generic).toBeGreaterThanOrEqual(0)
    expect(card).toBeLessThan(generic)
  })
})

/** Verbatim from SUBAGENT_SYNTHESIS_PREFIX in src/kiro_crew/dashboard/state.py. */
const SYNTHESIS = '[SYSTEM] Sub-agent synthesis:'

describe('parseRecoveryMessage – sub-agent synthesis', () => {
  // Unlike every sibling marker, this one ends in a colon and the instruction
  // continues on the SAME line. Shaped as SUBAGENT_SYNTHESIS_PROMPT emits it.
  const full =
    `${SYNTHESIS} all sub-agents you spawned have completed and each result was processed above. ` +
    'Produce a single consolidated synthesis as your reply for the user: (1) restate the original goal.'

  it('recognises the synthesis marker', () => {
    const p = parseRecoveryMessage(full)
    expect(p?.kind).toBe('synthesis')
  })

  it('names the fan-out rather than reporting an interruption', () => {
    const p = parseRecoveryMessage(full)
    expect(p?.title).toBe('Sub-agents finished')
    // Nothing failed, stalled or was blocked — the copy must not imply otherwise.
    expect(p?.title).not.toMatch(/stall|block|interrupt|error|fail/i)
    expect(p?.detail).not.toMatch(/recover/i)
  })

  it('strips the marker from the body and keeps the instruction', () => {
    const p = parseRecoveryMessage(full)
    expect(p?.body.startsWith('[SYSTEM]')).toBe(false)
    expect(p?.body).toMatch(/^all sub-agents you spawned have completed/)
  })

  it('carries no chip', () => {
    expect(parseRecoveryMessage(full)?.chip).toBe('')
  })

  it('renders collapsed as routine, and expands to the verbatim prompt', async () => {
    const p = parseRecoveryMessage(full)!
    render(<RecoveryCard parsed={p} />)
    const card = screen.getByTestId('recovery-card')
    expect(card).toHaveAttribute('data-kind', 'synthesis')
    // Routine: this is orchestration, not a fault the user must act on.
    expect(card).toHaveAttribute('data-severity', 'routine')
    expect(screen.queryByTestId('recovery-card-body')).toBeNull()
    await userEvent.click(screen.getByTestId('recovery-card-toggle'))
    expect(screen.getByTestId('recovery-card-body').textContent).toMatch(
      /^all sub-agents you spawned have completed/,
    )
  })
})

describe('RecoveryCard – generic system notice', () => {
  // The catch-all the render site constructs for an inject shape this build has
  // no prefix for. parseRecoveryMessage never produces it.
  const parsed = {
    kind: 'generic' as const,
    title: 'System notice',
    detail: 'injected by the gateway',
    chip: '',
    body: '[Some future marker] machine-facing prose',
  }

  it('renders as a folded note, not a bubble', async () => {
    render(<RecoveryCard parsed={parsed} />)
    const card = screen.getByTestId('recovery-card')
    expect(card).toHaveAttribute('data-kind', 'generic')
    expect(card).toHaveAttribute('data-severity', 'routine')
    // Collapsed by default: the prose is folded away, not shown inline.
    expect(screen.queryByTestId('recovery-card-body')).toBeNull()
    await userEvent.click(screen.getByTestId('recovery-card-toggle'))
    expect(screen.getByTestId('recovery-card-body').textContent).toContain('machine-facing prose')
  })
})

describe('resolveInjectCard – which inject rows become notes', () => {
  // Behavioural, not a source scan. The previous guards asserted ChatPage's raw
  // text contained `if (!m.meta?.cronLabel)`, which pinned the MECHANISM rather
  // than the behaviour — and that mechanism was wrong, because an inject row's
  // `cls` (where cronLabel actually lives) is not persisted, so the guard failed
  // on every restored row while the test still passed. Assert outcomes instead.
  const row = (content: string, meta?: Record<string, unknown>) => ({ content, meta })

  it('folds a gateway-stamped synthesis prompt into a note', () => {
    const card = resolveInjectCard(row(`${SYNTHESIS} produce the write-up.`, { injectKind: 'synthesis' }))
    expect(card?.kind).toBe('synthesis')
  })

  it('folds a stamped row whose marker this build does not know', () => {
    const card = resolveInjectCard(row('[Some future marker] machine prose', { injectKind: 'recovery' }))
    expect(card?.kind).toBe('generic')
  })

  it('leaves a cron row to its own labelled bubble, stamped OR restored-legacy', () => {
    // Stamped (durable, survives a flush):
    expect(resolveInjectCard(row('nightly report', { injectKind: 'cron', cronLabel: 'nightly' }))).toBeNull()
    // Legacy live row: cls-derived cronLabel with no injectKind yet.
    expect(resolveInjectCard(row('nightly report', { cronLabel: 'nightly' }))).toBeNull()
  })

  it('leaves a replay of the user OWN words as speech', () => {
    // The case a content-sniffing fallback cannot see: build_recovery_requeue
    // re-queues the user's original message verbatim, with no marker in it.
    expect(resolveInjectCard(row('run the backend gates', { injectKind: 'user_replay' }))).toBeNull()
  })

  it('leaves an UNMARKED row exactly as it rendered before', () => {
    // A row persisted by a gateway older than the field. Folding it away would
    // change history's rendering under the user, so absent provenance must mean
    // "not mine" rather than "machine prose".
    expect(resolveInjectCard(row('ordinary injection'))).toBeNull()
    expect(resolveInjectCard(row('ordinary injection', {}))).toBeNull()
  })

  it('still prefers the content marker, which is durable', () => {
    // Recovery copy is per-kind and no structural tag reproduces it, so a
    // recognised marker wins even when the stamp says something coarser.
    const card = resolveInjectCard(row('[Stalled turn — automatic recovery]\ngo on', { injectKind: 'recovery' }))
    expect(card?.kind).toBe('stalled')
  })
})
