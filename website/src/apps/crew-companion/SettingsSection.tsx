import { SlidersHorizontal } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import Card from './Card'
import ToggleRow from './ToggleRow'
import { BREAK_PRESETS, BREAK_MIN_MINS, BREAK_MAX_MINS, BREAK_DEFAULT_MINS } from './constants'
import { clampBreakMins } from './reminders'
import type { ReminderConfigPatch, RemindersPayload } from './types'


export default function SettingsSection({ rem, remError, onCfg, customMins, setCustomMins }: {
  rem: RemindersPayload | null
  /**
   * The resolved-offline sentinel, distinct from `rem == null`, which is also
   * true while the first fetch is in flight. Gating the hint on `!rem` made
   * "Open the desktop app" flash on every load even with the pet running.
   */
  remError: string | null
  onCfg: (patch: ReminderConfigPatch) => void
  /** `null` = not editing, `''` = cleared and about to be typed. Sharing one value
   *  for both made the field snap back to the stored number as soon as it emptied. */
  customMins: string | null
  setCustomMins: (v: string | null) => void
}) {
  const mins = rem?.breakReminderMins ?? BREAK_DEFAULT_MINS
  const isPreset = BREAK_PRESETS.includes(mins)

  return (
    <Card title={i18nT('apps.crewCompanion.settings.title')} icon={SlidersHorizontal}>
      {/* Rendered even when the backend is unreachable: always show what it can
          control, and say why it cannot right now, rather than hiding it. */}
      {remError ? <div className="cc-hint">{i18nT('apps.crewCompanion.settings.offline_hint')}</div> : null}

      <ToggleRow
        label={i18nT('apps.crewCompanion.settings.break_nudges')}
        hint={i18nT('apps.crewCompanion.settings.break_nudges_hint', { mins })}
        on={rem?.breakNudgesEnabled ?? false}
        disabled={!rem}
        onChange={(v) => onCfg({ breakNudgesEnabled: v })}
      />

      {/* How often, shown only when nudges are on — same rule as the desktop panel. */}
      {rem?.breakNudgesEnabled ? (
        <div className="cc-every">
          <span className="cc-every-label">{i18nT('apps.crewCompanion.settings.how_often')}</span>
          {BREAK_PRESETS.map((m) => (
            <button
              key={m}
              type="button"
              className="cc-pill"
              aria-pressed={mins === m}
              aria-label={i18nT('apps.crewCompanion.settings.preset_aria', { mins: m })}
              onClick={() => { setCustomMins(null); onCfg({ breakReminderMins: m }) }}
            >
              {m}
            </button>
          ))}
          {/* Any interval, not only the presets. */}
          <input
            type="number"
            className={`cc-num${isPreset ? '' : ' is-custom'}`}
            min={BREAK_MIN_MINS}
            max={BREAK_MAX_MINS}
            aria-label={i18nT('apps.crewCompanion.settings.custom_minutes')}
            placeholder={i18nT('apps.crewCompanion.settings.custom_placeholder')}
            value={customMins !== null ? customMins : (isPreset ? '' : String(mins))}
            onFocus={() => setCustomMins(isPreset ? '' : String(mins))}
            onChange={(e) => setCustomMins(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
            onBlur={() => {
              const next = clampBreakMins(customMins ?? '')
              setCustomMins(null)
              if (next !== null) onCfg({ breakReminderMins: next })
            }}
          />
        </div>
      ) : null}

      <ToggleRow
        label={i18nT('apps.crewCompanion.settings.session_done')}
        hint={i18nT('apps.crewCompanion.settings.session_done_hint')}
        on={rem?.sessionNotificationsEnabled ?? false}
        disabled={!rem}
        onChange={(v) => onCfg({ sessionNotificationsEnabled: v })}
      />

      {/* One section-level note below both toggles. */}
      <div className="cc-note">{i18nT('apps.crewCompanion.settings.always_notify_note')}</div>
    </Card>
  )
}
