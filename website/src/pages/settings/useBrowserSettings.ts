/**
 * Data behind Settings -> Browser: the gateway's browser status, the `browser.*`
 * config section and the Preview tab's origin list, plus the writes.
 *
 * One hook so the main card and the setting rows read the same answers and
 * invalidate the same keys. Reads are React Query; every config write is the
 * generic `PATCH /api/config/kirocrew` with ONE path, so a stale cache in this tab
 * can never clobber a key another client changed (the same reason the Chat panel
 * sends only the changed key).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, type BrowserInstallRequest, type BrowserStatusData, type BrowserTestData } from '../../api/client'
import { fetchDashboardConfig } from '../../api/dashboardConfigQuery'
import { browsePolicyOff } from '../../lib/browsePolicy'

export const BROWSER_STATUS_KEY = ['browserStatus'] as const
const CONFIG_KEY = ['kirocrewConfig'] as const

/** Poll cadence while something is in flight on the gateway (an install job, or
 *  Chrome's Allow prompt waiting for a click), which only a re-read can observe. */
const BUSY_POLL_MS = 2_000
/** Poll cadence at rest: notices a browser connected or dropped elsewhere. */
const IDLE_POLL_MS = 15_000

/** The slice of `GET /api/config/kirocrew` this page reads. Every field optional:
 *  an older gateway, or a config written before the section existed, omits them
 *  and the page falls back to the shipped defaults below. */
export interface BrowserSettingsConfig {
  browser?: {
    choice?: string
    live?: {
      enabled?: boolean
      quality?: string
      max_fps?: number
      mask_passwords?: boolean
    }
    credentials?: {
      enabled?: boolean | null
      domains?: string[]
    }
  }
  dashboard?: {
    browser_local_origins?: string[]
  }
}

/** The shipped defaults of `config/sections_browser.py`, applied when a key is absent. */
export const BROWSER_DEFAULTS = {
  choice: 'auto',
  live: { enabled: true, quality: 'auto', maxFps: 60, maskPasswords: true },
} as const

export const LIVE_QUALITIES = ['auto', 'high', 'medium', 'low'] as const
export const LIVE_MAX_FPS = [15, 30, 60] as const

export interface BrowserSettingsView {
  choice: string
  live: { enabled: boolean; quality: string; maxFps: number; maskPasswords: boolean }
  localOrigins: string[]
  /** `browser.credentials.domains` as stored; `[]` follows the provider's default. */
  credentialDomains: string[]
}

export function viewOf(cfg: BrowserSettingsConfig | undefined): BrowserSettingsView {
  const b = cfg?.browser
  const live = b?.live
  return {
    choice: typeof b?.choice === 'string' ? b.choice : BROWSER_DEFAULTS.choice,
    live: {
      enabled: typeof live?.enabled === 'boolean' ? live.enabled : BROWSER_DEFAULTS.live.enabled,
      quality: typeof live?.quality === 'string' ? live.quality : BROWSER_DEFAULTS.live.quality,
      maxFps: typeof live?.max_fps === 'number' ? live.max_fps : BROWSER_DEFAULTS.live.maxFps,
      maskPasswords:
        typeof live?.mask_passwords === 'boolean' ? live.mask_passwords : BROWSER_DEFAULTS.live.maskPasswords,
    },
    localOrigins: Array.isArray(cfg?.dashboard?.browser_local_origins)
      ? cfg!.dashboard!.browser_local_origins!.filter((o): o is string => typeof o === 'string')
      : [],
    credentialDomains: Array.isArray(b?.credentials?.domains)
      ? b!.credentials!.domains!.filter((d): d is string => typeof d === 'string')
      : [],
  }
}

/** A status that keeps the page polling fast until it settles. */
export function statusIsBusy(d: BrowserStatusData | undefined): boolean {
  return d?.install_job?.status === 'running' || d?.connection === 'waiting_allow'
}

export function useBrowserSettings() {
  const qc = useQueryClient()

  const dashCfg = useQuery<{ browse_enabled?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: fetchDashboardConfig,
    staleTime: 30_000,
  })
  const policyOff = browsePolicyOff(dashCfg.data)

  const status = useQuery<BrowserStatusData>({
    queryKey: BROWSER_STATUS_KEY,
    queryFn: api.getBrowserStatus,
    // Under a deny the gateway answers every browser route with 403: nothing to
    // read. Wait for the answer first; a failed config read still lets it run.
    enabled: !dashCfg.isLoading && !policyOff,
    // Stale at once, so returning to the page re-reads: a browser connected
    // from a chat turn becomes visible without waiting for the idle poll.
    staleTime: 0,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
    refetchInterval: (query) => (statusIsBusy(query.state.data) ? BUSY_POLL_MS : IDLE_POLL_MS),
  })

  const config = useQuery<BrowserSettingsConfig>({
    queryKey: CONFIG_KEY,
    queryFn: () => api.kirocrewConfig() as Promise<BrowserSettingsConfig>,
  })

  const invalidateStatus = () => { void qc.invalidateQueries({ queryKey: BROWSER_STATUS_KEY }) }
  const invalidateConfig = () => { void qc.invalidateQueries({ queryKey: CONFIG_KEY }) }

  const patch = useMutation({
    mutationFn: ({ path, value }: { path: string; value: unknown }) => api.patchConfig(path, value),
    // A sign-in setting reaches a running Kiro Chromium at its next page load;
    // the status read says so at once.
    onSettled: () => { invalidateConfig(); invalidateStatus() },
  })
  const install = useMutation({
    mutationFn: (req: BrowserInstallRequest = {}) => api.installBrowserHarness(req),
    onSuccess: (fresh) => qc.setQueryData(BROWSER_STATUS_KEY, fresh),
    onSettled: invalidateStatus,
  })
  const test = useMutation<BrowserTestData>({
    mutationFn: () => api.testBrowser(),
    onSettled: invalidateStatus,
  })
  const disconnect = useMutation({
    mutationFn: () => api.disconnectBrowser(),
    onSuccess: (fresh) => qc.setQueryData(BROWSER_STATUS_KEY, fresh),
    onSettled: invalidateStatus,
  })

  return {
    policyOff,
    policyPending: dashCfg.isLoading,
    status,
    config,
    view: viewOf(config.data),
    setConfig: (path: string, value: unknown) => patch.mutate({ path, value }),
    patch,
    install,
    test,
    disconnect,
  }
}

export type BrowserSettings = ReturnType<typeof useBrowserSettings>
