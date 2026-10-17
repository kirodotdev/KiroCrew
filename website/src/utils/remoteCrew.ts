/**
 * Constants the Remote Crew surfaces share with the backend. They live here,
 * not in `api/client.ts`, because most test files replace that module with a
 * hand-written mock and a new export there breaks every one of them.
 */
import type { LaunchJobStatus } from '../api/client'

/** Warm-set cap when the gateway reports none — named after the Python
 *  constant it pins, WARM_SET_CAP_AUTO_CEILING in
 *  src/kiro_crew/instances/constants.py. (Python's DEFAULT_WARM_SET_CAP is a
 *  different constant: 0, meaning auto.) */
export const WARM_SET_CAP_AUTO_CEILING = 10

/** Provisioner id of the built-in EC2 launcher. Mirrors
 *  BUILTIN_PROVISIONER_ID in src/kiro_crew/platform/interfaces.py. */
export const BUILTIN_PROVISIONER_ID = 'aws_ec2'

/** Transports that reach the crew through an SSM port-forward and therefore
 *  address it by `ssm_target` + AWS profile/region rather than `ssh_host`.
 *  Mirrors SSM_TRANSPORT_METHODS in src/kiro_crew/instances/registry.py. */
export const usesSsmTransport = (inst: { connection_method?: string }): boolean =>
  inst.connection_method === 'ssm' || inst.connection_method === 'fargate'

/** Whether a crew has a dashboard to embed. A fargate crew exposes a turn
 *  API on its forwarded port and nothing else: no dashboard, no token. So it
 *  gets no switcher tab, no pane, and no auto-connect; its card shows the
 *  turn URL instead. */
export const hasDashboardPane = (inst: { connection_method?: string }): boolean =>
  inst.connection_method !== 'fargate'

/** An ECS target as the crew list should print it. The full form is
 *  `ecs:<cluster>_<task id>_<runtime id>`, two 32-hex ids after the cluster,
 *  so two tasks in one cluster differ only far to the right and the row's
 *  CSS truncation (which cuts from the right) shows them identically. Keep
 *  the cluster, the head of the task id and the tail of the runtime id with
 *  an ellipsis between, so the distinguishing digits stay visible; the caller
 *  puts the full target in a `title`. Anything that is not an ECS target
 *  (an `i-...` instance id, an empty string) passes through unchanged. */
/** Horizontal ellipsis, as an escape so the source stays ASCII. */
const ELLIPSIS = '\u2026'

export function shortenEcsTarget(target: string): string {
  // The `ecs:` prefix rides inside the first capture. The cluster is captured
  // greedily, while the fixed-width task/runtime suffixes anchor the split.
  // The ellipsis is a named constant so the template below holds no letters in
  // its raw text: the i18n lint reads a lettered quasi as user copy.
  const m = /^(ecs:[A-Za-z0-9][A-Za-z0-9_-]{0,254})_([0-9a-f]{32})_([0-9a-f]{32}-[0-9]{1,20})$/.exec(target)
  if (!m) return target
  const [, clusterWithPrefix, taskId, runtimeId] = m
  return `${clusterWithPrefix}_${taskId.slice(0, 8)}${ELLIPSIS}${runtimeId.slice(-11)}`
}

/** Provisioner id of the Fargate lane. Mirrors FARGATE_PROVISIONER_ID in
 *  src/kiro_crew/platform/defaults.py. A launch on this lane records the
 *  task's ARN where the EC2 lane records an instance id. */
export const FARGATE_PROVISIONER_ID = 'aws_fargate'

/** The launch statuses the user is still waiting on: not yet a switchable
 *  crew, and worth re-reading. One list for both panels that show launches,
 *  so a status the gateway adds is classified once. Reached only through
 *  `launchIsInFlight`, the one spelling both panels use. */
const IN_FLIGHT_LAUNCH_STATUSES: ReadonlySet<LaunchJobStatus> = new Set<LaunchJobStatus>([
  'pending',
  'running',
  'awaiting_signin',
])

export function launchIsInFlight(status: LaunchJobStatus): boolean {
  return IN_FLIGHT_LAUNCH_STATUSES.has(status)
}
