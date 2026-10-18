import { useContext, useMemo } from 'react'
import { QueryClient, QueryClientContext, useQuery } from '@tanstack/react-query'
import {
  api,
  type ConfigSetHintsV1,
  type ConfigSetShellId,
  type KiroPrerequisiteStatus,
} from '../api/client'

/** What the gateway host is, for copy that names an OS feature by its real name. */
export type GatewayPlatform = 'darwin' | 'windows' | 'other'

/**
 * Classify a raw `process.platform`-shaped string into the three arms our copy has.
 *
 * Exported because not every reveal action runs on the gateway: Mochi's reveal is
 * an IPC send its Electron main process performs, so that surface reads the SHELL's
 * platform and must classify it by the same rule rather than a second one that could
 * disagree about what counts as Windows.
 *
 * Everything unrecognised collapses to `'other'`, deliberately. The gateway endpoint
 * reports the sentinel `'gateway'` to a non-owner dashboard user and to a probe that
 * could not run, an absent Electron bridge reports nothing at all, and Linux has no
 * single file manager to name — all three want the same generic wording, because
 * naming an application we are not sure exists is the failure mode worth designing
 * out.
 */
export function classifyPlatform(raw: string | undefined | null): GatewayPlatform {
  const platform = raw ?? ''
  // The gateway sends a human DISPLAY label, not `sys.platform`: the prerequisite
  // snapshot reports `"macOS"` and `"Windows"` (see `_platform_label` in
  // kiro_prerequisite.py), while Mochi's shell hands us raw `process.platform`
  // values (`"darwin"`, `"win32"`). BOTH spellings have to classify the same way
  // or an affordance is worded and gated by which surface asked: matching only
  // `"darwin"` collapsed every macOS gateway to `'other'`, so a Mac user was
  // offered the generic "Show in file manager" instead of Finder.
  const lower = platform.toLowerCase()
  if (lower === 'darwin' || lower === 'macos') return 'darwin'
  // A lowercase-only `startsWith('win')` would likewise collapse the display
  // label to `'other'` and mis-gate every Windows-only affordance.
  if (lower.startsWith('win')) return 'windows'
  return 'other'
}

/** The query the prerequisite gate owns; this hook subscribes without driving it. */
const PREREQUISITE_QUERY_KEY = ['kiro-prerequisite'] as const

/**
 * Read-only stand-in for a tree that has no `QueryClientProvider`.
 *
 * Mochi's Electron windows and the popout frames mount with a bare `createRoot`,
 * and `useQuery` throws "No QueryClient set" there — which would turn a component
 * that merely wants to word a label correctly into one that cannot render at all.
 * This cache stays empty and the query below never fetches, so a caller in such a
 * tree resolves to `'other'`: the same generic wording an unreadable platform gets.
 */
const ORPHAN_TREE_CACHE = new QueryClient()

/**
 * The platform of the GATEWAY host, not of the browser.
 *
 * The browser's OS is the wrong signal for anything the gateway executes:
 * `/api/reveal` shells out on the gateway, so a dashboard opened from a Mac
 * against a Linux gateway must not name Finder. The install command has the same
 * property and is resolved server-side for the same reason.
 *
 * A pure reader: `enabled: false` means this hook never fetches. The
 * prerequisite gate wraps the whole dashboard and owns that query, so the value
 * is cached before any page mounts, and this subscription re-renders when the
 * gate refreshes it.
 *
 * The `queryFn` is nevertheless a real fetch, and must stay one. React Query
 * keeps a single options object per query, so whichever observer mounted last
 * decides what a refetch driven through the CLIENT — rather than through an
 * observer — runs. A fetch-less `queryFn` registered here therefore surfaces as
 * `Missing queryFn` on the gate's query the next time something invalidates it
 * (the token-refresh scheduler does), which strands the whole dashboard behind
 * the gate's error screen. Reading the latched state is the cheap mode, so a
 * refetch that resolves through these options costs no `kiro-cli` spawn.
 *
 * See `classifyPlatform` for why anything unrecognised is generic wording.
 */
export function useGatewayPlatform(): GatewayPlatform {
  return classifyPlatform(usePrerequisiteStatus()?.platform)
}

/**
 * The shell prefixes the gateway resolved for `kirocrew config set`, or `undefined`
 * when the cached status carries none a command may be built from.
 *
 * Absent from a gateway older than the field, from a non-owner's redacted status, and
 * before the gate has loaded; each is the legacy-command case, not an error. A payload
 * is trusted only when it is exactly the V1 shape: version 1, the one encoding this UI
 * writes, at least one shell, every id recognised and unique, and every prefix a
 * single-line non-empty string. Anything else reads as absent, so a malformed or
 * future-versioned payload degrades to the legacy command rather than to a command
 * the shell would mis-run.
 */
export function useConfigSetHintsV1(): ConfigSetHintsV1 | undefined {
  const hints = usePrerequisiteStatus()?.config_set_hints
  return useMemo(() => parseConfigSetHintsV1(hints), [hints])
}

const CONFIG_SET_SHELL_IDS: readonly ConfigSetShellId[] = ['posix', 'powershell', 'cmd']

function parseConfigSetHintsV1(raw: unknown): ConfigSetHintsV1 | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined
  const { version, encoding, shells } = raw as Record<string, unknown>
  if (version !== 1 || encoding !== 'utf-8-base64') return undefined
  if (!Array.isArray(shells) || shells.length === 0) return undefined
  const seen = new Set<ConfigSetShellId>()
  const rows: ConfigSetHintsV1['shells'] = []
  for (const row of shells) {
    if (typeof row !== 'object' || row === null) return undefined
    const { id, prefix } = row as Record<string, unknown>
    if (typeof prefix !== 'string' || prefix.trim() === '' || /[\r\n]/.test(prefix)) return undefined
    if (!CONFIG_SET_SHELL_IDS.includes(id as ConfigSetShellId)) return undefined
    const shellId = id as ConfigSetShellId
    if (seen.has(shellId)) return undefined
    seen.add(shellId)
    rows.push({ id: shellId, prefix })
  }
  return { version: 1, encoding: 'utf-8-base64', shells: rows }
}

/**
 * The cached prerequisite status, read without fetching. Every reader in this module
 * goes through here so each registers the same options on the shared query; see
 * `useGatewayPlatform` for why the `queryFn` stays a real fetch.
 */
function usePrerequisiteStatus(): KiroPrerequisiteStatus | undefined {
  const provided = useContext(QueryClientContext)
  const { data } = useQuery<KiroPrerequisiteStatus>(
    {
      queryKey: PREREQUISITE_QUERY_KEY,
      queryFn: () => api.kiroPrerequisite(false),
      enabled: false,
    },
    provided ?? ORPHAN_TREE_CACHE,
  )
  return data
}
