/**
 * Source contract for #5434: ChatPage's CollapsibleToolGroup mounts must not
 * declare the standing-trust tier.
 *
 * Both mounts decide approvals through `lib/decidePermissionRow`, which maps
 * with the shared `toApiDecision` (`utils/approvalDecision.ts`, single-sourced
 * by #8193 — ChatPage held its own copy until then) into the one-shot
 * `api.decideApproval`, which has no trust verb. The group component is fail-closed (`canTrust` opt-in), so the
 * regression this pins is someone flipping `canTrust` on a ChatPage mount: the
 * Trust button would render, and because that mapping is fail-closed
 * (`'approved' -> approve`, else `reject`), a user's Trust click would resolve as
 * a SILENT DENIAL — worse than the silent one-shot approve #5434 removed.
 *
 * Why a source contract and not a render test: these mounts are currently
 * unreachable — `groupDisplayItems` skips `permission` rows entirely (the
 * pinned ApprovalBar owns them) and nothing else is GROUPABLE, so no transcript
 * can mount the group from ChatPage. That latency is exactly why the defect had
 * to be fixed by inspection (#5434), and why the pin must read the source
 * rather than the DOM. The component's own rendering of the decision set is
 * covered behaviorally in CollapsibleToolGroupCov80.test.tsx; the app-sdk
 * threading in ChatMessageList.test.tsx and ChatEmbed.test.tsx.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const source = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf-8')
const decider = readFileSync(resolve(__dirname, '../lib/decidePermissionRow.ts'), 'utf-8')

/** Every `<CollapsibleToolGroup ...>` opening tag's attribute block. */
function mountAttributeBlocks(src: string): string[] {
  const blocks: string[] = []
  const open = /<CollapsibleToolGroup\b/g
  let m: RegExpExecArray | null
  while ((m = open.exec(src))) {
    // The attribute block ends at the first `>` that is not inside a brace
    // expression — track brace depth so `onApprove={(() => {...})()}` and
    // arrow bodies do not end the scan early.
    let depth = 0
    for (let i = m.index; i < src.length; i++) {
      const ch = src[i]
      if (ch === '{') depth++
      else if (ch === '}') depth--
      else if (ch === '>' && depth === 0) {
        blocks.push(src.slice(m.index, i + 1))
        break
      }
    }
  }
  return blocks
}

describe('ChatPage CollapsibleToolGroup mounts (#5434 contract)', () => {
  const mounts = mountAttributeBlocks(source)

  it('finds the mounts (fail-closed: a rename or refactor must re-establish this contract)', () => {
    // Exactly the three known mounts: the transcript row, the pinned panel,
    // and the measure farm's off-screen replica. The farm mount is inert by
    // construction (hasPermission={false}, isRunning={false}, no pending
    // permission rows are ever farm targets' concern — approvals resolve in
    // the transcript mount). If this count changes, re-verify the new mount
    // set's resolve paths and update this contract deliberately.
    expect(mounts).toHaveLength(3)
  })

  it('no mount declares canTrust — their resolve path is the one-shot decideApproval', () => {
    for (const block of mounts) {
      // toApiDecision maps anything but 'approved' to 'reject', so a canTrust
      // mount here would turn a user's Trust click into a silent denial.
      expect(block).not.toMatch(/\bcanTrust\b/)
      // And the mounts stay latent as shipped: hasPermission is the literal
      // false. Flipping it truthy arms the approval row — legitimate, but the
      // author doing so must re-read the toApiDecision constraint comment.
      expect(block).toContain('hasPermission={false}')
    }
  })

  it('a chat decision is bound to the row\'s request and hands its outcome to that request\'s feed row', () => {
    // Both resolving mounts decide the row they show, never a bare id: the id
    // recurs and a chat runner's can collide with a coordinator one.
    const calls = source.match(/return \(action: string\) => decideRow\(meta, action\)/g) ?? []
    expect(calls).toHaveLength(2)
    expect(source).not.toMatch(/api\.resolveApproval\(/)
    expect(decider).toMatch(/const target = permissionRowTarget\(meta, slot\)/)
    expect(decider).toMatch(/await api\.decideApproval\(target, a\)/)
    // The chat row records the decision that was sent: a missing one defaults
    // to `approved` and would overwrite a rejection the backend frame wrote.
    expect(decider).toMatch(/dispatch\(resolveApprovalRow\(\{ target, decision \}\)\)/)
    // And the feed row for THAT request reads Approved/Rejected, not expired.
    expect(decider).toMatch(/approvalRowsFor\(notifications, target\)/)
    expect(decider).toMatch(/settleDecidedApproval\(n\.ts, a === 'approve' \? 'approve' : 'reject'\)/)
  })
})
