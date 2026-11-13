/**
 * A clarifier's Expand button keeps keyboard focus while it fetches follow-ups.
 *
 * A disabled button leaves the tab order, so disabling Expand for the fetch
 * dropped the focus of the user who pressed it to <body>. It is aria-disabled
 * instead, and a second press is refused until the fetch ends.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import '../../i18n'
import { i18nT } from '../../i18n/t'
import GrillTree from './GrillTree'
import type { GrillNode } from './grillTreeModel'

const clarifier: GrillNode = {
  id: 'c1',
  parent: null,
  kind: 'clarifier',
  text: 'Which market?',
  recommended: '',
  answer: 'EU',
  origin: 'grill',
  status: 'answered',
}

afterEach(cleanup)

describe('GrillTree Expand while fetching', () => {
  it('keeps focus on Expand and ignores a second press until the fetch ends', async () => {
    let finish: () => void = () => {}
    const onExpand = vi.fn(() => new Promise<void>(r => { finish = r }))
    render(<GrillTree tree={[clarifier]} dispatch={vi.fn()} onExpand={onExpand} />)

    const btn = screen.getByRole('button', { name: i18nT('apps.autoResearch.grillTree.expand') })
    btn.focus()
    fireEvent.click(btn)
    fireEvent.click(btn)

    expect(onExpand).toHaveBeenCalledTimes(1)
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).toHaveAttribute('aria-busy', 'true')
    expect(btn).not.toBeDisabled()
    expect(btn).toHaveFocus()

    await act(async () => { finish() })
    expect(btn).toHaveAttribute('aria-disabled', 'false')
    fireEvent.click(btn)
    expect(onExpand).toHaveBeenCalledTimes(2)
  })
})
