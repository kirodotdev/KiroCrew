import { describe, it, expect } from 'vitest'
import { parseRecoveryMessage } from '../pages/chat/RecoveryCard'
import { turnHadPolicyBlock } from '../app-sdk/turnPolicyBlock'
import type { ChatMessage } from '../types'

const PREFIX = '[Tool blocked — reason sent to the agent]'
const BODY =
  '[Kiro Crew policy notice] The tool call you just made was blocked by a Kiro Crew ' +
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
