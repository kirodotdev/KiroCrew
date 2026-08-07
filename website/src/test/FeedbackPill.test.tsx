/**
 * The prerelease bug-report chip: the affordance that exists so a nightly or
 * insider user does not have to know that Settings › About has a Support card.
 *
 * The behaviours pinned here are the ones whose failure is SILENT — a chip that
 * quietly stops rendering on nightly, or one that starts rendering on stable,
 * both look fine in review and are only noticed when the bug reports stop
 * arriving (or when a stable user asks why the app expects to break).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import FeedbackPill from '../components/FeedbackPill'

const status: { release_channel?: string } = {}

vi.mock('../store', () => ({
  useAppSelector: (sel: (s: unknown) => unknown) =>
    sel({ dashboard: { status } }),
}))

function mount() {
  const onRequestFeature = vi.fn()
  const onReportProblem = vi.fn()
  render(
    <FeedbackPill onRequestFeature={onRequestFeature} onReportProblem={onReportProblem} />,
  )
  return { onRequestFeature, onReportProblem }
}

beforeEach(() => {
  delete status.release_channel
})

describe('FeedbackPill', () => {
  it('renders only the feature-request half on a stable build', () => {
    status.release_channel = 'stable'
    mount()
    expect(screen.queryByTestId('prerelease-report-chip')).toBeNull()
  })

  it.each(['nightly', 'insider'])('shows the report chip on a %s build', ch => {
    status.release_channel = ch
    mount()
    expect(screen.getByTestId('prerelease-report-chip')).toBeInTheDocument()
  })

  it('treats a missing release_channel as not-prerelease', () => {
    // An older gateway does not send the field, and the very first render
    // happens before any status arrives. Neither may flash a chip that claims a
    // lane the dashboard has not been told about.
    mount()
    expect(screen.queryByTestId('prerelease-report-chip')).toBeNull()
  })

  it('opens the shared Report a Problem flow, not a bare issue link', async () => {
    // The chip must reuse the diagnostics flow (redacted bundle + pre-filled,
    // channel-labelled issue). A plain link to the tracker would lose exactly
    // what triage needs, which is why every other entry point mounts the modal.
    status.release_channel = 'nightly'
    const { onReportProblem, onRequestFeature } = mount()
    await userEvent.click(screen.getByTestId('prerelease-report-chip'))
    expect(onReportProblem).toHaveBeenCalledTimes(1)
    expect(onRequestFeature).not.toHaveBeenCalled()
  })

  it('still runs the feature-request action from the left half', async () => {
    status.release_channel = 'nightly'
    const { onReportProblem, onRequestFeature } = mount()
    await userEvent.click(screen.getByText(/request a feature/i))
    expect(onRequestFeature).toHaveBeenCalledTimes(1)
    expect(onReportProblem).not.toHaveBeenCalled()
  })

  it('gives the chip an accessible name that states the ACTION', () => {
    // The visible text is the lane ("Nightly"), which alone tells a screen
    // reader nothing about what the button does.
    status.release_channel = 'nightly'
    mount()
    expect(screen.getByTestId('prerelease-report-chip')).toHaveAccessibleName(
      /report a bug/i,
    )
  })

  it('names the lane the user is actually on', () => {
    status.release_channel = 'insider'
    mount()
    expect(screen.getByTestId('prerelease-report-chip').textContent).toMatch(/insider/i)
  })
})
