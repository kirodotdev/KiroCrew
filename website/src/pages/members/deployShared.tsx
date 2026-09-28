/**
 * What the two lane views of "Your crew in the cloud" share.
 *
 * The EC2 view (DeployMyCrew.tsx) and the Fargate view (FargateCrewView.tsx)
 * answer "is my crew still up" from different sources and so are two views,
 * not one view with a branch. What they share is vocabulary, kept here so
 * neither imports the other: the unknown glyph, the test for a region id
 * before it is put into a hostname, the age of a record, and the stat card.
 */
import type { LaunchJob } from '../../api/client'
import { fmtDuration } from '../../i18n/format'

/** The unknown-value glyph, shared with the member stat cards: an en dash,
 *  never a zero, so "nothing" and "could not read" cannot render alike. */
export const UNKNOWN = '\u2013'

/** Region ids as AWS spells them (us-east-1, ap-southeast-2, us-gov-west-1).
 *  Only a region that parses as one is put into a console link: the value
 *  comes off a stored record, and a hostname is built from it. */
export const AWS_REGION_RE = /^[a-z]{2}(-[a-z]+)+-\d$/

/**
 * "Step N of M" for a moving launch. The step list can be empty on a job an
 * older gateway persisted, so the total falls back to the four steps every
 * launch has (preflight, provision, sign-in, connect).
 */
export function deployProgress(job: Pick<LaunchJob, 'steps'>): { current: number; total: number } {
  const total = job.steps.length || 4
  const current = Math.min(total, job.steps.filter((s) => s.state === 'done').length + 1)
  return { current, total }
}

/**
 * The name of the step under way, so "Step 2 of 4" says what step 2 is. The
 * active step if the record marks one, else the first not yet done; the empty
 * string when the record carries no steps (an older gateway), in which case the
 * counter stands alone. The label is the gateway's own wording for the step.
 */
export function deployStepLabel(job: Pick<LaunchJob, 'steps'>): string {
  const active = job.steps.find((s) => s.state === 'active') ?? job.steps.find((s) => s.state !== 'done')
  return active?.label ?? ''
}

/**
 * Time since a moment, as a locale-aware "3d 4h" / "2h 10m" / "7m". Used for
 * the age of a launch record (what the gateway can attest) and for how long
 * ago ECS says a task stopped. It is never labelled "uptime": nothing here can
 * see whether anything ran the whole time.
 *
 * A missing or future timestamp renders as the unknown glyph rather than a
 * garbage age.
 */
export function deployAge(atSec: number | null | undefined, nowMs = Date.now()): string {
  if (atSec == null || !Number.isFinite(atSec) || atSec <= 0) return UNKNOWN
  const ms = nowMs - atSec * 1000
  if (ms < 0) return UNKNOWN
  const totalMin = Math.floor(ms / 60_000)
  const days = Math.floor(totalMin / 1440)
  const hours = Math.floor((totalMin % 1440) / 60)
  const minutes = totalMin % 60
  if (days >= 1) return fmtDuration([[days, 'day'], [hours, 'hour']], { dropZero: true })
  if (hours >= 1) return fmtDuration([[hours, 'hour'], [minutes, 'minute']], { dropZero: true })
  return fmtDuration([[minutes, 'minute']])
}

/** One stat card, copying the member stat vocabulary: a large number over an
 *  11px label, unknown drawn as the en dash. */
export function Stat({ value, label, testid }: { value: string; label: string; testid: string }) {
  return (
    <div className="border border-border rounded-lg px-3 py-2">
      <div className="text-lg font-semibold leading-tight" data-testid={testid}>{value}</div>
      <div className="text-[11px] text-muted">{label}</div>
    </div>
  )
}
