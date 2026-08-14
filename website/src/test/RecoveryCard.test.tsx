import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import RecoveryCard, { parseRecoveryMessage } from '../pages/chat/RecoveryCard'

// Verbatim prefixes from src/kiro_crew/dashboard/state.py. The separator is an
// em dash, not a hyphen — a mismatch is exactly the drift this suite guards.
const REFUSAL = '[Tool refusal — automatic recovery]'
const STALLED = '[Stalled turn — automatic recovery]'
const TOOL_STALL = '[Tool stall — automatic recovery]'
const CONNECTION = '[Connection lost — automatic recovery]'
const POSTTOKEN = '[Interrupted turn — automatic recovery]'
const EMPTY = '[Empty response — automatic recovery]'
const BUSY = '[Session busy — automatic recovery]'

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
    expect(p?.detail).toBe('safety policy · continuing automatically')
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
    expect(connection?.detail).toBe('backend error · continuing automatically')
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
    expect(busy?.detail).toBe('session busy · continuing automatically')
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
    expect(interrupted?.detail).toBe('backend error · continuing automatically')
    expect(interrupted?.chip).toBe('')
    expect(interrupted?.body.startsWith('[')).toBe(false)

    const empty = parseRecoveryMessage(
      `${EMPTY}\nYour previous turn produced no output (the model returned an empty response twice).`,
    )
    expect(empty?.kind).toBe('empty')
    expect(empty?.title).toBe('No response returned')
    expect(empty?.detail).toBe('empty output · continuing automatically')
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
    expect(screen.getByText('safety policy · continuing automatically')).toBeInTheDocument()
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

  it('imports the card and its parser', () => {
    expect(src).toMatch(
      /import\s+RecoveryCard\s*,\s*\{\s*parseRecoveryMessage\s*\}\s*from\s*['"][^'"]*RecoveryCard['"]/,
    )
  })

  it('routes recovery inject rows to the card', () => {
    expect(src).toMatch(/parseRecoveryMessage\s*\(\s*m\.content\s*\)/)
    expect(src).toMatch(/<RecoveryCard\s/)
  })

  it('checks for a recovery row BEFORE the generic inject bubble renders', () => {
    // The generic `isInject` branch paints any injected text as a full-width
    // warning bubble. If the recovery check lands after it, the card is dead
    // code and the raw prompt reappears.
    const card = src.indexOf('parseRecoveryMessage(m.content)')
    const generic = src.indexOf("const isInject = m.role === 'inject'")
    expect(card).toBeGreaterThanOrEqual(0)
    expect(generic).toBeGreaterThanOrEqual(0)
    expect(card).toBeLessThan(generic)
  })
})
