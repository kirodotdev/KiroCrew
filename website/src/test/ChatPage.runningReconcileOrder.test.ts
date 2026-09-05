import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * Arriving at a chat with `?autoSend=1` on a slot the server already has running
 * must not draw an optimistic user bubble: the backend answers that send with a
 * `queued` row instead, so a bubble drawn locally leaves the same message in the
 * transcript twice.
 *
 * What decides that is a LIVE store read inside `send` —
 * `selectComposerBusy(store.getState(), slot)` — and the value it reads,
 * `state.chat.slotRunning`, is written by the page's own
 * `syncSlotRunningFromServer` effect. Effects run in registration order within a
 * commit, so on the racing commit (`slots` populated, `connected` true,
 * auto-send armed, `slotRunning` still false) the reconcile effect has to have
 * dispatched BEFORE the actions controller's auto-send effect calls `send`.
 *
 * Asserted against source POSITION because that is what the invariant is: hook
 * registration order is a property of where the calls sit in the function body,
 * and it survives no runtime assertion that does not also reproduce the exact
 * commit boundary. Every link in the chain is pinned, not just the order, so a
 * change that makes the ordering stop mattering fails here and asks for this
 * file to be re-reasoned rather than passing vacuously.
 */
const CHAT_PAGE = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf-8')
const ACTIONS_CONTROLLER = readFileSync(
  join(__dirname, '..', 'pages', 'chat', 'useChatPageActionsController.ts'), 'utf-8',
)
const CHAT_SLICE = readFileSync(join(__dirname, '..', 'store', 'chatSlice.ts'), 'utf-8')

describe('running-state reconcile is registered before auto-send can read it', () => {
  it('dispatches syncSlotRunningFromServer above the actions controller call', () => {
    const reconcile = CHAT_PAGE.indexOf('dispatch(syncSlotRunningFromServer(')
    const actions = CHAT_PAGE.indexOf('const actions = useChatPageActionsController')

    expect(reconcile, 'syncSlotRunningFromServer dispatch not found in ChatPage.tsx').toBeGreaterThan(-1)
    expect(actions, 'actions controller call not found in ChatPage.tsx').toBeGreaterThan(-1)
    expect(reconcile).toBeLessThan(actions)
  })

  it('reconciles in exactly one place, so no later copy can win the commit', () => {
    const hits = CHAT_PAGE.split('dispatch(syncSlotRunningFromServer(').length - 1
    expect(hits).toBe(1)
  })

  it('keeps auto-send in the actions controller, which is what makes the order matter', () => {
    expect(ACTIONS_CONTROLLER).toMatch(/if \(connected && autoSendRef\.current\)/)
    expect(ACTIONS_CONTROLLER).toMatch(/\}, \[send, connected, autoSendTick, autoSendRef\]\)/)
  })

  it('decides the optimistic bubble from a LIVE store read, not a render closure', () => {
    // A render-closure read would make the intra-commit ordering unobservable;
    // `store.getState()` is what makes it load-bearing.
    expect(ACTIONS_CONTROLLER).toMatch(
      /const busyAtSend = selectComposerBusy\(store\.getState\(\), slot \?\? null\)/,
    )
    expect(ACTIONS_CONTROLLER).toMatch(/if \(!busyAtSend \|\| forceNew\) \{/)
  })

  it('routes that read through the slotRunning field the reconcile writes', () => {
    const selector = /export const selectComposerBusy[\s\S]*?\n\}/.exec(CHAT_SLICE)
    expect(selector, 'selectComposerBusy not found in chatSlice.ts').not.toBeNull()
    expect(selector![0]).toContain('state.chat.slotRunning')
    // The reducer the page's effect dispatches is the writer of that field.
    expect(CHAT_SLICE).toMatch(/syncSlotRunningFromServer\(state, action: PayloadAction</)
  })
})
