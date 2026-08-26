import React from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { SettingsSubNav } from '../../components/SettingsSubNav'
import { SlackIcon } from '../../components/SlackIcon'
import { DiscordIcon } from '../../components/DiscordIcon'
import { TelegramLogo } from '../../components/TelegramLogo'
import { WebexIcon } from '../../components/WebexIcon'
import { WeComLogo } from '../../components/WeComLogo'
import { TeamsIcon } from '../../components/TeamsIcon'
import { WeixinLogo } from '../../components/WeixinLogo'
import { IMessageIcon } from '../../components/IMessageIcon'
import { WhatsAppLogo } from '../../components/WhatsAppLogo'
import { SlackPanel } from './SlackPanel'
import { DiscordPanel } from './DiscordPanel'
import { TelegramPanel } from './TelegramPanel'
import { WebexPanel } from './WebexPanel'
import { WeComPanel } from './WeComPanel'
import { ChannelDisabledPanel } from './ChannelDisabledPanel'
import { TeamsPanel } from './TeamsPanel'
import { WeixinPanel } from './WeixinPanel'
import { IMessagePanel } from './IMessagePanel'
import { WhatsAppPanel } from './WhatsAppPanel'

import { i18nT } from '../../i18n/t'
/** Minimal status shape every channel config endpoint shares. */
interface ChannelStatus {
  connected: boolean
  configured: boolean
}

interface ChannelEntry {
  key: string
  name: string
  logo: React.ReactNode
  /** Matches the detail panel's queryKey so React Query shares the cache. */
  queryKey: string
  getConfig: () => Promise<ChannelStatus>
  Panel: React.ComponentType
}

/** Canonical list of chat channels. queryKey values MUST stay in sync with the
 *  per-channel panels (SlackPanel / BotChannelPanel specs) so the list and
 *  the detail pane read the same cache entry. */
const CHANNELS: ChannelEntry[] = [
  { key: 'slack', name: 'Slack', logo: <SlackIcon size={20} />, queryKey: 'slack-config', getConfig: () => api.getSlackConfig(), Panel: SlackPanel },
  { key: 'discord', name: 'Discord', logo: <DiscordIcon size={20} />, queryKey: 'discord-config', getConfig: () => api.getDiscordConfig(), Panel: DiscordPanel },
  { key: 'telegram', name: 'Telegram', logo: <TelegramLogo size={20} />, queryKey: 'telegram-config', getConfig: () => api.getTelegramConfig(), Panel: TelegramPanel },
  { key: 'webex', name: 'Webex', logo: <WebexIcon size={20} />, queryKey: 'webex-config', getConfig: () => api.getWebexConfig(), Panel: WebexPanel },
  { key: 'wecom', name: 'WeCom', logo: <WeComLogo size={20} />, queryKey: 'wecom-config', getConfig: () => api.getWeComConfig(), Panel: WeComPanel },
  { key: 'teams', name: 'Microsoft Teams', logo: <TeamsIcon size={20} />, queryKey: 'teams-config', getConfig: () => api.getTeamsConfig(), Panel: TeamsPanel },
  { key: 'weixin', name: 'WeChat', logo: <WeixinLogo size={20} />, queryKey: 'weixin-config', getConfig: () => api.getWeixinConfig(), Panel: WeixinPanel },
  { key: 'imessage', name: 'iMessage', logo: <IMessageIcon size={20} />, queryKey: 'imessage-config', getConfig: () => api.getIMessageConfig(), Panel: IMessagePanel },
  { key: 'whatsapp', name: 'WhatsApp', logo: <WhatsAppLogo size={20} />, queryKey: 'whatsapp-config', getConfig: () => api.getWhatsAppConfig(), Panel: WhatsAppPanel },
]

export const CHANNEL_KEYS = CHANNELS.map(c => c.key)

function statusLine(s: ChannelStatus | undefined, isError: boolean): { text: string; color: string; dot: boolean } {
  if (isError) return { text: i18nT('pages.settings.channelsPanel.status_unavailable'), color: 'var(--muted)', dot: false }
  if (!s) return { text: i18nT('pages.settings.channelsPanel.checking'), color: 'var(--muted)', dot: false }
  if (s.connected) return { text: i18nT('pages.settings.channelsPanel.connected'), color: 'var(--ok)', dot: true }
  if (s.configured) return { text: i18nT('pages.settings.channelsPanel.not_connected'), color: 'var(--warn)', dot: true }
  return { text: i18nT('pages.settings.channelsPanel.needs_setup'), color: 'var(--muted)', dot: false }
}

/** Per-channel `channels`-governance state, driven off the policy map. Every
 *  channel (Slack included) is governed: a policy that denies a channel blocks
 *  its inbound + tool-approval chokepoints, so the UI must reflect that. The
 *  editable config panel renders ONLY on a confirmed ALLOW — never while the
 *  policy is unknown, so a user can't edit config that won't take effect. */
type ChannelGovState = 'allowed' | 'denied' | 'pending' | 'unavailable'

function govState(
  key: string,
  policy: Record<string, boolean | null> | undefined,
  isLoading: boolean,
  isError: boolean,
): ChannelGovState {
  if (isError) return 'unavailable'
  if (isLoading || policy === undefined) return 'pending'
  const v = policy[key]
  if (v === true) return 'allowed'
  if (v === false) return 'denied'
  // null (eval error) or a missing key → cannot confirm ALLOW → unavailable.
  return 'unavailable'
}

/** Channels tab: SettingsSubNav list-detail over the chat integrations.
 *  Selection is URL-backed (?sub=slack; legacy ?channel= still lands) so deep
 *  links and the legacy ?tab=slack remap keep working. */
export function ChannelsPanel() {
  const statuses = useQueries({
    queries: CHANNELS.map(c => ({
      queryKey: [c.queryKey],
      queryFn: c.getConfig,
      staleTime: 30_000,
      // Keep the status column live while the tab stays open: a channel
      // reconnecting (or dropping) should be reflected without a reload.
      refetchInterval: 30_000,
      retry: false,
    })),
  })

  // Effective per-channel `channels` governance policy: { slack: true, ... }
  // (true permitted, false denied, null eval-error). All-true when no policy
  // governs channels (standard OSS build) → nothing greyed, UI unchanged.
  const {
    data: govPolicy,
    isLoading: govLoading,
    isError: govError,
  } = useQuery({
    queryKey: ['governance-channels'],
    queryFn: api.getGovernanceChannels,
    staleTime: 60_000,
    // The channels policy is a Level-2 PROFILE, which HOT-RELOADS at runtime (the
    // ProfileStore mtime watch) — unlike the boot-frozen Level-1 ceiling. So poll
    // on a modest interval: an admin tightening a live profile flips a channel to
    // "Off by admin" on an already-open Settings page within ~30s, no reload.
    refetchInterval: 30_000,
    retry: false,
  })
  const channelGov = (key: string): ChannelGovState =>
    govState(key, govPolicy, govLoading, govError)

  const items = CHANNELS.map((c, i) => {
    const st = statusLine(statuses[i].data as ChannelStatus | undefined, statuses[i].isError)
    const denied = channelGov(c.key) === 'denied'
    return {
      key: c.key,
      label: c.name,
      icon: c.logo,
      dimmed: denied,
      summary: denied ? (
        // A policy-denied channel shows "Off by admin" instead of its
        // connection status — the status is moot while the channel is
        // governed off. Full text in the title for the compact chip.
        <span
          className="inline-block mt-0.5 px-1.5 py-px rounded-full text-[11px] font-semibold uppercase bg-bg-hover text-muted border border-border whitespace-nowrap"
          title={i18nT('pages.settings.channelsPanel.off_by_admin')}
        >
          {i18nT('pages.settings.channelsPanel.off_by_admin')}
        </span>
      ) : (
        <span className="flex items-center gap-1.5 text-[11.5px]" style={{ color: st.color }}>
          {st.dot && <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: st.color }} />}
          {st.text}
        </span>
      ),
    }
  })

  return (
    <SettingsSubNav
      items={items}
      railWidth={280}
      listLabel={i18nT('pages.settings.channelsPanel.chat_channels')}
      backLabel={i18nT('pages.settings.channelsPanel.channels')}
    >
      {active => {
        const selected = CHANNELS.find(c => c.key === active)
        if (!selected) return null
        // The editable config panel renders ONLY on a confirmed ALLOW; a
        // denied / still-loading / unavailable governance state shows the
        // corresponding notice instead, so a user never edits (or the page
        // never flashes) a form whose config wouldn't take effect.
        return channelGov(selected.key) === 'allowed'
          ? <selected.Panel key={selected.key} />
          : <ChannelDisabledPanel
              key={`${selected.key}-gov`}
              label={selected.name}
              variant={channelGov(selected.key) as 'denied' | 'pending' | 'unavailable'}
            />
      }}
    </SettingsSubNav>
  )
}
