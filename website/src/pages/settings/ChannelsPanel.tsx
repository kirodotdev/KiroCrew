import React, { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQueries, useQuery } from '@tanstack/react-query'
import { ChevronRight, ArrowLeft } from 'lucide-react'
import { api } from '../../api/client'
import { useContainerWidth } from '../../hooks/useContainerWidth'
import { SlackIcon } from '../../components/SlackIcon'
import { DiscordIcon } from '../../components/DiscordIcon'
import { TelegramLogo } from '../../components/TelegramLogo'
import { WebexIcon } from '../../components/WebexIcon'
import { WeComLogo } from '../../components/WeComLogo'
import { TeamsIcon } from '../../components/TeamsIcon'
import { SlackPanel } from './SlackPanel'
import { DiscordPanel } from './DiscordPanel'
import { TelegramPanel } from './TelegramPanel'
import { WebexPanel } from './WebexPanel'
import { WeComPanel } from './WeComPanel'
import { ChannelDisabledPanel } from './ChannelDisabledPanel'
import { TeamsPanel } from './TeamsPanel'

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
]

export const CHANNEL_KEYS = CHANNELS.map(c => c.key)

/** Two-pane breakpoint on the CONTENT area width (not the viewport): below
 *  this the tab collapses to list <-> detail with a back button. */
const TWO_PANE_MIN_WIDTH = 760

function statusLine(s: ChannelStatus | undefined, isError: boolean): { text: string; color: string; dot: boolean } {
  if (isError) return { text: 'Status unavailable', color: 'var(--muted)', dot: false }
  if (!s) return { text: 'Checking…', color: 'var(--muted)', dot: false }
  if (s.connected) return { text: 'Connected', color: 'var(--ok)', dot: true }
  if (s.configured) return { text: 'Not connected', color: 'var(--warn)', dot: true }
  return { text: 'Needs setup', color: 'var(--muted)', dot: false }
}

/** Channels tab: responsive list-detail over the five chat integrations.
 *  Wide content area = persistent list + detail side by side; narrow = the
 *  list alone, drilling into a full-width detail view with a back button.
 *  Selection is URL-backed (?channel=slack) so deep links and the legacy
 *  ?tab=slack remap land on the right channel. */
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

export function ChannelsPanel() {
  const [params, setParams] = useSearchParams()
  const [containerRef, width] = useContainerWidth<HTMLDivElement>()
  // null width = first paint before measurement; assume wide to avoid flashing
  // the narrow layout on desktop.
  const twoPane = width === null || width >= TWO_PANE_MIN_WIDTH

  const rawChannel = params.get('channel')
  const selectedKey = CHANNELS.some(c => c.key === rawChannel) ? rawChannel : null
  // Wide mode always shows a detail pane; default to the first channel.
  const effectiveKey = selectedKey ?? (twoPane ? CHANNELS[0].key : null)
  const selected = CHANNELS.find(c => c.key === effectiveKey) ?? null

  const setChannel = (key: string | null) => setParams(prev => {
    const next = new URLSearchParams(prev)
    if (key) next.set('channel', key)
    else next.delete('channel')
    return next
  }, { replace: true })

  // Canonicalize the wide-mode implicit selection into the URL. Without this,
  // shrinking the container below the two-pane breakpoint would flip
  // effectiveKey to null and drop the implicitly-selected panel to the bare
  // list. Gated on a REAL measurement (width !== null): the pre-measurement
  // paint optimistically renders wide, but writing channel=slack before the
  // ResizeObserver reports would make a fresh narrow visit open Slack instead
  // of the channel list.
  useEffect(() => {
    if (width !== null && twoPane && !selectedKey) setChannel(CHANNELS[0].key)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, twoPane, selectedKey])

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

  const list = (
    <div
      className={twoPane ? 'w-[280px] shrink-0' : 'w-full'}
      role="listbox"
      aria-label="Chat channels"
    >
      <div className="rounded-lg border border-border bg-card overflow-hidden">
        {CHANNELS.map((c, i) => {
          const st = statusLine(statuses[i].data as ChannelStatus | undefined, statuses[i].isError)
          const active = twoPane && c.key === effectiveKey
          const gov = channelGov(c.key)
          const denied = gov === 'denied'
          return (
            <button
              key={c.key}
              role="option"
              aria-selected={active}
              onClick={() => setChannel(c.key)}
              className={`flex items-center gap-3 w-full text-left px-3.5 py-2.5 cursor-pointer border-none transition-colors ${
                i > 0 ? 'border-t border-t-border border-solid border-x-0 border-b-0' : ''
              } ${active ? 'bg-accent-subtle' : 'bg-transparent hover:bg-bg-hover'} ${denied ? 'opacity-60' : ''}`}
            >
              <span className="w-5 h-5 shrink-0 flex items-center justify-center">{c.logo}</span>
              <span className="flex-1 min-w-0">
                <span className={`block text-[13.5px] font-semibold ${active ? 'text-accent' : 'text-text-strong'}`}>{c.name}</span>
                {denied ? (
                  // A policy-denied channel shows "Off by admin" instead of its
                  // connection status — the status is moot while the channel is
                  // governed off. Full text in the title for the compact chip.
                  <span
                    className="inline-block mt-0.5 px-1.5 py-px rounded-full text-[11px] font-semibold uppercase bg-bg-hover text-muted border border-border whitespace-nowrap"
                    title="Off by admin"
                  >
                    Off by admin
                  </span>
                ) : (
                  <span className="flex items-center gap-1.5 text-[11.5px]" style={{ color: st.color }}>
                    {st.dot && <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: st.color }} />}
                    {st.text}
                  </span>
                )}
              </span>
              {!twoPane && <ChevronRight size={14} className="text-muted shrink-0" />}
            </button>
          )
        })}
      </div>
    </div>
  )

  // Layout notes: both responsive modes render the SAME three child slots in
  // the same order (list?, back-button?, panel-wrapper) so React reconciles
  // the panel wrapper by position and <selected.Panel> is NEVER remounted by
  // a width transition — remounting would discard unsaved form drafts
  // (tokens mid-paste, allowlists mid-edit). Only changing the selected
  // channel (key=) remounts the panel, which is intended.
  return (
    <div ref={containerRef}>
      <div className={twoPane ? 'flex gap-6 items-start' : 'flex flex-col'}>
        {(twoPane || !selected) && list}
        {!twoPane && selected && (
          <button
            onClick={() => setChannel(null)}
            className="flex items-center gap-1.5 self-start text-[13px] font-medium text-accent bg-transparent border-none cursor-pointer px-0 py-1 mb-2 hover:underline"
          >
            <ArrowLeft size={14} />
            Channels
          </button>
        )}
        <div className={twoPane ? 'flex-1 min-w-0' : 'w-full'}>
          {selected && (
            // The editable config panel renders ONLY on a confirmed ALLOW; a
            // denied / still-loading / unavailable governance state shows the
            // corresponding notice instead, so a user never edits (or the page
            // never flashes) a form whose config wouldn't take effect.
            channelGov(selected.key) === 'allowed'
              ? <selected.Panel key={selected.key} />
              : <ChannelDisabledPanel
                  key={`${selected.key}-gov`}
                  label={selected.name}
                  variant={channelGov(selected.key) as 'denied' | 'pending' | 'unavailable'}
                />
          )}
        </div>
      </div>
    </div>
  )
}
