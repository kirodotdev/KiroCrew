/**
 * Journal a remote crew's tunnel failure so the error surfaces that render it can
 * hand the agent real context instead of one truncated sentence.
 *
 * ## Why these failures need their own recorder
 *
 * `api/client.ts`'s `apiFailure` journals every non-2xx, which already covers a
 * failed `POST /api/instances/{id}/connect`. But the two surfaces a user actually
 * sits in front of read the failure from a **200**: the 60-second instances poll
 * carries `status.error` and the diagnosis ladder's verdict, and
 * `POST …/diagnose` answers 200 whether the verdict is healthy or not. Neither
 * passes through `apiFailure`, so without this the journal has no entry and the
 * hand-off degrades to the bare display string — dropping the `probes` ladder,
 * which is the one part that says WHICH link in the chain is broken.
 *
 * ## De-duplication is required, not an optimization
 *
 * The journal is 20 entries deep and the poll repeats every 60 seconds. Recording
 * unconditionally would evict every other error in the tab within twenty minutes
 * of one persistently-down crew, so a report is written only when an instance's
 * failure SIGNATURE changes. A crew that recovers and breaks again the same way
 * is re-reported, because the signature clears on recovery.
 */

import { recordError, type ErrorReport } from './errorReport'
import type { InstanceTunnelStatus } from '../api/client'

/**
 * Last-reported signature per instance id AND stage, with the report it produced.
 * Module state, per tab, matching the journal it feeds: persisting it would
 * suppress the first report after a reload, which is exactly when a user is
 * looking at the failure.
 *
 * The stage belongs in the KEY, not only in the signature. Two surfaces report
 * independently for one crew — the Settings row always as `connect`, the viewport
 * as either stage depending on its pane watchdog — so a per-id entry is shared
 * state they overwrite in turn. Each write then reads the other's signature, sees
 * a mismatch, and journals again, so the two stages ping-pong a fresh entry into
 * a 20-deep journal on every poll. Keying by stage gives each surface its own
 * entry, which is what makes de-duplication hold at all.
 *
 * The report is cached alongside the signature so a suppressed repeat still has
 * one to hand back — the caller must never be left without a diagnostic just
 * because we declined to journal a duplicate.
 */
const _reported = new Map<string, { signature: string; report: ErrorReport }>()

/** De-dup identity: one entry per surface, not one per crew. See `_reported`. */
function dedupKeyOf(id: string, stage: InstanceFailureStage): string {
  return `${id}|${stage}`
}

/** What the caller was looking at when it decided this was a failure. */
export type InstanceFailureStage =
  /** The tunnel/diagnosis says broken. */
  | 'connect'
  /** The tunnel claims connected but the embedded dashboard never loaded. */
  | 'pane_load'

/**
 * Render the diagnosis ladder as the ordered chain it is.
 *
 * The probe list is the payload's most actionable part: `ssh=ok` followed by
 * `remote_dashboard=FAILED` names a different repair than `ssh=FAILED` does, and
 * a reader who only sees the summary sentence cannot tell them apart. Written as
 * a chain so the first FAILED entry reads as the broken link.
 */
function describeProbes(probes: { name: string; ok: boolean }[] | undefined): string {
  if (!probes?.length) return ''
  return 'probes: ' + probes.map(p => `${p.name}=${p.ok ? 'ok' : 'FAILED'}`).join(' -> ')
}

/**
 * The message that identifies this failure to a reader.
 *
 * A diagnosis is only consulted when its verdict is negative: the stored result
 * is the last ladder RUN, so a stale `ok` would otherwise supply "All checks
 * passed" as the text of a failure.
 */
function failureMessageOf(
  status: InstanceTunnelStatus | undefined,
  fallbackMessage: string,
): string {
  const diagnosis = status?.diagnosis
  return status?.error || (diagnosis && !diagnosis.ok ? diagnosis.reason : '') || fallbackMessage
}

/**
 * Record one instance failure and return the report to hand to `AskAgentButton`.
 *
 * The report is returned rather than looked back up by message, and that is the
 * point: `findReport` matches on message TEXT, which is not an identity. Two crews
 * failing the same way — two stock installs whose hosts are both unreachable
 * produce byte-identical prose — would resolve to whichever was journaled last,
 * so the prompt would carry the other crew's name, transport and probe chain. It
 * also made the hand-off depend on the report still being IN the 20-deep journal.
 * Passing the object removes both: nothing is matched and nothing has to survive.
 *
 * Returns null when there is no failure to describe, having cleared the de-dup
 * signature so a later recurrence is journaled again.
 */
export function reportInstanceFailure(input: {
  id: string
  name: string
  /** `ssh` / `ssm` — which transport's repair steps apply. */
  transport: string
  status: InstanceTunnelStatus | undefined
  stage: InstanceFailureStage
  /** Display string the surface is showing, used when the status carries no error. */
  fallbackMessage: string
}): ErrorReport | null {
  const { id, name, transport, status, stage, fallbackMessage } = input
  const diagnosis = status?.diagnosis
  const message = failureMessageOf(status, fallbackMessage)
  const key = dedupKeyOf(id, stage)
  if (!message) {
    _reported.delete(key)
    return null
  }
  // Only a NEGATIVE verdict describes THIS failure. `status.diagnosis` is the last
  // ladder RUN, stored on the tunnel status, so a crew diagnosed healthy and broken
  // later still carries that `ok` verdict and its all-passing probe chain. Attaching
  // it would tell the agent the crew is fine in the same breath as asking why it is
  // broken — the backend promotes the verdict under the same condition and for the
  // same reason. One predicate, used by every consumer below, so the code and the
  // text cannot disagree about which verdict is current.
  const failing = diagnosis && !diagnosis.ok ? diagnosis : undefined
  const code = failing?.code
  const detail = [
    `crew: ${name} (${id})`,
    `transport: ${transport}`,
    `tunnel state: ${status?.state ?? 'unknown'}`,
    `stage: ${stage}`,
    failing ? `diagnosis: ${failing.code} — ${failing.reason}` : '',
    describeProbes(failing?.probes),
  ]
    .filter(Boolean)
    .join('\n')
  // The signature is derived from the DETAIL, so every input that reaches the
  // report is covered by construction. Enumerating fields here instead would
  // drift the moment a detail line is added: an input outside the signature makes
  // a de-dup hit hand back the cached report with the old value, and the probe
  // ladder — the one part that names WHICH link is broken — is exactly the field
  // that changes while the code and the message stay identical.
  const signature = [status?.state ?? '', code ?? '', message, detail].join('|')
  const seen = _reported.get(key)
  // A de-dup hit still returns the report it suppressed, so the caller is never
  // left without one. Re-journaling is what we avoid; withholding the diagnostic
  // is not.
  if (seen?.signature === signature) return seen.report
  const report = recordError({
    source: 'system',
    message,
    // The ladder's verdict, not an HTTP code: `ssh_unreachable` and `remote_down`
    // are what distinguish the repairs, and they are stable strings.
    code,
    detail,
  })
  _reported.set(key, { signature, report })
  return report
}

/** Test seam — the de-dup map is module state. */
export function __resetInstanceFailuresForTests(): void {
  _reported.clear()
}
