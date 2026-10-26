/**
 * The three chat rows the gateway writes to drive a KAS workflow turn that sends
 * the model no prompt of its own (`kas_workflows/router.py`):
 *
 *   Start workflow `name` in `/path` (launch <token>)      — a dashboard launch
 *   Reconnect workflow runs (reconnect <token>)              — after a restart
 *   [Workflow `name` completed] Kiro Agent's reply follows (observe <token>)
 *
 * They are machinery, not something the person typed, so they render as one
 * quiet notice line instead of a user bubble with a token in it. The shapes are
 * the router's own regexes, so nothing a person types can pass for one unless it
 * carries a 32-hex token too (and even then it only changes how the row looks).
 */
import { AlertTriangle, Info, Workflow, XCircle } from 'lucide-react'

import type { ChatMessage } from '../../../types'
import { fmtElapsed } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'

const LAUNCH_RE = /^Start workflow `([^`\n]*)` in `[^`\n]*` \(launch [0-9a-f]{32}\)\s*$/
const RECONNECT_RE = /^Reconnect workflow runs \(reconnect [0-9a-f]{32}\)\s*$/
const OBSERVE_RE = /^\[Workflow `([^`\n]*)` ([a-z]+)\] .* \(observe [0-9a-f]{32}\)\s*$/

export type KasTurnMarker =
  | { kind: 'launch'; name: string }
  | { kind: 'reconnect' }
  | { kind: 'observe'; name: string; status: string }

export function parseKasTurnMarker(message: Pick<ChatMessage, 'role' | 'content'>): KasTurnMarker | null {
  if (message.role !== 'user' || typeof message.content !== 'string') return null
  const text = message.content
  let m = OBSERVE_RE.exec(text)
  if (m) return { kind: 'observe', name: m[1] || 'workflow', status: m[2] }
  m = LAUNCH_RE.exec(text)
  if (m) return { kind: 'launch', name: m[1] || 'workflow' }
  if (RECONNECT_RE.test(text)) return { kind: 'reconnect' }
  return null
}

function markerText(marker: KasTurnMarker): string {
  switch (marker.kind) {
    case 'launch':
      return i18nT('apps.workflows.kas.marker_launch', { name: marker.name })
    case 'reconnect':
      return i18nT('apps.workflows.kas.marker_reconnect')
    case 'observe':
      if (marker.status === 'completed') return i18nT('apps.workflows.kas.marker_completed', { name: marker.name })
      if (marker.status === 'failed') return i18nT('apps.workflows.kas.marker_failed', { name: marker.name })
      if (marker.status === 'aborted') return i18nT('apps.workflows.kas.marker_stopped', { name: marker.name })
      // A step's note to this chat that woke it (the run goes on).
      if (marker.status === 'update') return i18nT('apps.workflows.kas.marker_update', { name: marker.name })
      if (marker.status === 'waiting') return i18nT('apps.workflows.kas.marker_waiting', { name: marker.name })
      if (marker.status === 'error') return i18nT('apps.workflows.kas.marker_step_error', { name: marker.name })
      return i18nT('apps.workflows.kas.marker_ended', { name: marker.name })
  }
}

export function KasTurnMarkerRow({ marker }: { marker: KasTurnMarker }) {
  return (
    <div
      className="flex items-center gap-2 my-1 text-[12px] text-muted"
      data-testid="kas-turn-marker"
      data-kind={marker.kind}
    >
      <Workflow size={13} className="shrink-0" aria-hidden="true" />
      <span className="truncate min-w-0">{markerText(marker)}</span>
    </div>
  )
}

// ── Event marker rows (gap 10): the gateway appends ONE transcript row for a
//    run end or a step note, as role 'notice' carrying meta.kas_event. L3
//    renders it localized from the structured fields (round4-contract.md). ──

type KasEventSeverity = 'info' | 'success' | 'warning' | 'error'

export interface KasEventOutcome {
  type: 'outcome'
  name: string
  status: 'completed' | 'failed' | 'aborted' | string
  durationSecs: number | null
}

export interface KasEventNote {
  type: 'note'
  name: string
  severity: KasEventSeverity
  message: string
  step: string | null
  agent: string | null
}

export type KasEventMarker = KasEventOutcome | KasEventNote

function readStr(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

/** Parse a `meta.kas_event` run-end / step-note marker, or null (gap 10). */
export function parseKasEventMarker(message: Pick<ChatMessage, 'meta'>): KasEventMarker | null {
  const event = message.meta?.kas_event
  if (!event || typeof event !== 'object') return null
  const e = event as Record<string, unknown>
  const name = readStr(e.name) ?? readStr(e.workflowId) ?? 'workflow'
  if (e.type === 'outcome') {
    return {
      type: 'outcome',
      name,
      status: readStr(e.status) ?? 'completed',
      durationSecs: typeof e.durationSecs === 'number' && Number.isFinite(e.durationSecs) ? e.durationSecs : null,
    }
  }
  if (e.type === 'note') {
    const sev = e.severity
    return {
      type: 'note',
      name,
      severity: sev === 'success' || sev === 'warning' || sev === 'error' ? sev : 'info',
      message: readStr(e.message) ?? '',
      step: readStr(e.step),
      agent: readStr(e.agent),
    }
  }
  return null
}

function outcomeText(marker: KasEventOutcome): string {
  const duration = marker.durationSecs != null ? fmtElapsed(marker.durationSecs * 1000, { wholeSeconds: true }) : null
  switch (marker.status) {
    case 'failed':
      return duration
        ? i18nT('apps.workflows.kas.event_failed', { name: marker.name, duration })
        : i18nT('apps.workflows.kas.event_failed_plain', { name: marker.name })
    case 'aborted':
      return duration
        ? i18nT('apps.workflows.kas.event_stopped', { name: marker.name, duration })
        : i18nT('apps.workflows.kas.event_stopped_plain', { name: marker.name })
    case 'completed':
    default:
      return duration
        ? i18nT('apps.workflows.kas.event_completed', { name: marker.name, duration })
        : i18nT('apps.workflows.kas.event_completed_plain', { name: marker.name })
  }
}

function noteText(marker: KasEventNote): string {
  const body =
    marker.step && marker.agent
      ? i18nT('apps.workflows.kas.event_note', { step: marker.step, agent: marker.agent, name: marker.name, message: marker.message })
      : marker.step
        ? i18nT('apps.workflows.kas.event_note_no_agent', { step: marker.step, name: marker.name, message: marker.message })
        : i18nT('apps.workflows.kas.event_note_no_step', { name: marker.name, message: marker.message })
  if (marker.severity === 'warning')
    return i18nT('apps.workflows.kas.event_note_waiting_prefix', { note: body })
  if (marker.severity === 'error')
    return i18nT('apps.workflows.kas.event_note_error_prefix', { note: body })
  return body
}

export function KasEventMarkerRow({ marker }: { marker: KasEventMarker }) {
  if (marker.type === 'outcome') {
    const failed = marker.status === 'failed'
    return (
      <div
        className={`flex items-start gap-2 my-1 text-[12px] ${failed ? 'text-warn' : 'text-muted'}`}
        data-testid="kas-event-marker"
        data-kind="outcome"
        data-status={marker.status}
      >
        {failed ? (
          <XCircle size={13} className="shrink-0 mt-px text-danger" aria-hidden="true" />
        ) : (
          <Workflow size={13} className="shrink-0 mt-px" aria-hidden="true" />
        )}
        <span className="min-w-0">{outcomeText(marker)}</span>
      </div>
    )
  }
  const Icon = marker.severity === 'error' ? XCircle : marker.severity === 'warning' ? AlertTriangle : Info
  const tone = marker.severity === 'error' ? 'text-danger' : marker.severity === 'warning' ? 'text-warn' : 'text-muted'
  return (
    <div
      className={`flex items-start gap-2 my-1 text-[12px] ${tone}`}
      data-testid="kas-event-marker"
      data-kind="note"
      data-severity={marker.severity}
    >
      <Icon size={13} className="shrink-0 mt-px" aria-hidden="true" />
      <span className="min-w-0 break-words">{noteText(marker)}</span>
    </div>
  )
}
