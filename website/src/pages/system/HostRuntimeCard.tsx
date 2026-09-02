/**
 * Host runtime — read-only WSL2 state of the machine the desktop shell runs on.
 *
 * Served by the `wsl:detect` IPC channel (electron/main.js), which rejects any
 * WebContents the local gateway did not serve: a connection window pointed at a
 * REMOTE gateway renders "unavailable" here rather than the host's distro
 * inventory. The card self-hides where the answer cannot exist — in a plain
 * browser tab (no bridge) and on macOS/Linux (no WSL subsystem).
 *
 * Detection spawns wsl.exe, so this is NOT polled like the rest of the plane.
 * React Query refetches on remount — switching System planes suffices — and
 * otherwise caches for a minute; state flipped mid-session appears on the next
 * visit rather than live.
 */
import { useQuery } from '@tanstack/react-query'
import { Card, CardTitle } from '../../components/ui'
import { electronPlatform } from '../../lib/electron'
import { i18nT } from '../../i18n/t'

interface WslDistro {
  name: string
  /** Stable enum — never branch on the raw localized text. */
  state: 'running' | 'stopped' | 'unknown'
  /** The OS's own word for the state, in the OS language, for display. */
  stateLabel: string
  version: number
  isDefault: boolean
}

interface WslDetectResult {
  available: boolean
  distros: WslDistro[]
  defaultDistro: string | null
  error?: string
  reason?: string
}

/** Present only in the desktop shell (see electron/preload.js). */
const resolveDetect =
  (): (() => Promise<WslDetectResult>) | undefined =>
    (window as { wslAPI?: { detect: () => Promise<WslDetectResult> } }).wslAPI?.detect

export default function HostRuntimeCard() {
  const detect = resolveDetect()
  const isWindowsShell = electronPlatform() === 'win32'
  const { data, isError } = useQuery({
    queryKey: ['wsl-detect'],
    queryFn: () => detect!(),
    enabled: !!detect && isWindowsShell,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  })

  if (!detect || !isWindowsShell) return null

  const rows: Array<{ label: string; value: string }> = []
  if (isError) {
    rows.push({
      label: i18nT('pages.servicesTab.wsl2'),
      value: i18nT('pages.servicesTab.wsl2_unavailable'),
    })
  } else if (data === undefined) {
    // In flight, not answered yet: the plane's own "value unknown" placeholder.
    // Claiming "unavailable" here would flash a false negative on every mount.
    rows.push({ label: i18nT('pages.servicesTab.wsl2'), value: '—' })
  } else if (!data.available) {
    rows.push({
      label: i18nT('pages.servicesTab.wsl2'),
      value: i18nT('pages.servicesTab.wsl2_unavailable'),
    })
  } else {
    if (data.defaultDistro) {
      rows.push({
        label: i18nT('pages.servicesTab.default_distro'),
        value: data.defaultDistro,
      })
    }
    for (const d of data.distros) {
      rows.push({
        label: d.name,
        value:
          d.state === 'unknown'
            ? i18nT('pages.servicesTab.status_unknown')
            : d.stateLabel || d.state,
      })
    }
    // WSL present but nothing on version 2 (e.g. a WSL1-only machine): without
    // this row the card collapses to a bare title, which reads as a glitch.
    if (data.distros.length === 0) {
      rows.push({
        label: i18nT('pages.servicesTab.wsl2'),
        value: i18nT('pages.servicesTab.wsl2_no_distros'),
      })
    }
  }

  return (
    <Card>
      <CardTitle>{i18nT('pages.servicesTab.host_runtime')}</CardTitle>
      {/* Rows as a description list: each distro IS a term/definition pair,
          which screen readers announce as such — a styled div list does not. */}
      <dl className="mb-4">
        {rows.map((row, i) => (
          <div
            key={`${row.label}-${i}`}
            className="flex justify-between gap-3 py-1.5 border-b border-border text-[12.5px] last:border-b-0"
          >
            <dt className="text-muted shrink-0">{row.label}</dt>
            <dd className="text-text-strong font-mono text-right break-words">{row.value}</dd>
          </div>
        ))}
      </dl>
    </Card>
  )
}
