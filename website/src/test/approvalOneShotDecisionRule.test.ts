/**
 * The gate that guards the chokepoint.
 *
 * `eslint-rules/approval-one-shot-decision.js` is the only layer that sees a
 * trust verb BEFORE the mapping erases it: an inline
 * `action === 'rejected' ? 'reject' : 'approve'` converts the verb into
 * `approve` upstream of both the typed `api.resolveApproval` client and the
 * backend's 400, so no runtime guard downstream can catch the class that
 * shipped three times (#5400, #5434, #5486).
 *
 * WHY THESE TESTS LEAD WITH VIOLATIONS: a suite that only lints the clean tree
 * passes when the rule works, when the rule is broken, when it is unwired from
 * `eslint.config.js`, and when it does not run at all — four states with one
 * green. So every test here asserts on a fixture that VIOLATES the rule and
 * requires the rule to flag it, linting through the REAL config exactly as
 * `npm run lint` does (the same outside-in approach as
 * `i18nStrictRule.test.ts`). The clean-tree direction is asserted too, but only
 * as the negative control.
 */
import { describe, it, expect } from 'vitest'
import { ESLint } from 'eslint'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const WEBSITE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const RULE_ID = 'approval-one-shot/no-inline-one-shot-decision'

/** Lint one snippet through the real `eslint.config.js`, as `npm run lint` does. */
async function lint(code: string): Promise<ESLint.LintResult['messages']> {
  const engine = new ESLint({
    cwd: WEBSITE_ROOT,
    overrideConfigFile: 'eslint.config.js',
    // A snippet must not be able to quietly disable the rule it is testing.
    allowInlineConfig: false,
  })
  const [result] = await engine.lintText(code, { filePath: path.join(WEBSITE_ROOT, 'src/probe.ts') })
  return result.messages
}

/** Only this rule's reports — other rules firing on a snippet is not the subject. */
function ruleMessages(messages: ESLint.LintResult['messages']) {
  return messages.filter(m => m.ruleId === RULE_ID)
}

describe('approval-one-shot/no-inline-one-shot-decision', () => {
  it('flags the exact ternary shape all three regressions took', async () => {
    // Verbatim shape of the defect: a trust verb in `action` becomes 'approve'.
    const hits = ruleMessages(await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject' | 'reject_once') => Promise<unknown> }
      export async function go(id: string, action: string) {
        await api.resolveApproval(id, action === 'rejected' ? 'reject' : 'approve')
      }
    `))
    expect(hits).toHaveLength(1)
    expect(hits[0].message).toContain('toApiDecision')
    expect(hits[0].severity).toBe(2)
  })

  it('flags the inverted spelling too — the rule is about the shape, not the operand order', async () => {
    const hits = ruleMessages(await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject' | 'reject_once') => Promise<unknown> }
      export async function go(id: string, action: string) {
        await api.resolveApproval(id, action === 'approved' ? 'approve' : 'reject')
      }
    `))
    expect(hits).toHaveLength(1)
  })

  it('flags a logical-operator default, which fails open the same way', async () => {
    const hits = ruleMessages(await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject' | 'reject_once') => Promise<unknown> }
      export async function go(id: string, action?: string) {
        await api.resolveApproval(id, (action as 'approve' | 'reject') || 'approve')
      }
    `))
    expect(hits).toHaveLength(1)
  })

  it('flags a bare call, not only a member call', async () => {
    const hits = ruleMessages(await lint(`
      declare function resolveApproval(id: string, action: 'approve' | 'reject'): Promise<unknown>
      export async function go(id: string, action: string) {
        await resolveApproval(id, action === 'rejected' ? 'reject' : 'approve')
      }
    `))
    expect(hits).toHaveLength(1)
  })

  it('flags a private mapping helper -- the shape #5486 actually shipped', async () => {
    // The re-introduction path that matters most historically. #5400, #5434 and
    // #5486 were each a module-private mapper, not an inline ternary at the
    // call site, so a rule that only rejected ternaries would have caught none
    // of the three defects it cites.
    const hits = ruleMessages(await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject') => Promise<unknown> }
      function myMap(d: string): 'approve' | 'reject' { return d === 'rejected' ? 'reject' : 'approve' }
      export async function go(id: string, action: string) {
        await api.resolveApproval(id, myMap(action))
      }
    `))
    expect(hits).toHaveLength(1)
  })

  it('flags a ternary hoisted into a local, which moves the shape one line up', async () => {
    const hits = ruleMessages(await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject') => Promise<unknown> }
      export async function go(id: string, action: string) {
        const decision = action === 'rejected' ? 'reject' : 'approve'
        await api.resolveApproval(id, decision)
      }
    `))
    expect(hits).toHaveLength(1)
  })

  it('flags a logical-operator default hoisted into a local', async () => {
    const hits = ruleMessages(await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject') => Promise<unknown> }
      export async function go(id: string, action?: string) {
        const decision = (action as 'approve' | 'reject') || 'approve'
        await api.resolveApproval(id, decision)
      }
    `))
    expect(hits).toHaveLength(1)
  })

  it('flags an unrecognized argument shape rather than passing it', async () => {
    // Fail-closed on shapes nobody enumerated. Every resolveApproval argument
    // in the repo today is a literal, a plain identifier, or a toApiDecision
    // call; a fourth shape must be judged deliberately, not admitted silently.
    const hits = ruleMessages(await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject') => Promise<unknown> }
      declare const table: Record<string, 'approve' | 'reject'>
      export async function go(id: string, action: string) {
        await api.resolveApproval(id, table[action])
      }
    `))
    expect(hits).toHaveLength(1)
  })

  it('flags a private mapper that SHADOWS the shared name -- literally #5486', async () => {
    // The rule must check the BINDING, not the spelling. #5486's defect was a
    // module-private function called exactly `toApiDecision`, so a name-only
    // check waves through the one shape the rule exists to stop.
    const hits = ruleMessages(await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject') => Promise<unknown> }
      function toApiDecision(d: string): 'approve' | 'reject' { return d === 'rejected' ? 'reject' : 'approve' }
      export async function go(id: string, action: string) {
        await api.resolveApproval(id, toApiDecision(action))
      }
    `))
    expect(hits).toHaveLength(1)
  })

  it('flags a local arrow mapper declared under the shared name', async () => {
    const hits = ruleMessages(await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject') => Promise<unknown> }
      const toApiDecision = (d: string) => (d === 'rejected' ? 'reject' : 'approve') as 'approve' | 'reject'
      export async function go(id: string, action: string) {
        await api.resolveApproval(id, toApiDecision(action))
      }
    `))
    expect(hits).toHaveLength(1)
  })

  it('flags a method call by that name on an arbitrary object', async () => {
    // `.toApiDecision` has zero occurrences in tree, so admitting any object's
    // method by that name only widens the guard for no consumer.
    const hits = ruleMessages(await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject') => Promise<unknown> }
      declare const anything: { toApiDecision: (d: string) => 'approve' | 'reject' }
      export async function go(id: string, action: string) {
        await api.resolveApproval(id, anything.toApiDecision(action))
      }
    `))
    expect(hits).toHaveLength(1)
  })

  it('is wired into the config CI runs — an unregistered rule id would not report', async () => {
    // Guards the unwiring failure mode specifically: if the plugin were dropped
    // from eslint.config.js, every fixture above would go quiet and read green.
    const messages = await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject') => Promise<unknown> }
      export async function go(id: string, action: string) {
        await api.resolveApproval(id, action === 'rejected' ? 'reject' : 'approve')
      }
    `)
    expect(messages.map(m => m.ruleId)).toContain(RULE_ID)
  })

  /* ── Negative controls: the rule must NOT fire on the compliant shapes ── */

  it('accepts a call through the shared mapping', async () => {
    const hits = ruleMessages(await lint(`
      import { toApiDecision } from './utils/approvalDecision'
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject' | 'reject_once') => Promise<unknown> }
      export async function go(id: string, action: string) {
        await api.resolveApproval(id, toApiDecision(action))
      }
    `))
    expect(hits).toHaveLength(0)
  })

  it('accepts a string literal and a pre-narrowed identifier', async () => {
    const hits = ruleMessages(await lint(`
      declare const api: { resolveApproval: (id: string, action: 'approve' | 'reject') => Promise<unknown> }
      export async function literal(id: string) { await api.resolveApproval(id, 'approve') }
      export async function narrowed(id: string, action: 'approve' | 'reject') { await api.resolveApproval(id, action) }
    `))
    expect(hits).toHaveLength(0)
  })

  it('ignores a ternary passed to some other method', async () => {
    const hits = ruleMessages(await lint(`
      declare const api: { approveChatSlot: (slot: string, action: string) => Promise<unknown> }
      export async function go(slot: string, action: string) {
        await api.approveChatSlot(slot, action === 'rejected' ? 'rejected' : 'trust')
      }
    `))
    expect(hits).toHaveLength(0)
  })
})
