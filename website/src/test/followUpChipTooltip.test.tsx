import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'
import FollowUpBar from '../components/FollowUpBar'

/**
 * A chip's tooltip must describe what its click actually does.
 *
 * With quick send on and one chip already picked, a click ADDS TO THE SELECTION -- quick send
 * declines once anything is picked. The tooltip claimed a double-click would select and send,
 * because the send-eligibility flag was passed where the quick-send setting belonged, leaving
 * the add-to-selection wording unreachable.
 */
describe('FollowUpBar chip tooltip agrees with the click', () => {
  const opts = ['Merge it now', 'Skip it']

  function titles(picked: string[]) {
    const { container } = render(
      <FollowUpBar
        options={opts}
        recommended={null}
        picked={new Set(picked)}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
      />,
    )
    return Array.from(container.querySelectorAll('button')).map(b => b.getAttribute('title') || '')
  }

  it('offers instant send only while nothing is picked', () => {
    expect(titles([]).some(t => /instantly/i.test(t))).toBe(true)
  })

  it('says add-to-selection once something is picked, not double-click-to-send', () => {
    const withPick = titles(['Merge it now'])
    const unpicked = withPick.filter(t => !/remove/i.test(t))
    expect(unpicked.length).toBeGreaterThan(0)
    // The defect: these read "double-click to select and send" while the click only adds.
    expect(unpicked.some(t => /double-click/i.test(t))).toBe(false)
    expect(unpicked.some(t => /selection/i.test(t))).toBe(true)
  })

  it('never advertises instant send once something is picked', () => {
    expect(titles(['Merge it now']).some(t => /instantly/i.test(t))).toBe(false)
  })

  it('does not promise a double-click send on a PICKED declined chip', () => {
    // `useDebouncedClick` drops a declined chip's send apparatus, so a double-click toggles
    // twice and dispatches nothing -- the picked branch promised that send anyway.
    const declined = '(recommended) /clear'
    const { container } = render(
      <FollowUpBar
        options={[declined, 'Merge it now']}
        recommended={declined}
        picked={new Set([declined])}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
      />,
    )
    const all = Array.from(container.querySelectorAll('button')).map(b => b.getAttribute('title') || '')
    const picked = all.filter(t => /remove/i.test(t))
    expect(picked.length).toBeGreaterThan(0)
    expect(picked.some(t => /double-click/i.test(t))).toBe(false)
  })
})
