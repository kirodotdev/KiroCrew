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
import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { i18nT } from '../i18n/t'

import FeedbackPill from '../components/FeedbackPill'
import { OPEN_DELAY_MS } from '../components/InstantTip'

const status: {
  release_channel?: string
  update_channel?: string
  update_check_status?: string
  update_channel_move_pending?: boolean
} = {}

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
  delete status.update_channel
  delete status.update_check_status
  delete status.update_channel_move_pending
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

  it('shows NO chip on a PROMOTED stable release, whose version reads as insider', () => {
    // `release_channel` is derived from the version STRING, and promotion
    // re-points the soaked candidate's bytes at stable without re-stamping them:
    // a promoted stable release is literally `0.4.1rc1`, which classifies as
    // `insider`. Keyed on that alone, this chip was a permanent header bug
    // affordance for the ENTIRE stable population — the thing the component's
    // own docstring says stable must not have.
    status.release_channel = 'insider'
    status.update_channel = 'stable'
    status.update_check_status = 'succeeded'
    status.update_channel_move_pending = false
    mount()
    expect(screen.queryByTestId('prerelease-report-chip')).toBeNull()
  })

  it('KEEPS the chip for insider bytes the stable lane never shipped', () => {
    // The exemption is "the lane you follow publishes these bytes", not "you
    // follow stable": an insider build whose switcher was flipped to stable is
    // still running prerelease bytes, and it is still the population whose
    // reports matter.
    status.release_channel = 'insider'
    status.update_channel = 'stable'
    status.update_check_status = 'succeeded'
    status.update_channel_move_pending = true
    mount()
    expect(screen.getByTestId('prerelease-report-chip')).toBeInTheDocument()
  })

  it('does not exempt on an unproven check', () => {
    // No completed comparison means UNKNOWN. Exempting there would hide the chip
    // from a genuine prerelease user on the strength of a check that never ran.
    status.release_channel = 'insider'
    status.update_channel = 'stable'
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

  it('renders the action as its visible text', () => {
    // The UX review's finding, pinned. The chip once rendered only a Bug icon
    // and the lane name ("Nightly") in accent badge type, with the action
    // confined to the tooltip — which reads as a status badge, so a first-time
    // prerelease user has no reason to click and the always-visible report entry
    // is never discovered. A tooltip is not discoverability: it requires a hover
    // the user has no reason to attempt.
    status.release_channel = 'nightly'
    mount()
    expect(screen.getByTestId('prerelease-report-chip').textContent).toMatch(
      /report problem/i,
    )
  })

  it('gives the chip an accessible name that states the ACTION', () => {
    status.release_channel = 'nightly'
    mount()
    expect(screen.getByTestId('prerelease-report-chip')).toHaveAccessibleName(
      /report a problem/i,
    )
  })

  it('speaks the same name as the flow it opens', () => {
    // The modal, the nav rail link and Settings › About all call this "Report a
    // Problem". A chip promising a "bug report" made the user re-read the
    // dialog to check they had clicked the right thing.
    status.release_channel = 'insider'
    mount()
    const chip = screen.getByTestId('prerelease-report-chip')
    expect(chip).toHaveAccessibleName(/problem/i)
    expect(chip.textContent).not.toMatch(/\bbug\b/i)
  })

  it.each(['nightly', 'insider'])(
    'keeps the %s lane out of the visible text',
    ch => {
      // The control is an ACTION, not a status readout. A lane name competing
      // with the action for the eye is what made the earlier revision read as
      // an identity badge, so its absence here is the fix — asserted because a
      // well-meaning "show the user which build they're on" edit would
      // reintroduce it and every other test would still pass.
      status.release_channel = ch
      mount()
      expect(screen.getByTestId('prerelease-report-chip').textContent).not.toMatch(
        new RegExp(ch, 'i'),
      )
    },
  )

  it('still names the lane in the tooltip', () => {
    // Which build a report gets tagged against is real information — it is
    // supplementary, not absent. It lives in the instant tip (below), not in a
    // native `title`: once the desktop top bar hides the chip's label (rung 2), the
    // chip is an icon alone, and a native title is the one tooltip keyboard
    // focus never shows. One tip, not two: the native title goes so the two
    // can never stack on hover.
    status.release_channel = 'nightly'
    mount()
    const chip = screen.getByTestId('prerelease-report-chip')
    expect(chip).not.toHaveAttribute('title')
    fireEvent.focus(chip)
    expect(screen.getByRole('tooltip').textContent).toMatch(/nightly/i)
  })

  it('shows Report problem a styled tip on hover intent and on keyboard focus, without repeating its name as a description', () => {
    // From rung 2 the feedback labels hide and the chip is a bare icon: the tip
    // is then its only visible name. Same mechanism as its sibling -- a DOM
    // bubble opened synchronously on keyboard focus and after the hover-intent
    // delay, placed below the pill -- but its OWN hook instance: each anchor
    // owns one bubble, and the two sit side by side. The tip's text IS the
    // chip's accessible name, so the chip carries no aria-describedby.
    vi.useFakeTimers()
    try {
      status.release_channel = 'nightly'
      mount()
      const chip = screen.getByTestId('prerelease-report-chip')
      const feature = screen.getByRole('button', { name: /request a feature/i })
      const expected = i18nT('components.feedbackPill.report_problem_on_build', {
        channel: i18nT('components.feedbackPill.nightly'),
      })
      expect(chip).toHaveAccessibleName(expected)
      expect(chip).not.toHaveAttribute('aria-describedby')
      expect(screen.queryByRole('tooltip')).toBeNull()

      // Keyboard focus: synchronous.
      fireEvent.focus(chip)
      let tip = screen.getByRole('tooltip')
      expect(tip).toHaveAttribute('data-placement', 'below')
      expect(tip).toHaveTextContent(expected)
      expect(chip).toHaveAccessibleDescription('')
      fireEvent.blur(chip)
      expect(screen.queryByRole('tooltip')).toBeNull()

      // Hover intent: after the shared delay, the same bubble.
      fireEvent.mouseEnter(chip)
      expect(screen.queryByRole('tooltip')).toBeNull()
      act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
      tip = screen.getByRole('tooltip')
      expect(tip).toHaveTextContent(expected)
      fireEvent.mouseLeave(chip)
      expect(screen.queryByRole('tooltip')).toBeNull()

      // The sibling's tip is untouched by the chip's: focusing it shows ITS copy.
      fireEvent.focus(feature)
      expect(screen.getByRole('tooltip')).toHaveTextContent(
        i18nT('components.feedbackPill.request_feature_starts_agent'),
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('tells the user up front, in plain words, that Request a Feature starts a chat that spends monthly usage (#13342)', () => {
    // The action is a metered agent turn by design (the agent drafts and files
    // the request), but its wording promised a feedback form: a capped user
    // learned the difference only from the usage-limit error. The explanation
    // is real copy, not a native `title` -- a DOM bubble the button names via
    // aria-describedby, opened synchronously on keyboard focus (and on hover
    // intent), so it is readable by keyboard and screen-reader users and can be
    // photographed. It opens BELOW the pill: the pill lives in the top bar.
    // The visible label stays the action, so the button's accessible NAME is
    // unchanged and every caller that finds it by that name keeps working.
    mount()
    const button = screen.getByRole('button', { name: /request a feature/i })
    expect(button).not.toHaveAttribute('title')
    expect(screen.queryByRole('tooltip')).toBeNull()
    fireEvent.focus(button)
    const tip = screen.getByRole('tooltip')
    expect(tip.id).toBe(button.getAttribute('aria-describedby'))
    expect(tip).toHaveAttribute('data-placement', 'below')
    expect(tip).toHaveTextContent(i18nT('components.feedbackPill.request_feature_starts_agent'))
    // Plain words: no "inference", no "agent conversation"; it names the cost.
    expect(tip.textContent).toMatch(/monthly usage/i)
    expect(tip.textContent).not.toMatch(/inference/i)
    expect(button).toHaveAccessibleName(/request a feature/i)
  })

  it('a touch tap still shows the usage warning (openOnTap), since the bubble is the only place it appears', () => {
    // The shared hook ignores a tap's replayed mouseenter/focus by default so a
    // bubble does not cost iOS the click. This anchor opts out: the warning
    // exists so a phone user sees "this spends monthly usage" BEFORE the tap
    // starts the metered turn (#13342), and nothing else on the pill says so.
    // Same event order as the `tap()` helper in InstantTip.test.tsx: the touch
    // pointer's own events, the replayed mouse events (mousedown -> focus),
    // then the click.
    const { onRequestFeature } = mount()
    const button = screen.getByRole('button', { name: /request a feature/i })
    fireEvent.pointerEnter(button, { pointerType: 'touch' })
    fireEvent.pointerDown(button, { pointerType: 'touch' })
    fireEvent.pointerUp(button, { pointerType: 'touch' })
    fireEvent.mouseEnter(button)
    fireEvent.mouseDown(button)
    fireEvent.focus(button)
    fireEvent.mouseUp(button)
    fireEvent.click(button)
    expect(screen.getByTestId('feedback-pill-request-feature-tip')).toBeInTheDocument()
    expect(onRequestFeature).toHaveBeenCalledTimes(1)
  })
})
