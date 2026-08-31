import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'
import FollowUpBar from '../components/FollowUpBar'

/**
 * A declined chip must be distinguishable from its siblings WITHOUT hovering.
 *
 * In a quick-send row every chip sends on click, except one whose label would become a
 * command once the marker came off -- that one fills the composer instead. Identical-looking
 * siblings behaving differently was reachable only through a tooltip or the under-bar note,
 * i.e. after the first surprising click. The dashed edge states it at rest.
 */
describe('FollowUpBar marks a declined chip at the chip itself', () => {
  function chips(options: string[]) {
    const { container } = render(
      <FollowUpBar
        options={options}
        recommended={options[0] ?? null}
        picked={new Set<string>()}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
      />,
    )
    return Array.from(container.querySelectorAll('button')).map(b => b.className)
  }

  // `/clear` would become a live command if the marker were stripped, so the guard declines it.
  const DECLINED = '(recommended) /clear'
  const ORDINARY = 'Merge it now'

  it('gives the declined chip a dashed edge', () => {
    const withDeclined = chips([DECLINED, ORDINARY]).filter(c => c.includes('border-dashed'))
    expect(withDeclined.length).toBeGreaterThan(0)
  })

  it('leaves an ordinary chip solid, so the marking actually discriminates', () => {
    const all = chips([DECLINED, ORDINARY])
    const dashed = all.filter(c => c.includes('border-dashed'))
    expect(dashed.length).toBe(1)
    expect(all.length - dashed.length).toBeGreaterThan(0)
  })

  it('marks nothing when no chip is declined', () => {
    expect(chips([ORDINARY, 'Skip it']).some(c => c.includes('border-dashed'))).toBe(false)
  })

  it('marks it in the scroll layout too, which renders through a different map', () => {
    const { container } = render(
      <FollowUpBar
        options={[DECLINED, ORDINARY]}
        recommended={DECLINED}
        picked={new Set<string>()}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
        layout="scroll"
      />,
    )
    const all = Array.from(container.querySelectorAll('button')).map(b => b.className)
    expect(all.filter(c => c.includes('border-dashed')).length).toBe(1)
  })
})
