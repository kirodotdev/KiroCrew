/**
 * Pure derivations behind the agent-desktop install card in Settings → Computer
 * Use.
 *
 * Kept out of the component so each fact the card shows (is a job running, is the
 * install current, which button to offer) is one function of the gateway's answer
 * plus this tab's own in-flight request, testable without rendering.
 */
import type { AgentDesktopPermission, AgentDesktopStatus } from '../../api/client'

/** Poll cadence while an install job runs. Idle needs no poll: nothing changes
 *  the status on its own except a job, and window focus re-reads it anyway. */
export const AGENT_DESKTOP_POLL_MS = 1500

/** How the installed binary compares to the version this build pins. */
export type VersionState = 'missing' | 'current' | 'outdated' | 'unknown'

export function versionState(data: AgentDesktopStatus): VersionState {
  if (!data.installed) return 'missing'
  if (!data.version) return 'unknown'
  return data.version === data.pinned ? 'current' : 'outdated'
}

/** True while the gateway reports a running job, or this tab's POST is unanswered. */
export function jobRunning(data: AgentDesktopStatus | undefined, pending: boolean): boolean {
  return pending || data?.job?.state === 'running'
}

/** The status query's refetch interval: poll only while a job runs. */
export function statusPollInterval(data: AgentDesktopStatus | undefined, pending: boolean): number | false {
  return jobRunning(data, pending) ? AGENT_DESKTOP_POLL_MS : false
}

/** Which install action to offer, or `null` when the pinned version is installed.
 *  `unknown` (installed, version unreadable) offers a reinstall: the card cannot
 *  say the binary is the pinned one, and reinstalling it is harmless. */
export type InstallAction = 'install' | 'update' | 'reinstall'

export function installAction(data: AgentDesktopStatus): InstallAction | null {
  const v = versionState(data)
  if (v === 'missing') return 'install'
  if (v === 'outdated') return 'update'
  if (v === 'unknown') return 'reinstall'
  return null
}

/** True when any grant is not `granted`, so the permission guide is worth offering. */
export function permissionsIncomplete(data: AgentDesktopStatus): boolean {
  const p = data.permissions
  return p?.accessibility !== 'granted' || p?.screen_recording !== 'granted'
}

/** Badge variant per permission state. */
export function permissionVariant(state: AgentDesktopPermission | undefined): 'ok' | 'warn' | 'muted' {
  if (state === 'granted') return 'ok'
  if (state === 'denied') return 'warn'
  return 'muted'
}

/** The last job's failure to show, or `null`. Hidden while a new job runs. */
export function failedJob(data: AgentDesktopStatus, pending: boolean): { error: string | null; logTail: string } | null {
  if (jobRunning(data, pending)) return null
  const job = data.job
  if (!job || job.state !== 'failed') return null
  return { error: job.error, logTail: job.log_tail ?? '' }
}
