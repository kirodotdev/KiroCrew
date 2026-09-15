/**
 * Guards ChatPage's crew-bound offer gates (PR 10617, FIX 1).
 *
 * A crew-bound (remote-executor) session refuses every LOCAL turn-starting
 * action server-side: `remote_bound_refusal` in
 * `src/kiro_crew/dashboard/remote_relay.py` answers 409
 * `remote_action_unsupported` for regenerate, edit-resend, rewind and continue,
 * because each would run the crew's turn on THIS machine and diverge the
 * transcripts. Continue was already gated client-side (`selectContinuable`);
 * regenerate and edit-resend were NOT, so ChatPage offered controls the server
 * is guaranteed to reject.
 *
 * This is a SOURCE-CONTRACT test, matching the existing ChatPage convention
 * (see ChatPage.reachability.test.tsx): ChatPage's message list is driven by a
 * custom virtualizer that mounts an empty window under jsdom, so a full-page
 * render cannot exercise these offers. What regressed — and what must not come
 * back — is precisely the gate SHAPE: which condition each offer is guarded on.
 * That is what this locks in, and removing the guard from any one site fails the
 * matching assertion (the mutation check).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const chatPageSrc = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
const sidebarSrc = readFileSync(resolve(here, '../pages/ChatSidebar.tsx'), 'utf8')

describe('ChatPage – crew-bound offer gates', () => {
  it('derives the crew-bound flag from the shared predicate on the active slot', () => {
    // ONE spelling of "crew-bound", shared with selectContinuable, so the two
    // surfaces cannot key on different fields.
    expect(chatPageSrc).toMatch(/import \{[^}]*\bslotIsRemoteBound\b[^}]*\} from '\.\.\/store\/dashboardSlice'/)
    expect(chatPageSrc).toMatch(/const activeSlotRemoteBound = slotIsRemoteBound\(/)
  })

  it('gates handleRegenerate on the crew-bound flag (early return)', () => {
    expect(chatPageSrc).toMatch(/if \(!activeSlot \|\| regenerating \|\| slotRunning \|\| activeSlotRemoteBound\) return/)
  })

  it('gates handleEditResend on the crew-bound flag (early return)', () => {
    expect(chatPageSrc).toMatch(/if \(!activeSlot \|\| slotRunning \|\| activeSlotRemoteBound\) return/)
  })

  it('withholds the Edit affordance (canEdit) on a crew-bound slot', () => {
    expect(chatPageSrc).toMatch(/canEdit=\{!slotRunning && !regenerating && !!activeSlot && !activeSlotRemoteBound\}/)
  })

  it('withholds the Regenerate offer on a crew-bound slot', () => {
    expect(chatPageSrc).toMatch(/onRegenerate=\{i === lastTextIdxRef\.current && !slotRunning && !regenerating && activeSlot && !activeSlotRemoteBound \? handleRegenerate : undefined\}/)
  })

  it('does NOT gate onSwitchVariant — the server allows switch-variant on a bound slot (no remote_bound_refusal)', () => {
    // Deliberate: `api_chat_slot_switch_variant` carries no remote_bound_refusal,
    // so gating it here would hide a control the server accepts. This pins the
    // ungated shape; if a future edit inserts the crew-bound guard before the
    // `?`, this exact condition no longer matches and the choice is revisited.
    expect(chatPageSrc).toMatch(/onSwitchVariant=\{i === lastTextIdxRef\.current && m\.variants && m\.variants\.length > 1 && activeSlot \?/)
    // And the switch-variant offer must not carry the crew-bound flag.
    const switchVariant = chatPageSrc.slice(
      chatPageSrc.indexOf('onSwitchVariant={'),
      chatPageSrc.indexOf('api.switchVariant('),
    )
    expect(switchVariant).not.toContain('activeSlotRemoteBound')
  })
})

describe('ChatSidebar – the interrupted row shares the composer’s predicate', () => {
  // The sidebar row and the composer gate answer the SAME question ("will the
  // server refuse a local turn on this slot?"), so they must not drift apart.
  // A second hand-spelled `executor === 'remote'` here is exactly the
  // per-call-site re-implementation this PR exists to remove, and it is what the
  // review caught: the row was gated inline while the predicate sat one file away.
  it('builds the interrupted label from slotIsRemoteBound, not an inline field read', () => {
    expect(sidebarSrc).toMatch(/import \{[^}]*\bslotIsRemoteBound\b[^}]*\} from '\.\.\/store\/dashboardSlice'/)
    expect(sidebarSrc).toMatch(/const label = slotIsRemoteBound\(s\)/)
  })

  it('leaves no inline crew-bound spelling in the interrupted row builder', () => {
    // Scoped to the row builder, because the crew CHIP legitimately reads
    // `executor === 'remote'` (paired with instance_id) to answer a different
    // question: which crew the row runs on, not whether an action is refused.
    const start = sidebarSrc.indexOf("key: 'interrupted'")
    expect(start).toBeGreaterThan(-1)
    const builder = sidebarSrc.slice(start, sidebarSrc.indexOf('},', sidebarSrc.indexOf('return (', start)))
    expect(builder).not.toMatch(/s\.executor === 'remote'/)
  })
})
