import { useEffect, useState } from 'react'
import { Lock, MonitorCog, Blocks } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect } from '../../components/settings'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '../../components/ui/select'
import { Toggle } from '../../components/ui'
import { api } from '../../api/client'
import type { NotificationChannel } from '../../types'
import {
  SOUND_PRESETS, type SoundPreset, type SoundCategory,
  loadSoundSettings, saveSoundSettings, playPreset,
} from '../../hooks/useNotificationSound'

import { i18nT } from '../../i18n/t'
const PRESET_OPTIONS: SoundPreset[] = ['none', ...SOUND_PRESETS]

/**
 * Catalog KEY for each sound preset's display label.
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()`
 * call here would freeze the boot language and never re-resolve on a language
 * switch. The lookup happens in `presetLabels()`, which runs during render.
 *
 * Shaped as a flat `Record` of full literal keys, indexed inline at the
 * `i18nT()` call, because that is the form `scripts/check-i18n-keys.mjs` can
 * resolve statically — a key it cannot resolve is a key it cannot verify exists.
 */
const PRESET_LABEL_KEY: Record<SoundPreset, string> = {
  none: 'pages.settings.notificationsPanel.preset_none',
  chime: 'pages.settings.notificationsPanel.preset_chime',
  ding: 'pages.settings.notificationsPanel.preset_ding',
  blip: 'pages.settings.notificationsPanel.preset_blip',
  pop: 'pages.settings.notificationsPanel.preset_pop',
  pulse: 'pages.settings.notificationsPanel.preset_pulse',
}
const DEFAULT_SENTINEL = 'default'
const OVERRIDE_OPTIONS: string[] = [DEFAULT_SENTINEL, ...PRESET_OPTIONS]

/** Localised preset labels, positionally aligned with `PRESET_OPTIONS`. No
 *  `hasOwnProperty` guard: `SoundPreset` is a closed union and
 *  `loadSoundSettings` validates stored values against it, so every id reaching
 *  this table has an entry (unlike `lib/effort.ts`, whose levels are whatever
 *  the backend reports). */
const presetLabels = (): string[] => PRESET_OPTIONS.map(p => i18nT(PRESET_LABEL_KEY[p]))

/** …plus the leading "inherit the default sound" row the per-category selects
 *  carry, aligned with `OVERRIDE_OPTIONS`. */
const overrideLabels = (): string[] => [
  i18nT('pages.settings.notificationsPanel.use_default'),
  ...presetLabels(),
]

/** Per-category sound rows, in display order. Ids only — the label and
 *  description are catalog keys below, resolved per render for the same reason
 *  `PRESET_LABEL_KEY` holds keys. */
const CATEGORY_ROWS: SoundCategory[] = [
  'all', 'turn', 'cron', 'approval', 'hook', 'heartbeat', 'subagent', 'taskrunner',
]
const CATEGORY_LABEL_KEY: Record<SoundCategory, string> = {
  all: 'pages.settings.notificationsPanel.category_all',
  turn: 'pages.settings.notificationsPanel.category_turn',
  cron: 'pages.settings.notificationsPanel.category_cron',
  approval: 'pages.settings.notificationsPanel.category_approval',
  hook: 'pages.settings.notificationsPanel.category_hook',
  heartbeat: 'pages.settings.notificationsPanel.category_heartbeat',
  subagent: 'pages.settings.notificationsPanel.category_subagent',
  taskrunner: 'pages.settings.notificationsPanel.category_taskrunner',
}
const CATEGORY_DESCRIPTION_KEY: Record<SoundCategory, string> = {
  all: 'pages.settings.notificationsPanel.category_all_description',
  turn: 'pages.settings.notificationsPanel.category_turn_description',
  cron: 'pages.settings.notificationsPanel.category_cron_description',
  approval: 'pages.settings.notificationsPanel.category_approval_description',
  hook: 'pages.settings.notificationsPanel.category_hook_description',
  heartbeat: 'pages.settings.notificationsPanel.category_heartbeat_description',
  subagent: 'pages.settings.notificationsPanel.category_subagent_description',
  taskrunner: 'pages.settings.notificationsPanel.category_taskrunner_description',
}

/** Sentinel for "this channel has no priority override". It is the select's
 *  COMPARED value, not its rendered text: the option renders
 *  `i18nT('…channel_default')` while `value` / `onValueChange` compare this
 *  string, so localising it in place would silently change what the handler
 *  matches on. Nothing persists it (choosing it PUTs `priority: null`) and the
 *  backend never emits it.
 *
 *  Left as the English phrase deliberately. Making it an opaque id is the right
 *  shape, but that rewrite lands on the same line the zero-tolerance
 *  `[added-lines]` i18n gate reads, and it is out of scope here — see the PR's
 *  follow-ups. */
const PRIORITY_SENTINEL = 'Channel default'
const PRIORITY_OPTIONS = [PRIORITY_SENTINEL, 'critical', 'default', 'passive']

/** Shared style for the sound-preview buttons: the Sound card's "Test
 *  notification" button and the per-category "Test" buttons must stay
 *  visually identical, so both compose from this one string. */
const TEST_BTN_CLASS = 'px-3 py-1.5 rounded-md border border-border text-[12px] font-medium cursor-pointer bg-transparent text-muted hover:text-text hover:border-border-strong disabled:opacity-40 disabled:cursor-not-allowed transition-all font-body'

/** Human label for a channel within its group (drop the source prefix apps
 *  and system channels share with their group header). */
function channelLabel(c: NotificationChannel): string {
  return c.channel.startsWith(`${c.source}.`) ? c.channel.slice(c.source.length + 1) : c.channel
}

/** Per-channel notification settings: mute + priority override,
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
      .catch(() => { if (!cancelled) setError(i18nT('pages.settings.notificationsPanel.failed_to_load_channels')) })
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

  if (error) return <SettingsSection title={i18nT('pages.settings.notificationsPanel.sources')}><div className="text-[12px] text-muted">{error}</div></SettingsSection>
  if (channels === null || channels.length === 0) return null

  const sources = Array.from(new Set(channels.map(c => c.source)))
    .sort((a, b) => (a === 'system' ? -1 : b === 'system' ? 1 : a.localeCompare(b)))

  return (
    <SettingsSection title={i18nT('pages.settings.notificationsPanel.sources')}>
      <div className="text-[12px] text-muted -mt-1 mb-2">{i18nT('pages.settings.notificationsPanel.mute_notification_sources_or_override_their_prio')}</div>
      {sources.map((source, i) => (
        <SettingsCard key={source} index={i}>
          <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[.05em] text-muted pb-1 border-b border-border">
            {source === 'system' ? <MonitorCog className="lucide-inline" /> : <Blocks className="lucide-inline" />}
            {source}
            {source !== 'system' && <span className="text-[10px] font-medium normal-case tracking-normal px-1.5 py-px rounded-full bg-accent-subtle text-accent">{i18nT('pages.settings.notificationsPanel.app')}</span>}
          </div>
          {channels.filter(c => c.source === source).map(c => {
            const muted = !!c.settings.muted
            const override = c.settings.priority
            return (
              <div key={c.channel} className={`flex flex-wrap items-center gap-2.5 py-1.5 ${muted || !c.registered ? 'opacity-60' : ''}`}>
                <div className="flex-1 min-w-0 basis-40">
                  <div className="text-[13px] text-text flex items-center gap-1.5">
                    {channelLabel(c)}
                    {c.protected && <Lock className="lucide-inline text-muted" aria-label={i18nT('pages.settings.notificationsPanel.protected_channel')} />}
                  </div>
                  <div className="text-[11px] text-muted">
                    {!c.registered
                      ? i18nT('pages.settings.notificationsPanel.channel_not_active_app_disabled_setting_retained')
                      : c.protected
                        ? i18nT('pages.settings.notificationsPanel.always_interrupts_cannot_be_muted_or_lowered')
                        : i18nT('pages.settings.notificationsPanel.default_priority', { priority: c.default_priority || 'default' })}
                  </div>
                </div>
                {c.protected ? (
                  <span className="text-[11px] text-muted italic shrink-0">{i18nT('pages.settings.notificationsPanel.protected')}</span>
                ) : (
                  <>
                    <div className="shrink-0 w-full sm:w-48">
                      <Select
                        value={override ?? PRIORITY_SENTINEL}
                        onValueChange={v => patch(c.channel, { priority: v === PRIORITY_SENTINEL ? null : v })}
                      >
                        <SelectTrigger aria-label={i18nT('pages.settings.notificationsPanel.priority_for', { name: c.channel })}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {PRIORITY_OPTIONS.map(opt => (
                            <SelectItem key={opt} value={opt}>
                              {opt === PRIORITY_SENTINEL
                                ? i18nT('pages.settings.notificationsPanel.channel_default')
                                : opt}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="shrink-0">
                      <Toggle
                        checked={!muted}
                        onChange={on => patch(c.channel, { muted: !on })}
                        label={i18nT('pages.settings.notificationsPanel.notifications_for', { name: c.channel })}
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
      {/* ChannelsSection resolves its sources from a fetch, so its cards mount
          in a later commit than the two static cards below: each group runs its
          own stagger ladder from its own mount paint. Continuing one ladder
          across the boundary would be wrong in the common case — delays are
          relative to element mount, so the static cards would wait
          sources.length steps on nothing while the fetch is still in flight. */}
      <ChannelsSection />
      <SettingsSection title={i18nT('pages.settings.notificationsPanel.sound')}>
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.notificationsPanel.play_sound_on_new_notifications')}
            checked={settings.enabled}
            onChange={v => update({ enabled: v })}
          />
          <div className="flex flex-col gap-1.5 py-1.5">
            {/* Slider is correctly associated via htmlFor+id (a range input can't be nested); label-has-for's nesting requirement is a false positive here. */}
            {/* eslint-disable-next-line jsx-a11y/label-has-for */}
            <label htmlFor="mc-volume-slider" className="text-[13px] font-semibold text-text">{i18nT('pages.settings.notificationsPanel.volume')}</label>
            <div className="text-[12px] text-muted">{Math.round(settings.volume * 100)}%</div>
            <input
              id="mc-volume-slider"
              aria-label={i18nT('pages.settings.notificationsPanel.volume')}
              type="range" min={0} max={100} step={5}
              value={Math.round(settings.volume * 100)}
              onChange={e => update({ volume: Number(e.target.value) / 100 })}
              disabled={!settings.enabled}
              className="w-full accent-[var(--accent)]"
            />
          </div>
          {/* Plays the fallback ('all') preset at the current volume — the same
              sample a real notification with no category override would play —
              so the user can dial in volume without triggering a real event.
              Labelled "Test sound", not "Test notification": no notification is
              created or delivered, and a user debugging missing notifications
              must not conclude delivery works because a tone played. Disabled
              conditions mirror the per-category Test buttons below: sound off,
              fallback set to none, or volume at zero all mean a click would be
              a silent no-op. The Default (all categories) row below keeps its
              own trailing Test button even though it runs the same action:
              that one serves in-place audition while choosing sounds in the
              per-category grid, this one serves volume dialing next to the
              slider — removing either forces a scroll across cards mid-task
              (maintainer decision on PR review). */}
          <div className="py-1.5">
            <button
              type="button"
              onClick={() => playPreset(fallback, settings.volume)}
              disabled={!settings.enabled || fallback === 'none' || settings.volume === 0}
              className={TEST_BTN_CLASS}
            >
              {i18nT('pages.settings.notificationsPanel.test_sound')}
            </button>
          </div>
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.notificationsPanel.per_category_sounds')}>
        <SettingsCard index={1}>
          {CATEGORY_ROWS.map(cat => {
            const hasOverride = cat !== 'all' && settings.perCategory[cat] !== undefined
            const effective: SoundPreset = cat === 'all'
              ? fallback
              : (settings.perCategory[cat] ?? fallback)
            const selectValue: string = cat === 'all'
              ? fallback
              : (hasOverride ? (settings.perCategory[cat] as SoundPreset) : DEFAULT_SENTINEL)
            const opts = cat === 'all' ? PRESET_OPTIONS : OVERRIDE_OPTIONS
            const optLabels = cat === 'all' ? presetLabels() : overrideLabels()
            return (
              <div key={cat} className="flex items-end gap-2">
                <div className="flex-1 min-w-0">
                  <SettingsSelect
                    label={i18nT(CATEGORY_LABEL_KEY[cat])}
                    description={i18nT(CATEGORY_DESCRIPTION_KEY[cat])}
                    value={selectValue}
                    options={opts}
                    optionLabels={optLabels}
                    onChange={v => {
                      if (v === DEFAULT_SENTINEL) {
                        clearCategoryOverride(cat)
                        if (fallback !== 'none') playPreset(fallback, settings.volume)
                      } else {
                        setCategoryPreset(cat, v as SoundPreset)
                        if (v !== 'none') playPreset(v as SoundPreset, settings.volume)
                      }
                    }}
                    disabled={!settings.enabled}
                  />
                </div>
                <button
                  type="button"
                  onClick={() => playPreset(effective, settings.volume)}
                  disabled={!settings.enabled || effective === 'none' || settings.volume === 0}
                  className={`mb-2 ${TEST_BTN_CLASS}`}
                >
                  {i18nT('pages.settings.notificationsPanel.test')}
                </button>
              </div>
            )
          })}
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
