// A small status pill for a run (review thread).
//
// One distinct treatment per backend status, so the state reads at a glance in
// the list and the live header: running (accent, a pulsing dot), done (ok),
// error (danger), cancelled (muted), interrupted (warn). When a cancel has been
// requested on a still-running run the pill flips to a muted "Cancelling" — the
// backend cancels COOPERATIVELY, so this is an in-flight state, not "stopped".
import type { RunStatus } from '../lib/types'
import { i18nT } from '../../../i18n/t'

/** Per-status pill metadata. Class strings are static (not interpolated) so
 * Tailwind's content scan keeps them.
 *
 * The label is held as a KEY, not a translated string: this table is
 * module-level, so it evaluates once at import time — before any language
 * switch — and a resolved string here would freeze the first locale for the
 * process. The key is resolved per render instead. */
/** Label keys as full literals in one indexable map, so the key-resolution
 *  gate can verify each one exists. Indexed at the call site, not read off a
 *  local — that indirection is what made these sites unverifiable. */
const STATUS_LABEL_KEY: Record<RunStatus | 'cancelling', string> = {
  running: 'apps.codeReviewSage.components.runStatusPill.running',
  done: 'apps.codeReviewSage.components.runStatusPill.done',
  error: 'apps.codeReviewSage.components.runStatusPill.error',
  cancelled: 'apps.codeReviewSage.components.runStatusPill.cancelled',
  interrupted: 'apps.codeReviewSage.components.runStatusPill.interrupted',
  cancelling: 'apps.codeReviewSage.components.runStatusPill.cancelling',
}

const STATUS_META: Record<RunStatus, { cls: string; dot: string; pulse?: boolean }> = {
  running: {
    cls: 'bg-accent-subtle text-accent border-accent',
    dot: 'bg-accent',
    pulse: true,
  },
  done: {
    cls: 'bg-ok-subtle text-ok border-ok',
    dot: 'bg-ok',
  },
  error: {
    cls: 'bg-danger-subtle text-danger border-danger',
    dot: 'bg-danger',
  },
  cancelled: {
    cls: 'bg-card text-muted border-border',
    dot: 'bg-muted',
  },
  interrupted: {
    cls: 'bg-warn-subtle text-warn border-warn',
    dot: 'bg-warn',
  },
}

/** The muted "Cancelling" treatment shown when a running run has a cancel
 * pending — deliberately NOT accent, so it reads as winding down. */
const CANCELLING_META = {
  cls: 'bg-warn-subtle text-warn border-warn',
  dot: 'bg-warn',
} as const

export default function RunStatusPill({
  status, cancelRequested = false,
}: {
  status: RunStatus
  cancelRequested?: boolean
}) {
  const cancelling = cancelRequested && status === 'running'
  const meta = cancelling ? CANCELLING_META : STATUS_META[status]
  const pulse = !cancelling && 'pulse' in meta && meta.pulse
  return (
    <span
      className={
        'inline-flex flex-shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 '
        + `text-[11px] font-medium ${meta.cls}`
      }
    >
      <span
        className={
          `h-1.5 w-1.5 flex-shrink-0 rounded-full ${meta.dot} `
          + (pulse ? 'animate-pulse motion-reduce:animate-none' : '')
        }
        aria-hidden="true"
      />
      {cancelling ? i18nT(STATUS_LABEL_KEY.cancelling) : i18nT(STATUS_LABEL_KEY[status])}
    </span>
  )
}
