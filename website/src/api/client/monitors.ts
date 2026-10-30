/**
 * Session watch loops: auto-nudge goal loops and structured monitors, listed
 * in aggregate and per slot, with create/update/stop/clear/restart.
 */

import type { AutoNudgeListResponse } from '../../components/autoNudgeLoop'
import type { ClientTransport } from './transport'

export type MonitorWrite = {
  slot_key?: string
  kind?: 'github_pull_request'
    | 'gitlab_merge_request'
    | 'azure_devops_pull_request'
    | 'bitbucket_pull_request'
  objective?: 'review_ready'
  target?: string
  cadence_secs?: number
  max_runtime_secs?: number
  max_agent_turns?: number
  max_tokens?: number
  max_provider_errors?: number
  wake_instructions?: string
}

export type MonitorResponse = { ok: true; monitor: unknown }

export function createMonitorsEndpoints({ post, patch, j, jfetch: fetch }: ClientTransport) {
  const loops = {
    /** All goal loops across sessions — every record the service holds, ACTIVE
     *  OR STOPPED (a stopped loop keeps `active: false` + `stopped_reason`, which
     *  is how a surface can say WHY a patrol went quiet). Returns
     *  `{enabled:false, loops:[]}` when the auto-nudge feature flag is off, so
     *  callers need no flag check. */
    autonudgeList: (): Promise<AutoNudgeListResponse> =>
      fetch('/api/autonudge').then(j),
    autonudgeForSlot: (slot: string): Promise<{ enabled: boolean; loop: unknown | null }> =>
      fetch('/api/autonudge/slot/' + encodeURIComponent(slot)).then(j),
    /** Pause (`active: false`) or resume (`active: true`) a goal loop in place.
     *  The body carries ONLY `active`: a pause is not a save, and the service
     *  records a pause as `stopped_reason: "manual"`. A resume on a loop a bound
     *  stopped resets only the spent bound's counter (the popover's Play does the
     *  same write), so nothing has to be raised. Returns the written record. */
    autonudgeSetActive: (id: string, active: boolean): Promise<{ loop: unknown }> =>
      patch('/api/autonudge/' + encodeURIComponent(id), { active }).then(j) as Promise<{ loop: unknown }>,
    /** Structured monitor records include terminal outcomes for inspection. */
    monitorsList: (): Promise<{ enabled: boolean; monitors: unknown[] }> =>
      fetch('/api/monitors').then(j),
    /** `max_runtime_ceiling_secs` is the LIVE operator ceiling
     *  (`monitoring.max_runtime_secs`), which a default install sets far below the
     *  contract's absolute maximum; the popover bounds its runtime input by it. */
    monitorForSlot: (slot: string): Promise<{
      enabled: boolean
      monitor: unknown | null
      max_runtime_ceiling_secs?: number
    }> =>
      fetch('/api/monitors/slot/' + encodeURIComponent(slot)).then(j),
    monitorCreate: (body: Required<MonitorWrite>): Promise<MonitorResponse> =>
      post('/api/monitors', body).then(j) as Promise<MonitorResponse>,
    monitorUpdate: (id: string, body: MonitorWrite): Promise<MonitorResponse> =>
      patch('/api/monitors/' + encodeURIComponent(id), body).then(j) as Promise<MonitorResponse>,
    monitorStop: (id: string): Promise<MonitorResponse> =>
      post('/api/monitors/' + encodeURIComponent(id) + '/stop').then(j) as Promise<MonitorResponse>,
    /** Remove an already-STOPPED monitor's record. `monitorStop` retains its
     *  outcome for inspection, and a retained stop refuses a re-arm, so this is
     *  the only way the session's slot is freed to watch a different subject --
     *  `monitorRestart` revives the same one. Irreversible; the response carries
     *  `monitor: null`, which is how every read here spells "nothing armed". */
    monitorClear: (id: string): Promise<MonitorResponse> =>
      post('/api/monitors/' + encodeURIComponent(id) + '/clear').then(j) as Promise<MonitorResponse>,
    monitorRestart: (id: string): Promise<MonitorResponse> =>
      post('/api/monitors/' + encodeURIComponent(id) + '/restart').then(j) as Promise<MonitorResponse>,
  }

  return { loops }
}
