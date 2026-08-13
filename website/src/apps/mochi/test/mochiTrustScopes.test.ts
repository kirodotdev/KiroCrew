/**
 * Scoped trust grants + the approval bubble.
 *
 * These are the two things a regression here would break QUIETLY, which is why
 * they are pinned:
 *
 *  1. The GRANT SCOPE. A trust click widens what runs unasked, and the `pattern`
 *     string decides by how much. The pet panel must produce EXACTLY what the
 *     dashboard's TrustDropdown produces for the same click; until that transform
 *     is hoisted into a shared module (its own core-scoped PR — see
 *     src/shared/trustPatterns.ts), these exact-output assertions are what keep
 *     the two from drifting and the pet from granting wider than its label says.
 *  2. The BUBBLE COPY. It must name the agent's purpose and NOTHING about the
 *     command: a bubble sits on the desktop in front of anyone looking at the
 *     screen. A future edit that interpolates the command there would be a leak,
 *     not a cosmetic change.
 */
import { describe, expect, it } from 'vitest'

import {
  familyGrantIsDistinct,
  trustBasePattern,
  truncateCommandLabel,
} from '../src/shared/trustPatterns'
import { permissionApprovalFromFrame } from '../panel/panelBridge'
import { approvalBubbleText, approvalPurpose } from '../src/renderer/hooks/useApprovalBubble'
// The dashboard copy, imported ONLY so the parity test can compare the two
// implementations directly rather than restating one of them as literals.
import { truncateCommandLabel as dashboardTruncateCommandLabel } from '../../../utils/trustPatterns'

/** A permission-role chat frame, as the gateway broadcasts it. */
function frame(meta: Record<string, unknown>): Record<string, unknown> {
  return { slot: 'mochi', role: 'permission', content: 'x', cls: JSON.stringify(meta) }
}

describe('trust pattern transform (shared with the dashboard)', () => {
  it('trusts one command with any arguments', () => {
    expect(trustBasePattern('npm')).toBe('npm *')
  })

  it('covers every segment of a piped or chained command', () => {
    // The gateway sends "cat,wc" for `cat f | wc -l`; a grant that only covered
    // the first segment would leave the turn blocked on the second.
    expect(trustBasePattern('cat,wc')).toBe('cat *,wc *')
  })

  it('tolerates the spacing the gateway may emit', () => {
    expect(trustBasePattern('cat, wc')).toBe('cat *,wc *')
  })

  it('truncates only the LABEL, never the pattern', () => {
    const long = 'a'.repeat(80)
    expect(truncateCommandLabel(long)).toHaveLength(64)
    expect(trustBasePattern(long)).toBe(long + ' *')
  })

  it('is byte-identical to the dashboard implementation', () => {
    // Stronger than mirroring a constant by hand: this compares the two copies
    // directly, so a divergent BUDGET or a divergent ALGORITHM both fail here.
    // The previous version pinned literals, which meant the dashboard moving to
    // middle-ellipsis left this copy silently head-truncating -- the same
    // collision on one surface only (see #4436).
    const cases = [
      'a'.repeat(64),
      'a'.repeat(65),
      'gh api repos/owner/some-repository/contents/config.json --jq .sha',
      'gh api repos/rapid7-security-platform-team/ai-vault-service/contents/config.json --jq .sha',
      'gh api repos/rapid7-security-platform-team/ai-vault-service/contents/secrets.json --jq .sha',
      // These two STRADDLE the cut points at the default budget, so the parity
      // comparison actually exercises the surrogate snapping. At max=64 the cuts
      // are head=42 and tailStart=length-21; an emoji merely sitting in the
      // elided middle (as the first version of this case did) never snaps, and
      // the test would have stayed green while the two copies' snap arithmetic
      // diverged -- the exact behaviour this PR added.
      `${'a'.repeat(41)}😀${'b'.repeat(40)}`,
      `${'a'.repeat(61)}😀${'b'.repeat(20)}`,
      `gh api ${'a'.repeat(40)}😀${'b'.repeat(40)}`,
      'short',
      '',
    ]
    for (const cmd of cases) {
      expect(truncateCommandLabel(cmd)).toBe(dashboardTruncateCommandLabel(cmd))
    }
    for (const max of [2, 4, 8, 24, 30, 64, 200]) {
      const cmd = 'gh api repos/owner/some-repository/contents/secrets.json --jq .sha'
      expect(truncateCommandLabel(cmd, max)).toBe(dashboardTruncateCommandLabel(cmd, max))
    }
  })

  it('does not render two commands from the original report with the same label', () => {
    // The label is the only thing the user reads before granting an exact-string
    // match. These two differ only in the filename, past the old 30-char budget,
    // and rendered identically before the fix. Asserting inequality (not just the
    // new number) catches a build that reintroduces the collision at any length
    // shorter than where these diverge.
    const config = 'gh api repos/owner/some-repository/contents/config.json --jq .sha'
    const secrets = 'gh api repos/owner/some-repository/contents/secrets.json --jq .sha'
    expect(truncateCommandLabel(config)).not.toBe(truncateCommandLabel(secrets))
    // ...and pin that the OLD budget is what made them collide, so this test
    // fails if the budget is narrowed back.
    expect(truncateCommandLabel(config, 30)).toBe(truncateCommandLabel(secrets, 30))
  })

  it('offers the family grant only when it differs from the exact command', () => {
    // Shell command with arguments — family grant is meaningfully wider.
    expect(familyGrantIsDistinct('npm test', 'npm')).toBe(true)
    // Plain MCP tool call — base equals full, so a family grant would duplicate
    // the command grant and is hidden instead.
    expect(familyGrantIsDistinct('SomeMcpTool', 'SomeMcpTool')).toBe(false)
    expect(familyGrantIsDistinct('ls', undefined)).toBe(false)
  })
})

describe('permissionApprovalFromFrame carries the scope fields', () => {
  it('keeps the gateway-computed pattern inputs', () => {
    const req = permissionApprovalFromFrame(
      frame({
        request_id: 'r1',
        tool_title: 'Running: npm test',
        full_command: 'npm test',
        base_command: 'npm',
      }),
    )
    expect(req).not.toBeNull()
    expect(req?.fullCommand).toBe('npm test')
    expect(req?.baseCommand).toBe('npm')
  })

  it('leaves them undefined when the gateway sent none, so the card degrades to the single grant', () => {
    const req = permissionApprovalFromFrame(frame({ request_id: 'r1', tool_title: 'T' }))
    expect(req?.fullCommand).toBeUndefined()
    expect(req?.baseCommand).toBeUndefined()
  })

  it('extracts the agent-declared purpose from the tool arguments', () => {
    const req = permissionApprovalFromFrame(
      frame({
        request_id: 'r1',
        tool_title: 'T',
        tool_input: JSON.stringify({ __tool_use_purpose: '  Walk around the screen  ', action: 'move' }),
      }),
    )
    expect(req?.purpose).toBe('Walk around the screen')
  })

  it('extracts the purpose under a model-paraphrased key spelling', () => {
    const req = permissionApprovalFromFrame(
      frame({
        request_id: 'r1',
        tool_title: 'T',
        tool_input: JSON.stringify({ __purpose: 'Walk around the screen', action: 'move' }),
      }),
    )
    expect(req?.purpose).toBe('Walk around the screen')
  })

  it('has no purpose when the arguments are not JSON', () => {
    const req = permissionApprovalFromFrame(
      frame({ request_id: 'r1', tool_title: 'T', tool_input: 'rm -rf build' }),
    )
    expect(req?.purpose).toBeUndefined()
  })
})

describe('approval bubble copy', () => {
  it('reads the purpose from either approval shape', () => {
    // Interactive tool call.
    expect(approvalPurpose({ purpose: 'Deploy the thing' })).toBe('Deploy the thing')
    // The gateway's own approval frame (Slack / background sources).
    expect(approvalPurpose({ tool_purpose: 'Run the nightly' })).toBe('Run the nightly')
    expect(approvalPurpose(undefined)).toBe('')
    expect(approvalPurpose({})).toBe('')
  })

  it('names the pet and the purpose', () => {
    const text = approvalBubbleText('Mimi', 'Walk around the screen')
    expect(text).toContain('Mimi')
    expect(text).toContain('Walk around the screen')
  })

  it('falls back to a purpose-less sentence rather than an empty slot', () => {
    // A missing purpose used to render "approval for " with nothing after it.
    const text = approvalBubbleText('Mimi', '   ')
    expect(text).toContain('Mimi')
    expect(text).not.toMatch(/\bfor\s*$/)
  })

  it('never puts the command in the bubble', () => {
    // The bubble is desktop-visible; the exact command belongs on the card only.
    const text = approvalBubbleText('Mimi', 'Clean the build output')
    expect(text).not.toContain('rm -rf')
  })
})
