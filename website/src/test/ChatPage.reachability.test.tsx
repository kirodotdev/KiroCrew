/**
 * Guards the native-browser reachability declaration in ChatPage.
 *
 * The Electron command channel can only deliver a `browser_*` op for a session
 * key it is already polling for (see `listPanelIds` in electron/main.js), and it
 * must poll BEFORE any URL is known. An earlier revision declared only
 * `activeSlot`, which produced two failures seen in a live diagnostic run:
 *
 *   1. a chat created and messaged within seconds RACED the registration — the
 *      navigate reached the gateway first, got `no-native-panel` (503) because no
 *      poller held that key yet, and the proxy fell back to the Playwright mirror
 *      for the whole turn;
 *   2. a BACKGROUND chat was never reachable at all, even when it was the session
 *      the agent was acting for.
 *
 * Declaring a key is not authorization (Browser Mode is), so reporting every open
 * slot costs nothing and removes both.
 *
 * This is a SOURCE-CONTRACT test, matching the existing ChatPage convention (see
 * ChatPage.mcpOAuth.test.tsx): ChatPage's message list is driven by a custom
 * virtualizer that mounts an empty window under jsdom, so a full-page render
 * cannot exercise this effect. The behaviour under test is an IPC side effect of
 * a Redux-derived list, and what regressed before was precisely the source shape
 * — which input the effect reads — so that is what this locks in.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const chatPageSrc = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')

describe('ChatPage – native browser reachability', () => {
  it('derives the tracked keys from the open slot list, not the active slot', () => {
    // The regression shape: `trackSession(activeSlot, ...)`. If that ever comes
    // back, a background chat is unreachable and a fresh chat races the poller.
    expect(chatPageSrc).not.toMatch(/trackSession\(\s*activeSlot/)
    expect(chatPageSrc).toMatch(/trackableSlotKeys\s*=\s*useMemo\(/)
    expect(chatPageSrc).toMatch(/slots\.map\(s => s\.key\)/)
  })

  it('runs the declaration off the slot-key list', () => {
    // A dependency on `activeSlot` would re-run (and previously re-register) on
    // every tab switch while still only ever declaring one key.
    expect(chatPageSrc).toMatch(/\}, \[trackableSlotKeys\]\)/)
  })

  it('untracks only keys that disappeared, never the whole set per change', () => {
    // A cleanup that untracked everything on each slot-list edit would drop a key
    // mid-turn — the same race, re-introduced through teardown instead of setup.
    expect(chatPageSrc).toMatch(/trackedSlotsRef/)
    expect(chatPageSrc).toMatch(/if \(want\.has\(key\)\) continue/)
  })
})
