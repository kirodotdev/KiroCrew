/** The page marker must be exactly as durable as the draft it captions: same store, same TTL.
 *  It now lives in the recovery store, so this pins that the merge did not weaken it. */
import { describe, it, expect, beforeEach } from 'vitest'
import { PANE_RECOVERY_KEY, loadStagedSend, setStagedSend, clearStagedSend, loadPaneRecoveries, savePaneRecoveries, __resetPaneRecoveryForTests } from './chatPaneRecovery'
import { DRAFTS_KEY } from './chatDrafts'

describe('the staged-send marker survives a reload in the recovery store', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear(); __resetPaneRecoveryForTests() })

  it('persists to localStorage and reads back', () => {
    setStagedSend('slot-a', 's-durable-1')
    expect(localStorage.getItem(PANE_RECOVERY_KEY)).toBeTruthy()
    expect(sessionStorage.getItem(PANE_RECOVERY_KEY)).toBeNull()
    expect(loadStagedSend('slot-a')).toBe('s-durable-1')
  })

  it('does not collide with a pane payload for the SAME slot', () => {
    // Measured: MembersPage renders <ChatPane slotKey={activeSlot}>, so both surfaces can hold
    // restored work for one slot. An unqualified key would have one clobber the other.
    const all = loadPaneRecoveries()
    all['slot-a'] = { text: 'the pane payload', files: [], sendId: 's-pane' }
    savePaneRecoveries(all)
    setStagedSend('slot-a', 's-page')
    expect(loadPaneRecoveries()['slot-a']?.text).toBe('the pane payload')
    expect(loadStagedSend('slot-a')).toBe('s-page')
  })

  it('clears only its own record', () => {
    setStagedSend('slot-a', 's-1')
    clearStagedSend('slot-a')
    expect(loadStagedSend('slot-a')).toBeUndefined()
  })

  it('uses a different key from the drafts store', () => {
    expect(DRAFTS_KEY).not.toBe(PANE_RECOVERY_KEY)
  })
})
