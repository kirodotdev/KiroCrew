import { describe, it, expect } from 'vitest'
import { parseRecoveryMessage } from '../pages/chat/RecoveryCard'
import { turnHadPolicyBlock } from '../app-sdk/turnPolicyBlock'
import type { ChatMessage } from '../types'

const PREFIX = '[Tool blocked — reason sent to the agent]'
const BODY =
  '[Kiro Crew host notice] The tool call you just made was blocked by a Kiro Crew ' +
  'safety policy.\n\nBlocked: Running: bash -c x: Blocked by security policy: deny-rule\n'

const msg = (role: string, content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', meta } as unknown as ChatMessage)

describe('the in-band tool-blocked card', () => {
  it('is recognised as its own kind, not as a recovery', () => {
    const parsed = parseRecoveryMessage(`${PREFIX}\n${BODY}`)
    expect(parsed?.kind).toBe('tool_blocked')
  })

  it('never renders the marker itself', () => {
    const parsed = parseRecoveryMessage(`${PREFIX}\n${BODY}`)
    expect(parsed?.body.startsWith(PREFIX)).toBe(false)
    expect(parsed?.title).not.toContain('[Tool blocked')
  })

  // The gateway writes `f"{PREFIX} {cause}\n{notice}"` (chat_runner.py), so the
  // cause token rides the MARKER line. It is read for the summary wording and
  // must not also appear as the body's first line.
  it.each(['policy', 'invalid_name', 'hook_error'])(
    'never leaves the %s wire token at the top of the expanded body',
    cause => {
      const parsed = parseRecoveryMessage(`${PREFIX} ${cause}\n${BODY}`)
      expect(parsed?.body.startsWith(cause)).toBe(false)
      expect(parsed?.body).toBe(BODY.trim())
    }
  )

  it('still keys the summary on the cause it stripped', () => {
    // Stripping the line must not cost the wording it selects -- the two read
    // the same marker line, so a fix that dropped it too early would silently
    // fall back to the policy default for all three causes.
    const invalid = parseRecoveryMessage(`${PREFIX} invalid_name\n${BODY}`)
    const policy = parseRecoveryMessage(`${PREFIX} policy\n${BODY}`)
    expect(invalid?.detail).not.toBe(policy?.detail)
  })

  it('keeps a pre-cause row readable', () => {
    // Rows written before the cause was added are `PREFIX\n<notice>`: an empty
    // marker line. Dropping it is what the old generic trim already did.
    expect(parseRecoveryMessage(`${PREFIX}\n${BODY}`)?.body).toBe(BODY.trim())
  })

  it('reuses the deny-pattern chip', () => {
    // The card already knew how to pull a pattern out of a refusal body; the
    // in-band notice carries the same marker, so the chip comes for free.
    expect(parseRecoveryMessage(`${PREFIX}\n${BODY}`)?.chip).toBe('deny-rule')
  })

  it('does not claim a continuation was sent', () => {
    // The whole point of the in-band path is that no second turn happened;
    // borrowing the recovery copy would describe a turn that never ran.
    const parsed = parseRecoveryMessage(`${PREFIX}\n${BODY}`)
    expect(parsed?.detail).not.toMatch(/continuation/i)
  })

  it('counts only the blocked items, not the guidance the gateway appends', () => {
    // `build_refusal_recovery_prompt` (dashboard/state.py) appends per-class
    // remediation under a "How to do this properly:" heading. The blocked-item
    // count comes from BULLET_RE over the WHOLE body, so guidance rendered as a
    // `  - ` bullet would be counted as a second blocked tool call. It is
    // indented plain prose for exactly that reason — this pins the shape.
    const withGuidance =
      '[Tool refusal — automatic recovery]\nBlocked:\n' +
      '  - bash: Blocked: command accesses sensitive credential path\n' +
      '\nHow to do this properly:\n' +
      '    You do not need to read AWS credential material. Run the command you wanted.\n'
    const parsed = parseRecoveryMessage(withGuidance)
    expect(parsed?.kind).toBe('refusal')
    expect(parsed?.body).toContain('How to do this properly:')
    // One blocked call, so the title must not read as a multi-block summary.
    expect(parsed?.title).not.toMatch(/2/)
  })

  it('is distinct from the recovery refusal kind', () => {
    const recovery = parseRecoveryMessage(
      '[Tool refusal — automatic recovery]\nBlocked:\n  - bash: Blocked by security policy: r'
    )
    expect(recovery?.kind).toBe('refusal')
    expect(recovery?.detail).not.toBe(parseRecoveryMessage(`${PREFIX}\n${BODY}`)?.detail)
  })
})

describe('turnHadPolicyBlock', () => {
  it('finds the notice earlier in the same turn', () => {
    const rows = [msg('user', 'do it'), msg('inject', `${PREFIX}\n${BODY}`), msg('assistant', 'x')]
    expect(turnHadPolicyBlock(rows, 2)).toBe(true)
  })

  it('does not reach back into an earlier turn', () => {
    // A block in a PREVIOUS turn must not silence this turn's chip.
    const rows = [
      msg('user', 'first'),
      msg('inject', `${PREFIX}\n${BODY}`),
      msg('assistant', 'a'),
      msg('user', 'second'),
      msg('assistant', 'b'),
    ]
    expect(turnHadPolicyBlock(rows, 4)).toBe(false)
  })

  it('keeps the chip when the person also steered this turn', () => {
    // Their steer earned its acknowledgement; suppressing on the notice alone
    // would swallow it. Ordered as it actually happens: the person steers, and a
    // later call in the SAME turn is then blocked — so the scan meets the notice
    // first and the steer second, which is the only ordering that proves the
    // steer row is honoured rather than merely ending the scan.
    const rows = [
      msg('user', 'do it'),
      msg('user', 'actually, also check X', { steer: true }),
      msg('inject', `${PREFIX}\n${BODY}`),
      msg('assistant', 'x'),
    ]
    expect(turnHadPolicyBlock(rows, 3)).toBe(false)
  })

  it('is false for a turn with no block', () => {
    expect(turnHadPolicyBlock([msg('user', 'hi'), msg('assistant', 'yo')], 1)).toBe(false)
  })

  it('ignores an unrelated inject row', () => {
    const rows = [
      msg('user', 'do it'),
      msg('inject', '[Stalled turn — automatic recovery]\ncontinue'),
      msg('assistant', 'x'),
    ]
    expect(turnHadPolicyBlock(rows, 2)).toBe(false)
  })
})


describe('the card names the real cause, not always "policy"', () => {
  // The detail is what the reader sees WITHOUT expanding. Before the cause was
  // carried, every cause rendered "safety policy blocked the call" — so an
  // invalid tool name or a faulted hook sent them to audit a security rule that
  // does not exist, while the true cause sat in the collapsed body.
  const row = (cause: string) => `${PREFIX} ${cause}\n${BODY}`

  it('keys the detail on the cause the marker line carries', () => {
    const invalid = parseRecoveryMessage(row('invalid_name'))
    const hook = parseRecoveryMessage(row('hook_error'))
    const policy = parseRecoveryMessage(row('policy'))
    expect(invalid?.kind).toBe('tool_blocked')
    expect(invalid?.detail).not.toBe(policy?.detail)
    expect(hook?.detail).not.toBe(policy?.detail)
    expect(invalid?.detail).not.toBe(hook?.detail)
    // Neither new cause may claim a policy verdict — that is the whole finding.
    expect(invalid?.detail?.toLowerCase()).not.toContain('policy')
    expect(hook?.detail?.toLowerCase()).not.toContain('policy')
  })

  it('falls back to the policy wording when the cause is absent or unknown', () => {
    // Matches the backend's own cause default: a wrong noun is recoverable,
    // rendering a raw key or an empty summary is not.
    const policy = parseRecoveryMessage(row('policy'))
    for (const c of ['', 'not-a-cause']) {
      expect(parseRecoveryMessage(row(c))?.detail).toBe(policy?.detail)
    }
  })

  it('always resolves through i18n, never leaks a raw key', () => {
    for (const c of ['policy', 'invalid_name', 'hook_error']) {
      const d = parseRecoveryMessage(row(c))?.detail ?? ''
      expect(d).not.toContain('recoveryCard.')
      expect(d.length).toBeGreaterThan(0)
    }
  })
})
