/**
 * The switches that keep bot sessions in the person's own Sessions list.
 *
 * Two settings, one rule (`crew_mode.mate_lists_sessions`): the global
 * `dashboard.show_bot_sessions_in_list` lists every bot's sessions, and while it
 * is on no bot's own switch applies; while it is off, each bot's own
 * `show_sessions_in_list` decides for that bot. A bot's own switch therefore
 * reads on and cannot be pressed while the global one is on.
 *
 * Both sit at the top of a bot's Sessions list, in the Profile card and the bot
 * page side panel; the global one is also in Settings > Chat > Sessions
 * (`ShowAllBotsSessionsRow`). Every copy of the global switch reads the full
 * config query (`['kirocrewConfig']`), which every refresh frame re-reads, and the
 * gateway sends one when either setting is saved, so every list and switch follows.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { List } from 'lucide-react'
import { api, type MemberRosterRow } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Toggle } from '../../components/ui'
import { RowGroup, RowStatic } from './profileRow'

/** The full config read (`GET /api/config/kirocrew`): any refresh frame re-reads it. */
const CONFIG_QUERY_KEY = ['kirocrewConfig'] as const

interface ListingConfig { dashboard?: { show_bot_sessions_in_list?: boolean } }

/** The global switch: its saved value, the shown value while a save runs, and the
 *  config read's own state, shared by every place that offers it. */
export function useAllBotsSessionsSwitch() {
  const queryClient = useQueryClient()
  const cfg = useQuery<ListingConfig>({ queryKey: CONFIG_QUERY_KEY, queryFn: () => api.kirocrewConfig() })
  const flip = useMutation({
    mutationFn: (want: boolean) => api.updateDashboardConfig({ show_bot_sessions_in_list: want }),
    onSuccess: (_data, want) => {
      queryClient.setQueryData<ListingConfig>(CONFIG_QUERY_KEY, (old) =>
        old ? { ...old, dashboard: { ...old.dashboard, show_bot_sessions_in_list: want } } : old)
      void queryClient.invalidateQueries({ queryKey: ['dashboardConfig'] })
    },
  })
  const saved = cfg.data?.dashboard?.show_bot_sessions_in_list === true
  return {
    cfg,
    on: flip.isPending ? (flip.variables ?? saved) : saved,
    set: (want: boolean) => flip.mutate(want),
    pending: flip.isPending,
    saveFailed: flip.isError && !flip.isPending,
  }
}

/** The config read failed with nothing cached: the switches cannot show a value,
 *  so say so with an in-place retry. */
export function AllBotsConfigLoadNotice({ cfg }: { cfg: ReturnType<typeof useAllBotsSessionsSwitch>['cfg'] }) {
  const { t } = useTranslation()
  if (!cfg.isError || cfg.data) return null
  return (
    /* No hand-off: the bots page may hold an unsent draft, and in Settings the
       ChatPanel's `localRoleOther` / `localBudget` / `localKeepChars` drafts stay
       mounted; "Ask the agent" would navigate away and discard them. */
    <ErrorNotice
      message={t('pages.settings.crewmatesSection.failed_to_load')}
      testId="bot-sessions-in-list-load-error"
      footer={
        <Btn onClick={() => { void cfg.refetch() }} disabled={cfg.isFetching} data-testid="bot-sessions-in-list-retry">
          {t('pages.settings.crewmatesSection.retry')}
        </Btn>
      }
    />
  )
}

export interface BotSessionListSwitchesProps {
  member: Pick<MemberRosterRow, 'name' | 'show_sessions_in_list'>
  /** The bot's name as the page shows it. */
  displayName: string
}

export default function BotSessionListSwitches({ member, displayName }: BotSessionListSwitchesProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const all = useAllBotsSessionsSwitch()
  const botFlip = useMutation({
    mutationFn: (want: boolean) => api.updateKirocrewAgent(member.name, { show_sessions_in_list: want }),
    onSuccess: (_data, want) => {
      queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, (rows) =>
        rows?.map((r) => (r.name === member.name ? { ...r, show_sessions_in_list: want } : r)))
    },
  })

  const botSaved = member.show_sessions_in_list === true
  const botOn = botFlip.isPending ? (botFlip.variables ?? botSaved) : botSaved
  const allLabel = t('pages.membersPage.bot_sessions_all_switch')
  const botLabel = t('pages.membersPage.bot_sessions_this_switch', { name: displayName })
  const failed = all.saveFailed || (botFlip.isError && !botFlip.isPending)
  const loaded = !!all.cfg.data

  return (
    <div className="flex flex-col gap-2" data-testid="bot-session-list-switches">
      <RowGroup>
        <RowStatic icon={<List size={16} />} tone="accent" label={allLabel} testId="bot-sessions-in-list-all" wrap>
          <Toggle checked={all.on} onChange={all.set} busy={all.pending} disabled={!loaded} label={allLabel} />
        </RowStatic>
        <RowStatic icon={<List size={16} />} tone="accent" label={botLabel} testId="bot-sessions-in-list-this" wrap>
          {/* The global switch lists every bot's sessions, so this one reads on and cannot be pressed while it is on. */}
          <Toggle checked={all.on || botOn} onChange={(v) => botFlip.mutate(v)} busy={botFlip.isPending} disabled={all.on || !loaded} label={botLabel} />
        </RowStatic>
      </RowGroup>
      <AllBotsConfigLoadNotice cfg={all.cfg} />
      {/* No hand-off: the bots page may hold an unsent draft, and "Ask the agent"
          would navigate away and discard it. */}
      <ErrorNotice message={failed ? t('pages.settings.crewmatesSection.failed_to_save') : null} testId="bot-sessions-in-list-error" />
    </div>
  )
}
