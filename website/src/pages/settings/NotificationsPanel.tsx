import { useEffect, useState } from 'react'
import { Lock, MonitorCog, Blocks } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect } from '../../components/settings'
import StyledSelect from '../../components/StyledSelect'
import { Toggle } from '../../components/ui'
import { api } from '../../api/client'
import type { NotificationChannel } from '../../types'
import {
  SOUND_PRESETS, type SoundPreset, type SoundCategory,
  loadSoundSettings, saveSoundSettings, playPreset,
} from '../../hooks/useNotificationSound'

const PRESET_OPTIONS: SoundPreset[] = ['none', ...SOUND_PRESETS]
const PRESET_LABELS: Record<SoundPreset, string> = {
  none: 'Silent', chime: 'Chime (C6-E6-G6)', ding: 'Ding', blip: 'Blip', pop: 'Pop',
}
const DEFAULT_SENTINEL = 'default'
const OVERRIDE_OPTIONS: string[] = [DEFAULT_SENTINEL, ...PRESET_OPTIONS]
const OVERRIDE_LABELS: string[] = ['Use default', ...PRESET_OPTIONS.map(p => PRESET_LABELS[p])]

const CATEGORY_ROWS: { key: SoundCategory; label: string; description: string }[] = [
  { key: 'all',        label: 'Default (all categories)', description: 'Fallback sound when no category-specific override is set' },
  { key: 'turn',       label: 'Agent replies', description: 'When the agent finishes a turn in any chat' },
  { key: 'cron',       label: 'Cron',       description: 'Scheduled job completions' },
  { key: 'approval',   label: 'Approval',   description: 'Tool approval requests' },
  { key: 'hook',       label: 'Webhook',    description: 'External hook triggers' },
  { key: 'heartbeat',  label: 'Heartbeat',  description: 'Heartbeat task results' },
  { key: 'subagent',   label: 'Subagent',   description: 'Background subagent completions' },
  { key: 'taskrunner', label: 'Tasks',      description: 'Task runner completions' },
]

const PRIORITY_SENTINEL = 'Channel default'
const PRIORITY_OPTIONS = [PRIORITY_SENTINEL, 'critical', 'default', 'passive']

/** Human label for a channel within its group (drop the source prefix apps
 *  and system channels share with their group header). */
function channelLabel(c: NotificationChannel): string {
  return c.channel.startsWith(`${c.source}.`) ? c.channel.slice(c.source.length + 1) : c.channel
}

/** Per-channel notification settings (RFC Phase 3): mute + priority override,
 *  grouped by source (System first, then apps). Protected channels render
 *  locked. Channels with stored settings but no live registration (app
 *  disabled) stay visible so mutes remain editable. */
function ChannelsSection() {
  const [channels, setChannels] = useState<NotificationChannel[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api.notificationChannels()
      .then((d: { channels?: NotificationChannel[] }) => { if (!cancelled) setChannels(d.channels || []) })
      .catch(() => { if (!cancelled) setError('Failed to load channels') })
    return () => { cancelled = true }
  }, [])

  const patch = (channel: string, settings: { muted?: boolean; priority?: string | null }) => {
    // Optimistic update; the PUT is authoritative and a failure reloads.
    setChannels(prev => prev?.map(c => {
      if (c.channel !== channel) return c
      const next = { ...c.settings }
      if (settings.muted !== undefined) { if (settings.muted) next.muted = true; else delete next.muted }
      if ('priority' in settings) { if (settings.priority) next.priority = settings.priority; else delete next.priority }
      return { ...c, settings: next }
    }) ?? null)
    api.updateNotificationChannelSettings(channel, settings).catch(() => {
      api.notificationChannels().then((d: { channels?: NotificationChannel[] }) => setChannels(d.channels || [])).catch(() => {})
    })
  }

  if (error) return <SettingsSection title="Channels"><div className="text-[12px] text-muted">{error}</div></SettingsSection>
  if (channels === null || channels.length === 0) return null

  const sources = Array.from(new Set(channels.map(c => c.source)))
    .sort((a, b) => (a === 'system' ? -1 : b === 'system' ? 1 : a.localeCompare(b)))

  return (
    <SettingsSection title="Channels">
      <div className="text-[12px] text-muted -mt-1 mb-2">Mute channels or override their priority. Muted notifications stay in history but never badge, sound, or banner.</div>
      {sources.map(source => (
        <SettingsCard key={source}>
          <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[.05em] text-muted pb-1 border-b border-border">
            {source === 'system' ? <MonitorCog className="lucide-inline" /> : <Blocks className="lucide-inline" />}
            {source}
            {source !== 'system' && <span className="text-[10px] font-medium normal-case tracking-normal px-1.5 py-px rounded-full bg-accent-subtle text-accent">app</span>}
          </div>
          {channels.filter(c => c.source === source).map(c => {
            const muted = !!c.settings.muted
            const override = c.settings.priority
            return (
              <div key={c.channel} className={`flex items-center gap-2.5 py-1.5 ${muted || !c.registered ? 'opacity-60' : ''}`}>
                <div className="flex-1 min-w-0">
                  <div className="text-[13px] text-text flex items-center gap-1.5">
                    {channelLabel(c)}
                    {c.protected && <Lock className="lucide-inline text-muted" aria-label="Protected channel" />}
                  </div>
                  <div className="text-[11px] text-muted">
                    {!c.registered
                      ? 'Channel not active (app disabled) — setting retained'
                      : c.protected
                        ? 'Always interrupts — cannot be muted or lowered'
                        : `Default priority: ${c.default_priority || 'default'}`}
                  </div>
                </div>
                {c.protected ? (
                  <span className="text-[11px] text-muted italic shrink-0">protected</span>
                ) : (
                  <>
                    <div className="shrink-0 w-48">
                      <StyledSelect
                        options={PRIORITY_OPTIONS}
                        value={override ?? PRIORITY_SENTINEL}
                        onChange={v => patch(c.channel, { priority: v === PRIORITY_SENTINEL ? null : v })}
                      />
                    </div>
                    <div className="shrink-0">
                      <Toggle
                        checked={!muted}
                        onChange={on => patch(c.channel, { muted: !on })}
                        label={`Notifications for ${c.channel}`}
                      />
                    </div>
                  </>
                )}
              </div>
            )
          })}
        </SettingsCard>
      ))}
    </SettingsSection>
  )
}

export function NotificationsPanel() {
  const [settings, setSettings] = useState(() => loadSoundSettings())

  const update = (partial: Partial<typeof settings>) => {
    const next = { ...settings, ...partial }
    setSettings(next)
    saveSoundSettings(next)
  }

  const setCategoryPreset = (cat: SoundCategory, preset: SoundPreset) => {
    update({ perCategory: { ...settings.perCategory, [cat]: preset } })
  }

  const clearCategoryOverride = (cat: SoundCategory) => {
    const { [cat]: _drop, ...rest } = settings.perCategory
    void _drop
    update({ perCategory: rest })
  }

  const fallback = settings.perCategory.all ?? 'chime'

  return (
    <>
      <ChannelsSection />
      <SettingsSection title="Sound">
        <SettingsCard>
          <SettingsToggle
            label="Play sound on new notifications"
            checked={settings.enabled}
            onChange={v => update({ enabled: v })}
          />
          <div className="flex flex-col gap-1.5 py-1.5">
            {/* Slider is correctly associated via htmlFor+id (a range input can't be nested); label-has-for's nesting requirement is a false positive here. */}
            {/* eslint-disable-next-line jsx-a11y/label-has-for */}
            <label htmlFor="mc-volume-slider" className="text-[13px] font-semibold text-text">Volume</label>
            <div className="text-[12px] text-muted">{Math.round(settings.volume * 100)}%</div>
            <input
              id="mc-volume-slider"
              aria-label="Volume"
              type="range" min={0} max={100} step={5}
              value={Math.round(settings.volume * 100)}
              onChange={e => update({ volume: Number(e.target.value) / 100 })}
              disabled={!settings.enabled}
              className="w-full accent-[var(--accent)]"
            />
          </div>
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Per-category sounds">
        <SettingsCard>
          {CATEGORY_ROWS.map(row => {
            const hasOverride = row.key !== 'all' && settings.perCategory[row.key] !== undefined
            const effective: SoundPreset = row.key === 'all'
              ? fallback
              : (settings.perCategory[row.key] ?? fallback)
            const selectValue: string = row.key === 'all'
              ? fallback
              : (hasOverride ? (settings.perCategory[row.key] as SoundPreset) : DEFAULT_SENTINEL)
            const opts = row.key === 'all' ? PRESET_OPTIONS : OVERRIDE_OPTIONS
            const optLabels = row.key === 'all'
              ? PRESET_OPTIONS.map(p => PRESET_LABELS[p])
              : OVERRIDE_LABELS
            return (
              <div key={row.key} className="flex items-end gap-2">
                <div className="flex-1 min-w-0">
                  <SettingsSelect
                    label={row.label}
                    description={row.description}
                    value={selectValue}
                    options={opts}
                    optionLabels={optLabels}
                    onChange={v => {
                      if (v === DEFAULT_SENTINEL) clearCategoryOverride(row.key)
                      else setCategoryPreset(row.key, v as SoundPreset)
                    }}
                    disabled={!settings.enabled}
                  />
                </div>
                <button
                  type="button"
                  onClick={() => playPreset(effective, settings.volume)}
                  disabled={!settings.enabled || effective === 'none' || settings.volume === 0}
                  className="mb-2 px-3 py-1.5 rounded-md border border-border text-[12px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong disabled:opacity-40 disabled:cursor-not-allowed transition-all font-body"
                >
                  Test
                </button>
              </div>
            )
          })}
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
