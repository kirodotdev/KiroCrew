/**
 * Settings > Display > View: Channels' switch (`dashboard.channels_enabled`).
 *
 * Channels was the hidden built-in app `channels`: off by default, absent from
 * the App Store, turned on with `kirocrew app enable channels`, and listed in
 * Library (where it could be turned off) only while it was on. This row keeps
 * that: it is drawn only while the gateway's setting is on, and stays drawn
 * after it is turned off here so the change can be undone without leaving the
 * page. A settings search offers it only while it is on too (`settingEntryOffered`),
 * for the same reason. Turning it on from the CLI (`kirocrew app enable channels`
 * or `kirocrew config set dashboard.channels_enabled true`) is what offers it.
 *
 * Off hides Channels' sidebar row and Search Everywhere result only: the page
 * and the channels on it are kept. Library does not list Channels, so the
 * sidebar unpin this browser may hold for its row (`mc-app-nav-hidden`, under
 * the row's nav id) is cleared when it is turned on.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { SettingsToggle } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { toggleAppNavHidden, useAppNavHidden } from '../../lib/appNavHidden'
import { BUILTIN_SURFACE_NAV } from '../../surfaces/surfaceData'
import { setConfigPathValue, useOptimisticConfigPaths } from './useOptimisticConfigPaths'

/** The config path this row writes; the rail's Channels row reads the same one. */
export const CHANNELS_SWITCH = BUILTIN_SURFACE_NAV.channels.configSwitch

type ChannelsCfg = { dashboard?: { channels_enabled?: boolean } }

export function ChannelsSetting() {
  const qc = useQueryClient()
  const cfgQ = useQuery<ChannelsCfg>({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const overlay = useOptimisticConfigPaths(qc)
  const [error, setError] = useState<string | null>(null)
  // Once the row has been drawn it stays for this visit, so turning Channels off
  // does not take away the control that turns it back on.
  const [shown, setShown] = useState(false)
  const mut = useMutation(overlay.mutationOpts<boolean>({
    queryKey: ['kirocrewConfig'],
    mutationFn: (value: boolean) => api.patchConfig(CHANNELS_SWITCH, value),
    path: () => CHANNELS_SWITCH,
    displayValue: v => v,
    applyToCache: (cached, value) => setConfigPathValue(cached as ChannelsCfg, CHANNELS_SWITCH, value),
    onFailure: () => setError(i18nT('pages.settings.displayPanel.channels_save_failed')),
    onSupersede: () => setError(null),
  }))
  const unpinned = useAppNavHidden().has(BUILTIN_SURFACE_NAV.channels.navId)
  const held = cfgQ.data?.dashboard?.channels_enabled
  const on = overlay.shown(CHANNELS_SWITCH, held === true)
  if (on && !shown) setShown(true)
  if (typeof held !== 'boolean' || !(on || shown)) return null
  const change = (value: boolean) => {
    if (value && unpinned) toggleAppNavHidden(BUILTIN_SURFACE_NAV.channels.navId)
    if (value !== on) mut.mutate(value)
  }
  return (
    <>
      <SettingsToggle
        configKey="dashboard.channels_enabled"
        label={i18nT('pages.settings.displayPanel.channels')}
        description={i18nT('pages.settings.displayPanel.channels_hint')}
        checked={on}
        onChange={change}
        disabled={mut.isPending}
      />
      {/* No hand-off: this row renders inside DisplayPanel, whose `shellDraft`
          and `installValue` are unsaved local drafts that navigation would
          discard. */}
      <ErrorNotice message={error} variant="inline" />
    </>
  )
}
