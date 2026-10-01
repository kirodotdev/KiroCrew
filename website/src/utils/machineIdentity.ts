/**
 * Which machine an `/api/system` frame describes, reduced to the two short
 * lines the topbar metrics card shows under its readings.
 *
 * Every dashboard tab polls its OWN gateway's `/api/system`: the local tab
 * reads this machine, and each remote instance tab is that instance's own
 * dashboard reading its own host. The cards look identical, so without a name
 * a reader cannot tell which machine an 87% disk belongs to.
 *
 * The inputs come from the static system info every frame is seeded with
 * (`hostname`, `os`, `cpu_count`), but `api.system()` is untyped, so each is
 * proven before use and a missing one simply drops out of the result.
 *
 * Its own module rather than a member of `App.tsx` so the test can reach it
 * without importing the app root (see `metricColor`).
 */

export type MachineIdentity = {
  /** The hostname's first DNS label, or the whole name for an IP literal. Empty when unknown. */
  host: string
  /** The hostname exactly as reported, for the tooltip. Empty when unknown. */
  fullHost: string
  /** The OS family's display name, e.g. "macOS". Empty when unknown. */
  os: string
  /** Logical CPU count. 0 when unknown. */
  cores: number
}

// `platform.system()` values whose display name differs from the raw value.
// Linux and Windows already read as their own names.
const OS_DISPLAY_NAMES: Readonly<Record<string, string>> = { Darwin: 'macOS' }

// A dotted-quad or any IPv6 form: the first "label" of an address is not a name.
const IP_LITERAL = /^(\d{1,3}(\.\d{1,3}){3}|.*:.*)$/

/** The first DNS label of a hostname; an IP literal is returned whole. */
export function shortHostname(hostname: string): string {
  const name = hostname.trim()
  if (!name || IP_LITERAL.test(name)) return name
  return name.split('.')[0] || name
}

/**
 * The display name of the OS family in the backend's `os` field, which is
 * `f"{platform.system()} {platform.release()}"` (e.g. "Darwin 25.0.0").
 * The release is dropped: the card names the machine, it does not inventory it.
 */
export function osDisplayName(os: string): string {
  const family = os.trim().split(/\s+/)[0] ?? ''
  return OS_DISPLAY_NAMES[family] ?? family
}

/** The identity to show, or null when the frame names nothing at all. */
export function machineIdentity(hostname: unknown, os: unknown, cpuCount: unknown): MachineIdentity | null {
  const fullHost = typeof hostname === 'string' ? hostname.trim() : ''
  const osName = typeof os === 'string' ? osDisplayName(os) : ''
  const cores = typeof cpuCount === 'number' && Number.isInteger(cpuCount) && cpuCount > 0 ? cpuCount : 0
  if (!fullHost && !osName && !cores) return null
  return { host: shortHostname(fullHost), fullHost, os: osName, cores }
}
