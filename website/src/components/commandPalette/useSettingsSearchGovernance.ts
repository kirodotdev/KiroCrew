import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type SlackConfigData } from '../../api/client'
import { fetchDashboardConfig } from '../../api/dashboardConfigQuery'
import { useUpdateSwitchesShown } from '../../utils/updateSwitches'
import { isInstancesDisabledError } from '../../utils/instancesDisabled'
import { ACP_BACKEND_KAS, type AcpBackendConfig } from '../../api/acpBackend'
import { fetchConfigSchema } from '../settingRef/useConfigSchema'
import type { SchemaEntry } from '../settingRef/resolveSettingRef'
import type { SettingsSearchGovernance } from './settingsSearchCore'

/**
 * The governance answers every settings search reads (the command palette,
 * the Command Bar and the Settings page search), resolved once so the three
 * cannot offer different rows.
 *
 * Each read is the SAME query the governed surface itself renders from, so a
 * search and the page it lands on cannot disagree. A search that may fetch
 * re-reads an answer over 30 s old when it opens; with `fetch: false` it reads
 * only what is cached, from entries kept for the session. The updater's answer
 * is read from cache only everywhere (`useUpdateSubscription` seeds it at
 * boot). An unanswered or failed read is
 * not a denial: the row is withheld only once it is known to be absent.
 */
const FRESH_ENOUGH = { staleTime: 30_000 } as const
const CACHED_ONLY = { enabled: false, refetchOnMount: false, staleTime: Infinity, gcTime: Infinity } as const

export function useSettingsSearchGovernance(
  /** `false` reads only what is cached: the Command Bar's root issues no request. */
  { fetch = true }: { fetch?: boolean } = {},
): SettingsSearchGovernance {
  // The Decisions card's `['dashboardConfig']` read.
  const dashCfgQ = useQuery<{ decisions_enabled?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: fetchDashboardConfig,
    ...(fetch ? FRESH_ENOUGH : CACHED_ONLY),
  })
  const decisionsEnabled = !dashCfgQ.isSuccess || dashCfgQ.data?.decisions_enabled === true
  // The `['tipsStatus']` read ChatPanel uses to drop its Discovery rail group.
  const tipsQ = useQuery<{ enabled_config: boolean }>({
    queryKey: ['tipsStatus'],
    queryFn: () => api.tipsStatus(),
    ...(fetch ? FRESH_ENOUGH : CACHED_ONLY),
  })
  const tipsEnabled = !tipsQ.isSuccess || tipsQ.data?.enabled_config !== false
  // The two About update switches, under the rule About draws them by.
  // The updater's answer from cache only, on every surface: a search open is no
  // reason for an IPC round trip, and About re-reads it when it is opened.
  const { app, gateway } = useUpdateSwitchesShown({ fetch: false })
  // The `['config-schema']` read the Kiro account tab is drawn by
  // (`useKiroSignInOffered`): the sign-in entry is offered while the build's
  // `agent.acp_backend` choices list KAS, whichever harness runs. Withheld only
  // on a successful read that does not list it. Read once and kept: the schema
  // changes only with the build.
  const schemaQ = useQuery<Map<string, SchemaEntry>>({
    queryKey: ['config-schema'],
    queryFn: fetchConfigSchema,
    ...(fetch ? { staleTime: Infinity } : CACHED_ONLY),
  })
  const backendChoices = schemaQ.data?.get('agent.acp_backend')?.enum
  const kiroSignInOffered = !schemaQ.isSuccess || !backendChoices || backendChoices.includes(ACP_BACKEND_KAS)
  // The `['kirocrewConfig']` read holds Channels' switch, which Display > View
  // draws only while on. From cache only, on every surface: the shell already
  // reads this at boot (the credits pill), so a search adds no request of its own.
  const cfgQ = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    ...CACHED_ONLY,
  })
  const channelsOn = cfgQ.isSuccess
    && (cfgQ.data as { dashboard?: { channels_enabled?: unknown } } | undefined)?.dashboard?.channels_enabled === true
  // The `['slack-config']` read Settings > Slack itself renders from, from cache
  // only: it carries the server's own answer to whether Connections offers the
  // Slack card (`slack_mcp_card`), which with the saved switch is the page's whole
  // rule for drawing the Slack tools switch. Withheld only on a successful read
  // that says neither; until the Slack page has been opened there is no answer.
  const slackQ = useQuery<SlackConfigData>({ queryKey: ['slack-config'], queryFn: api.getSlackConfig, ...CACHED_ONLY })
  const slackToolsShown = !slackQ.isSuccess
    || slackQ.data?.slack_mcp_card === true
    || slackQ.data?.slack_mcp === true
  // The `['instances']` read RemoteCrewPanel picks its branch by, from cache only:
  // the shell's tab bar already reads it at boot.
  const instancesQ = useQuery({ queryKey: ['instances'], queryFn: () => api.listInstances(), ...CACHED_ONLY })
  const remoteCrewsEnabled = instancesQ.isSuccess
    ? true
    : isInstancesDisabledError(instancesQ.error) ? false : undefined
  return useMemo(
    () => ({ decisionsEnabled, tipsEnabled, updateSwitches: { app, gateway }, kiroSignInOffered, remoteCrewsEnabled, channelsOn, slackToolsShown }),
    [decisionsEnabled, tipsEnabled, app, gateway, kiroSignInOffered, remoteCrewsEnabled, channelsOn, slackToolsShown],
  )
}
