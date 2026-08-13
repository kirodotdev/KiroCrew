/**
 * The panel footer must tell the truth about session notifications.
 *
 * This was a SILENT failure, which is why it needs a test rather than a fix alone:
 * the footer rendered a perfectly plausible sentence either way. `sessionOn` was
 * declared optional with a `= true` default, and `panel.tsx` never threaded the
 * value out of the payload it was already fetching — so the line said "task alerts
 * on" while the backend had the setting stored as false and was correctly
 * suppressing every notification. The user then sees silence and reasonably
 * concludes notifications are broken, when the only broken thing is this sentence.
 *
 * `breakMins` sat right beside it, was declared REQUIRED, and was threaded
 * correctly. That contrast is the lesson: an optional prop with a truthy default
 * cannot fail loudly, so the compiler never asked for the value.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import { PanelCard } from '../apps/crew-companion/PanelCard'

/** The card needs almost nothing else to render its footer. */
function renderFooter(sessionOn: boolean) {
  return render(
    <PanelCard upNext={[]} breakMins={30} sessionOn={sessionOn} />,
  )
}

describe('panel footer: session notification state', () => {
  it('says alerts are ON when they are enabled', () => {
    renderFooter(true)
    expect(screen.getByText(/task alerts on/i)).toBeInTheDocument()
  })

  it('says alerts are OFF when they are disabled', () => {
    renderFooter(false)
    // The regression: this used to read "on" because the prop defaulted to true.
    expect(screen.getByText(/task alerts off/i)).toBeInTheDocument()
    expect(screen.queryByText(/task alerts on/i)).not.toBeInTheDocument()
  })

  it('renders the break cadence beside it from the same source', () => {
    renderFooter(false)
    expect(screen.getByText(/30/)).toBeInTheDocument()
  })
})
