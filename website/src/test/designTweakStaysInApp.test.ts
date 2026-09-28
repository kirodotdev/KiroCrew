import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

/**
 * Design Tweak is a self-contained surface: the app never decides to move the
 * user to the chat tab. Error recovery and fallbacks resolve inside the panel.
 *
 * This is pinned structurally because it is exactly the kind of rule a helpful
 * fallback re-breaks — twice already, `navigate(chatRoute())` was added as
 * "recovery" for a failed dispatch and for a missing project folder. The ONE
 * permitted navigation is `openInChat`, which fires from an icon the user
 * clicks; that is the user moving themselves.
 */
describe('design-tweak stays in the app', () => {
  const src = readFileSync(
    join(process.cwd(), 'src/apps/design-tweak/DesignTweakPage.tsx'),
    'utf-8',
  )

  it('navigates exactly once, and only from the explicit open-in-chat control', () => {
    const calls = src.match(/navigate\(/g) ?? []
    expect(calls).toHaveLength(1)

    // Slice the `openInChat` useCallback — from its declaration to the `}, [`
    // that closes it — and assert the sole navigate call lives inside it.
    const start = src.indexOf('const openInChat')
    expect(start).toBeGreaterThan(-1)
    const end = src.indexOf('}, [', start)
    expect(end).toBeGreaterThan(start)

    const idx = src.indexOf('navigate(')
    expect(idx).toBeGreaterThan(start)
    expect(idx).toBeLessThan(end)
  })

  it('does not stage a prompt into the chat composer', () => {
    // `setPendingInput` is how the old composer handoff worked. Its absence is
    // what keeps a failed send from ejecting the user into the chat tab.
    expect(src).not.toContain('setPendingInput')
  })
})
