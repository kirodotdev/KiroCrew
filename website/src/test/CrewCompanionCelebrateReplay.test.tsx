/**
 * Celebrate-hop replay regression.
 *
 * The animated span is keyed off the motion name AND a per-reaction epoch. Before the
 * epoch existed, the key was the motion NAME alone: once the companion was in
 * celebrate-continuity (a `happy` mood resolves to `celebrate`, and every completion
 * sets `happy`), the next completion reused the same DOM node, the CSS keyframes never
 * re-fired, and the hop was silently skipped. These pin that a repeat celebration with
 * a bumped epoch remounts (replays), while the same epoch does not churn.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import React from 'react'
import { PetAvatar } from '../apps/crew-companion/PetAvatar'
import { activeAnimFor } from '../apps/crew-companion/petAnim'

function animSpan(container: HTMLElement): HTMLElement {
  return container.querySelector('span[aria-hidden] > span') as HTMLElement
}

describe('celebrate hop replays on every completion', () => {
  it('remounts the animated span whenever the reaction epoch is bumped', () => {
    // idle / neutral — no motion.
    let anim = activeAnimFor({ state: 'idle', mood: 'neutral' })
    const { container, rerender } = render(
      <PetAvatar size={128} state={'idle'} mood={'neutral'} anim={anim} animEpoch={0} />,
    )
    const nIdle = animSpan(container)
    expect(nIdle.className).toBe('')

    // First completion: react('done') bumps the epoch and sets mood happy.
    anim = activeAnimFor({ state: 'done', mood: 'happy' })
    rerender(<PetAvatar size={128} state={'done'} mood={'happy'} anim={anim} animEpoch={1} />)
    const nDone1 = animSpan(container)
    expect(nDone1.className).toContain('kg-anim-celebrate')
    expect(nDone1).not.toBe(nIdle) // remounted → keyframes play

    // 2.4s later the state resets to idle but the happy mood (and so celebrate) lingers.
    // No new reaction, so the epoch is unchanged: the node must NOT churn.
    anim = activeAnimFor({ state: 'idle', mood: 'happy' })
    rerender(<PetAvatar size={128} state={'idle'} mood={'happy'} anim={anim} animEpoch={1} />)
    const nHold = animSpan(container)
    expect(nHold.className).toContain('kg-anim-celebrate')
    expect(nHold).toBe(nDone1) // same epoch, same motion → no spurious restart

    // A SECOND completion while still in celebrate-continuity. Same motion name, but a
    // fresh reaction bumps the epoch — so the span MUST remount and replay the hop.
    anim = activeAnimFor({ state: 'done', mood: 'happy' })
    rerender(<PetAvatar size={128} state={'done'} mood={'happy'} anim={anim} animEpoch={2} />)
    const nDone2 = animSpan(container)
    expect(nDone2.className).toContain('kg-anim-celebrate')
    expect(nDone2).not.toBe(nHold) // the fix: repeat celebration replays
  })
})
