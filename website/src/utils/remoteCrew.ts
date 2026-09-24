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

/** The method a record with no explicit `connection_method` means. Mirrors the
 *  gateway, which reads `(inst.connection_method or "ssh").strip().lower()` in
 *  `_resolve_transport` (src/kiro_crew/instances/ssh_tunnel_manager.py). An
 *  ABSENT method legitimately means ssh; an UNRECOGNISED one does not, and
 *  conflating the two is the defect `transportPresentation` exists to close. */
export const DEFAULT_CONNECTION_METHOD = 'ssh'

/** Normalise a record's method the way the gateway does, so the dashboard and
 *  the gateway never disagree about which transport a record names. */
export function normalizeConnectionMethod(method?: string): string {
  return (method ?? '').trim().toLowerCase() || DEFAULT_CONNECTION_METHOD
}

/**
 * How one connection method presents itself on every Remote Crew surface.
 *
 * One table, read by the badge, the badge's hover hint, the card's address
 * line and both diagnostics handoffs. It replaces four independent chains of
 * conditionals whose last arm was `ssh`, which made every method they did not
 * name render as SSH — silently, and inconsistently between surfaces: the same
 * fargate failure reported `ssm` from the settings panel and `ssh` from the
 * viewport.
 *
 * It deliberately carries no i18n key. The key-reference gate
 * (`website/scripts/check-i18n-keys.mjs`) can only check a key it resolves
 * statically, and its dynamic-site baseline is ratchet-DOWN only, so
 * `i18nT(presentation.labelKey)` would have to raise a count the gate says to
 * never raise. The copy therefore stays at the call site as literal keys, and a
 * test pins that call site's method set against `PRESENTED_CONNECTION_METHODS`
 * so copy cannot silently go missing for a new transport.
 */
export interface TransportPresentation {
  /** The normalised method this presentation describes. */
  readonly method: string
  /** Which `Instance` field addresses this crew, `null` when none does. An
   *  unmapped method prints no address rather than an empty `ssh_host`. */
  readonly addressField: 'ssh_host' | 'ssm_target' | null
  /** The transport name the failure report keys its repair steps by. Always
   *  the method's own name, so an unmapped method reports itself and gets no
   *  other transport's repair steps. */
  readonly reportTransport: string
  /** False when the method is absent from the table below. */
  readonly mapped: boolean
}

/** The presentation table. Adding a method to the gateway's
 *  `CONNECTION_METHODS` without adding a row here is what the exhaustiveness
 *  test in `remoteCrewTransportPresentation.test.ts` fails on. */
const TRANSPORT_PRESENTATIONS = {
  ssh: { addressField: 'ssh_host' },
  ssm: { addressField: 'ssm_target' },
  fargate: { addressField: 'ssm_target' },
} as const

/** The methods this mapping covers. Pinned by a test against the gateway's
 *  registered set, so a fourth transport cannot ship unmapped. */
export const PRESENTED_CONNECTION_METHODS: readonly string[] = Object.keys(TRANSPORT_PRESENTATIONS)

/** The presented methods as a type, so a table keyed by it (the badge copy in
 *  `pages/settings/transportCopy.ts`) fails to COMPILE when a transport is added
 *  here without its copy. */
export type PresentedConnectionMethod = keyof typeof TRANSPORT_PRESENTATIONS

/** Total: every method resolves, including one this build has never heard of. */
export function transportPresentation(inst: { connection_method?: string }): TransportPresentation {
  const method = normalizeConnectionMethod(inst.connection_method)
  const row: { readonly addressField: 'ssh_host' | 'ssm_target' } | undefined = (
    TRANSPORT_PRESENTATIONS as Record<string, { readonly addressField: 'ssh_host' | 'ssm_target' }>
  )[method]
  if (row === undefined) {
    // Visibly unmapped, never a sibling's identity: no address field, and a
    // report that names the method it actually is.
    return { method, addressField: null, reportTransport: method, mapped: false }
  }
  return { method, addressField: row.addressField, reportTransport: method, mapped: true }
}

/** The address a crew's card should print, through the mapping's own field
 *  choice. Empty for a method with no address field, rather than an empty
 *  `ssh_host` that reads as a configured-but-blank SSH crew. */
export function transportTarget(inst: {
  connection_method?: string
  ssh_host?: string
  ssm_target?: string
}): string {
  const { addressField } = transportPresentation(inst)
  if (addressField === null) return ''
  return (addressField === 'ssm_target' ? inst.ssm_target : inst.ssh_host) ?? ''
}

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
