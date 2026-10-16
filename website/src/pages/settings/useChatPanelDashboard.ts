/**
 * Settings ▸ Chat's dashboard-side server state: the `dashboardConfig` read, the
 * value its controls show and the two writes (a key patch, the default memory mode),
 * plus the Discovery page's Feature Tips and feature-video reads. `ChatPanel` calls
 * it once per render, after the optimistic overlay it reads and before its own
 * kirocrewConfig read.
 */
import { useQuery, useMutation, type QueryClient } from '@tanstack/react-query'
import type { DashboardConfig, MemoryMode } from '../chat/ChatSettings'
import { api, type FeatureVideoStatus } from '../../api/client'
import { useAppSelector } from '../../store'
import { serializeDefaultMemoryModeUpdate } from '../../api/queryClient'
import { i18nT } from '../../i18n/t'
import { fetchDashboardConfig } from '../../api/dashboardConfigQuery'
import type { useOptimisticConfigPaths } from './useOptimisticConfigPaths'
import {
  FEATURE_VIDEO_POLL_MS, DEFAULT_MEMORY_MODE_PATH,
} from './chatPanelOptions'

type OptimisticOverlay = ReturnType<typeof useOptimisticConfigPaths>

export function useChatPanelDashboard({
  qc, setPathSaveError, clearOwnPathError, overlay,
}: {
  qc: QueryClient
  setPathSaveError: (path: string, msg: string) => void
  clearOwnPathError: (path: string) => void
  overlay: OptimisticOverlay
}) {
  // ── Dashboard config (server-side) ──
  const dashQ = useQuery<DashboardConfig>({
    queryKey: ['dashboardConfig'],
    queryFn: fetchDashboardConfig,
  })
  // Shown config: the in-flight save when one is pending, else the server's.
  // Toggles both render this and BUILD THEIR PAYLOAD from it (setDash), so a
  // second toggle during a save carries the first one's value forward.
  const dashCfg = overlay.shown(
    'dashboardConfig',
    dashQ.data ?? { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, default_memory_mode: 'persistent' as const, widget_density: 'more' as const, verbosity: 'answer_only' as const, quick_send: false, session_grid: false, tail_fork_enabled: false, link_previews: false, link_patterns: [], mcp_app_panel: false, auto_open_git_panel: false, side_panel_always_open: false, side_panel_pin_summary: false, session_card_source_links: true, folder_suggestions_enabled: true, topic_tags_enabled: false, use_builtin_browser: true, model_picker_hidden_models: [] },
  )
  const shownDefaultMemoryMode = overlay.shown(
    DEFAULT_MEMORY_MODE_PATH,
    dashCfg.default_memory_mode,
  )

  // ── Feature Tips opt-out (server-side per-user state) ──
  const tipsQ = useQuery<{ enabled_config: boolean; opted_out: boolean }>({
    queryKey: ['tipsStatus'],
    queryFn: () => api.tipsStatus(),
  })
  const tipsOpts = overlay.mutationOpts<boolean>({
    queryKey: ['tipsStatus'],
    mutationFn: (enable: boolean) => api.tipsFeedback('', enable ? 'optin' : 'optout'),
    path: () => 'tipsStatus.opted_out',
    displayValue: enable => !enable,
    applyToCache: (cached, enable) => ({ ...(cached as { enabled_config: boolean; opted_out: boolean }), opted_out: !enable }),
    onFailure: () => setPathSaveError('tipsStatus.opted_out', i18nT('pages.settings.chatPanel.failed_to_save_tips_preference')),
    onSupersede: clearOwnPathError,
  })
  const tipsMut = useMutation({
    ...tipsOpts,
    onSettled: (data: unknown, err: unknown, enable: boolean, token: number | undefined) => {
      tipsOpts.onSettled(data, err, enable, token)
      // Drop any cached/in-flight tip so a running Chat view can't display a
      // tip fetched before the preference changed.
      qc.removeQueries({ queryKey: ['tips-next'] })
    },
  })
  const tipsConfigOff = tipsQ.data ? !tipsQ.data.enabled_config : false
  const shownOptedOut = overlay.shown('tipsStatus.opted_out', tipsQ.data?.opted_out)

  // ── Feature-video cache (read-only readout + one manual action) ──
  //
  // Both calls carry the ACTIVE SLOT's key. The status route's read gate reads
  // "not restricted" for a missing key and for the shared `dashboard:ui`
  // placeholder alike, so a request without one is served the permanent
  // engagement history even from a temporary session -- the one kind of session
  // whose contract is that reads are withheld. Naming the slot is what makes the
  // server's own gate reachable, exactly as the startup modal does for the two
  // routes it calls.
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const fvSessionKey = activeSlot ? `dashboard:${activeSlot}` : undefined
  //
  // Polled rather than pushed, and only while this panel is mounted: the clips
  // are fetched by a background pass that reports no events, so the only way to
  // watch it move is to ask. 15s is slow enough to be free and quick enough that
  // a clip finishing feels live. Closing the panel unmounts this and the polling
  // stops with it -- which is why the interval is safe to leave running.
  //
  // The key is IN the query key: a restricted slot and an ordinary one get
  // different answers from the same route, so one cache entry for both would
  // serve whichever landed first.
  const fvQ = useQuery<FeatureVideoStatus>({
    queryKey: ['featureVideoStatus', fvSessionKey],
    queryFn: () => api.featureVideoStatus(fvSessionKey),
    refetchInterval: FEATURE_VIDEO_POLL_MS,
  })
  // Kicks the background pass and returns immediately; the readout above is what
  // reports progress, so this refetches once rather than tracking the work.
  const fvFetchMut = useMutation({
    mutationFn: () => api.featureVideoFetchAll(fvSessionKey),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['featureVideoStatus'] }) },
  })
  const fv = fvQ.data
  /**
   * One line, three states, in the order that the most specific wins.
   *
   * Returns null while the read is still out, when the feature is off, and when
   * the gateway reports no cache at all -- so the row is absent rather than
   * showing a zero-of-zero that reads like an empty cache, or a policy that was
   * never stated.
   */
  const featureVideoStatusLine = (): string | null => {
    if (!fv || !fv.enabled) return null
    // No `download_enabled` means this gateway has no cache to report -- the
    // route answers 200 with an older payload that carries none of these
    // fields. Show nothing rather than reading the absence as `false`, which
    // would put a download policy on screen that does not exist.
    if (fv.download_enabled === undefined) return null
    if (fv.downloading) {
      // The title is what a reader recognises. The id is only the fallback for
      // a gateway that predates `downloading_title`, or a manifest entry with no
      // title: dropping it there would hide which clip is moving.
      return i18nT('pages.settings.chatPanel.feature_videos_downloading', {
        title: fv.downloading_title || fv.downloading,
      })
    }
    if (!fv.download_enabled) {
      return i18nT('pages.settings.chatPanel.feature_videos_downloads_disabled')
    }
    return i18nT('pages.settings.chatPanel.feature_videos_cached', {
      cached: fv.cached, total: fv.total, release: fv.release,
    })
  }
  const featureVideoLine = featureVideoStatusLine()

  // Only the CHANGED keys go on the wire, the way `BrowserPanel`'s own dashboard
  // mutation already does it: the config handler applies whichever keys the body
  // carries, so a full-object PUT rebuilt from this tab's cache would write every
  // OTHER setting back at its cached value -- clobbering one that a second tab
  // (or `kirocrew config set`) changed after we cached it.
  //
  // The overlay still displays and caches the WHOLE object, so the patch is
  // merged in both places. Merging onto the SHOWN config rather than the server
  // value is what keeps the property above -- a second toggle during an in-flight
  // save carries the first one's value forward -- and the monotonic token still
  // keeps a slow earlier save from clobbering a newer one's display or cache write.
  const dashMut = useMutation(overlay.mutationOpts<Partial<DashboardConfig>>({
    queryKey: ['dashboardConfig'],
    mutationFn: (patch: Partial<DashboardConfig>) => api.updateDashboardConfig(patch),
    path: () => 'dashboardConfig',
    displayValue: patch => ({ ...dashCfg, ...patch }),
    applyToCache: (cached, patch) => ({ ...(cached as DashboardConfig), ...patch }),
    onFailure: () => setPathSaveError('dashboardConfig', i18nT('pages.settings.chatPanel.failed_to_save_dashboard_config')),
    onSupersede: clearOwnPathError,
  }))
  const defaultModeMut = useMutation(overlay.mutationOpts<MemoryMode>({
    queryKey: ['dashboardConfig'],
    mutationFn: (value: MemoryMode) => serializeDefaultMemoryModeUpdate(
      value,
      () => api.updateDashboardConfig({ default_memory_mode: value }),
    ),
    path: () => DEFAULT_MEMORY_MODE_PATH,
    displayValue: value => value,
    applyToCache: (cached, value) => ({
      ...(cached as DashboardConfig),
      default_memory_mode: value,
    }),
    onFailure: () => setPathSaveError(DEFAULT_MEMORY_MODE_PATH, i18nT('pages.settings.chatPanel.failed_to_save_dashboard_config')),
    onSupersede: clearOwnPathError,
  }))
  return {
    dashQ, dashCfg, shownDefaultMemoryMode, tipsQ, tipsMut, tipsConfigOff, shownOptedOut, fvQ,
    fvFetchMut, fv, featureVideoLine, dashMut, defaultModeMut,
  }
}
