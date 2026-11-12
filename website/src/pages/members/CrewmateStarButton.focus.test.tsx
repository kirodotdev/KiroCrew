// The crewmate star keeps its focus while its write is in flight.
//
// A star press flips the star at once and holds the control until the server
// answers. A native `disabled` on the button a reader just pressed blurs it to
// <body>, and an unstarred star at md+ is shown only while hovered or focused,
// so a keyboard user lost both their place and the control. It is held with
// aria-disabled instead, and a second press is refused.
import { useState } from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, cleanup, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../../test/helpers'
import type { MemberRosterRow } from '../../api/client'
import CrewmateStarButton from './CrewmateStarButton'

afterEach(cleanup)

const ROW = { name: 'atlas', slug: 'atlas', starred: false } as unknown as MemberRosterRow

/** The host's role, reduced: a press flips the star and holds it until settled. */
function Harness({ onToggle }: { onToggle: (m: MemberRosterRow) => void }) {
  const [m, setM] = useState(ROW)
  const [pending, setPending] = useState(false)
  return (
    <>
      <CrewmateStarButton
        m={m}
        pending={pending}
        testId="star"
        onToggle={(row) => { onToggle(row); setM({ ...row, starred: !row.starred }); setPending(true) }}
      />
      <button type="button" onClick={() => setPending(false)}>settle</button>
    </>
  )
}

describe('CrewmateStarButton focus while its write is in flight', () => {
  it('keeps focus on the star, refuses a second press, and keeps it after the write lands', async () => {
    const onToggle = vi.fn()
    renderWithProviders(<Harness onToggle={onToggle} />)
    const star = screen.getByTestId('star')

    star.focus()
    await userEvent.keyboard('{Enter}')
    expect(onToggle).toHaveBeenCalledTimes(1)
    expect(star.getAttribute('aria-pressed')).toBe('true')
    expect(star.getAttribute('aria-disabled')).toBe('true')
    expect(star.hasAttribute('disabled')).toBe(false)
    expect(document.activeElement).toBe(star)

    await userEvent.keyboard('{Enter}')
    expect(onToggle).toHaveBeenCalledTimes(1)

    // A programmatic click: it settles the write without moving focus.
    act(() => { screen.getByRole('button', { name: 'settle' }).click() })
    expect(star.hasAttribute('aria-disabled')).toBe(false)
    expect(document.activeElement).toBe(star)
  })
})
