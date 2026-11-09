/**
 * `Btn` keeps keyboard focus while the action it started is still running.
 *
 * The Issue Radar action bars (CommentBox, PrActionsBar, PrBulkBar) route their
 * Post / Approve / Merge / Apply … buttons through `Btn`, and each used to pass
 * its own `*.busy` flag as native `disabled`. A disabled button leaves the tab
 * order, so the keyboard user who pressed one had their focus dropped to <body>
 * while the request ran. `Btn` now takes a `busy` prop that marks the button
 * `aria-disabled`+`aria-busy` (keeping it focusable) and refuses the press until
 * the work settles, while a genuinely-unavailable button still passes `disabled`.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { Btn } from './ui'

afterEach(cleanup)

describe('Btn while busy', () => {
  it('stays focusable, refuses the press, and is not native-disabled', () => {
    const onClick = vi.fn()
    render(<Btn busy onClick={onClick}>Post</Btn>)
    const btn = screen.getByRole('button', { name: 'Post' })

    btn.focus()
    expect(btn).toHaveFocus()

    fireEvent.click(btn)
    fireEvent.click(btn)
    expect(onClick).not.toHaveBeenCalled()

    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).toHaveAttribute('aria-busy', 'true')
    // `data-busy` is the hook the `.busy-cue*` CSS keys on, set only while busy.
    expect(btn).toHaveAttribute('data-busy')
    expect(btn).not.toBeDisabled()
    // Focus is still on the button it was pressed on, not dropped to <body>.
    expect(btn).toHaveFocus()
  })

  it('fires and clears the busy markers once the action settles', () => {
    const onClick = vi.fn()
    const { rerender } = render(<Btn busy onClick={onClick}>Post</Btn>)
    rerender(<Btn onClick={onClick}>Post</Btn>)
    const btn = screen.getByRole('button', { name: 'Post' })

    expect(btn).not.toHaveAttribute('aria-disabled')
    expect(btn).not.toHaveAttribute('aria-busy')
    expect(btn).not.toHaveAttribute('data-busy')
    fireEvent.click(btn)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('does not apply the busy styling hook to a caller that is aria-disabled for its own reason', () => {
    // The `.busy-cue*` CSS keys on `data-busy`, which only the `busy` prop sets —
    // NOT on `aria-disabled`, which a caller may set itself. So a Btn that is
    // aria-disabled WITHOUT being busy carries `busy-cue` in its class list but
    // is never matched by the busy rules, leaving existing callers unchanged.
    render(<Btn aria-disabled>Resume</Btn>)
    const btn = screen.getByRole('button', { name: 'Resume' })
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).not.toHaveAttribute('data-busy')
    expect(btn).not.toHaveAttribute('aria-busy')
  })

  it('still takes native disabled when blocked for a reason other than busy', () => {
    const onClick = vi.fn()
    render(<Btn disabled onClick={onClick}>Approve</Btn>)
    const btn = screen.getByRole('button', { name: 'Approve' })
    expect(btn).toBeDisabled()
    fireEvent.click(btn)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('can be both blocked and busy without losing either signal', () => {
    render(<Btn disabled busy>Apply</Btn>)
    const btn = screen.getByRole('button', { name: 'Apply' })
    expect(btn).toBeDisabled()
    expect(btn).toHaveAttribute('aria-busy', 'true')
  })
})
