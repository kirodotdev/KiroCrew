/**
 * The Investigate / Review header button keeps keyboard focus while its session
 * starts.
 *
 * A disabled button leaves the tab order, so disabling it while busy dropped
 * the focus of the user who pressed it to <body>, and a failed start then
 * opened its error popover with focus lost. It is aria-disabled while busy
 * instead, and a press while busy is ignored.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { Telescope } from 'lucide-react'
import '../../../i18n'
import AgentSessionButton from './AgentSessionButton'

afterEach(cleanup)

const onStart = vi.fn()
let finish: () => void = () => {}

// Mirrors the owning buttons: the press sets busy until the start settles.
function Harness() {
  const [busy, setBusy] = useState(false)
  return (
    <AgentSessionButton
      icon={Telescope}
      label="Investigate"
      record={null}
      busy={busy}
      error={null}
      onClick={() => {
        onStart()
        setBusy(true)
        finish = () => setBusy(false)
      }}
      startHint="start"
      resumeHint="resume"
    />
  )
}

describe('AgentSessionButton while busy', () => {
  it('keeps focus on the button and ignores a press until the start settles', () => {
    render(<Harness />)
    const btn = screen.getByRole('button', { name: /Investigate/ })
    btn.focus()
    fireEvent.click(btn)
    fireEvent.click(btn)

    expect(onStart).toHaveBeenCalledTimes(1)
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).toHaveAttribute('aria-busy', 'true')
    expect(btn).not.toBeDisabled()
    expect(btn).toHaveFocus()

    act(() => { finish() })
    expect(btn).not.toHaveAttribute('aria-disabled')
    fireEvent.click(btn)
    expect(onStart).toHaveBeenCalledTimes(2)
  })

  it('stays disabled when blocked for a reason other than busy', () => {
    render(
      <AgentSessionButton icon={Telescope} label="Investigate" record={null} busy={false} error={null}
        onClick={onStart} startHint="start" resumeHint="resume" disabled />,
    )
    expect(screen.getByRole('button', { name: /Investigate/ })).toBeDisabled()
  })
})
