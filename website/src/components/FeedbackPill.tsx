import { Lightbulb, Bug } from 'lucide-react'

import { useAppSelector } from '../store'
import { i18nT } from '../i18n/t'

/**
 * Top-bar feedback control: "Request a Feature" on the left, and — only on a
 * prerelease build — a channel chip on the right that opens the SAME "Report a
 * Problem" flow the nav rail and Settings › About already use.
 *
 * WHY A SPLIT PILL rather than a second free-standing button. The two actions
 * are one intent ("tell the maintainers something"), and the header already
 * spends its width on a readout capsule whose segments are divided by a
 * `w-px h-3.5 bg-border` rule — so a split pill is the shape this header
 * already speaks, and it costs one pill's worth of chrome instead of two.
 *
 * WHY THE CHIP IS CHANNEL-GATED. On stable this renders byte-identically to the
 * single button it replaced: a supported build already has three ways to report
 * a problem, and a permanent bug affordance in the header would read as "this
 * app expects to break". A nightly build genuinely does expect to break, and its
 * users are the ones whose reports are worth an always-visible entry point —
 * previously they had to know to look in Settings › About, and on a CLI install
 * that note did not render at all.
 *
 * WHY THE LANE IS NOT ON THE CHIP. It reads as an action, not as a status badge:
 * the visible text is the action alone, styled identically to its sibling. Which
 * build the report will be tagged against is real information, but it belongs in
 * the tooltip — a lane name competing with the action for the eye is what made an
 * earlier revision read as an identity indicator nobody thought to click.
 */
export default function FeedbackPill({
  onRequestFeature,
  onReportProblem,
}: {
  onRequestFeature: () => void
  onReportProblem: () => void
}) {
  // Resolved BY THE GATEWAY (see release_channel.py). Deliberately not derived
  // from `status.version` here: the same release is stamped as SemVer for the
  // desktop app and PEP 440 for wheels, and a frontend copy of that rule would
  // drift and quietly classify a prerelease build as stable — which is the bug
  // this whole change fixes.
  const channel = useAppSelector(s => s.dashboard.status?.release_channel)

  // `undefined` means an older gateway that does not send the field, or a status
  // payload that has not arrived yet. Treated as "not prerelease" so the header
  // never flickers a chip in and out, and never claims a lane it does not know.
  const chipChannel = channel === 'nightly' || channel === 'insider' ? channel : null

  const channelLabel =
    chipChannel === 'nightly'
      ? i18nT('components.feedbackPill.nightly')
      : i18nT('components.feedbackPill.insider')

  // One string for tooltip AND accessible name, so the two can never drift.
  // Names the flow the way the flow names ITSELF ("Report a Problem" — see
  // ReportProblemModal): a chip that promises a "bug report" and opens a
  // "Report a Problem" dialog makes the user re-read the dialog to check they
  // clicked the right thing.
  const actionDescription = i18nT('components.feedbackPill.report_problem_on_build', {
    channel: channelLabel,
  })

  return (
    <div
      data-testid="feedback-pill"
      className="flex items-center h-7 rounded-xl bg-card shrink-0 overflow-hidden"
    >
      <button
        type="button"
        className="flex items-center gap-1.5 h-full px-2.5 text-muted hover:text-text transition-colors cursor-pointer text-[12px] whitespace-nowrap bg-transparent border-0"
        onClick={onRequestFeature}
        title={i18nT('app.request_a_feature')}
      >
        <Lightbulb size={13} className="lucide-inline" />{' '}
        {i18nT('app.request_a_feature_2')}
      </button>

      {chipChannel && (
        <>
          {/* Same 1px × 14px rule the readout capsule uses between segments, so
              the split reads as one control rather than two glued buttons. */}
          <span className="w-px h-3.5 bg-border shrink-0" aria-hidden="true" />
          <button
            type="button"
            data-testid="prerelease-report-chip"
            // Styled EXACTLY like its sibling: same 12px, same muted-to-text
            // hover. An earlier revision made this accent-coloured, 11px
            // semibold, tracking-wide, with the lane name as its only visible
            // text — badge typography carrying a status word, which read as an
            // identity indicator, so a first-time user had no reason to click
            // and the always-visible report entry went undiscovered.
            //
            // Matching the sibling is what makes it read as the second half of
            // one control rather than a decoration hanging off the end.
            className="flex items-center gap-1.5 h-full px-2.5 text-muted hover:text-text transition-colors cursor-pointer text-[12px] whitespace-nowrap bg-transparent border-0"
            onClick={onReportProblem}
            // The LANE lives here and only here. Which build a report will be
            // tagged against is useful but secondary, so it is supplementary
            // hover detail — not a word competing with the action for the eye.
            title={actionDescription}
            aria-label={actionDescription}
          >
            <Bug size={13} className="lucide-inline" />{' '}
            {i18nT('components.feedbackPill.report_problem')}
          </button>
        </>
      )}
    </div>
  )
}
