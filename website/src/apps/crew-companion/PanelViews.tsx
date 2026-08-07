/**
 * The panel's secondary views — "All reminders" and "Settings".
 *
 * These render as the CARD'S OWN CONTENT, in normal flow, replacing the reminder
 * list. Deliberately not `position: absolute` over the card: an overlay reads as a
 * second layer stacked on the first, which is the thing to avoid. Same card, same
 * chrome, different body.
 *
 * They also never navigate anywhere. Earlier versions sent these two links to the
 * Kiro Crew dashboard — a browser tab, then an Electron window — and both were a new
 * page. Everything here happens in the panel the user already has open.
 */
import { ArrowLeft, X } from 'lucide-react'
import React, { useCallback, useEffect, useState } from 'react'
import type { AppRegionStyle } from './appRegion'
import { useSkin, PANEL_FONT } from './panelSkin'
import { i18nT } from '../../i18n/t'
import { fmtUnit } from '../../i18n/format'
import { labelFor, BREAK_PRESETS, clampBreakMins } from './reminders'
import { BREAK_MIN_MINS, BREAK_MAX_MINS } from './constants'
import type { Reminder } from './types'
import { ReminderInput } from './ReminderInput'
import { petBridge } from './petBridge'

const api = petBridge

export type PanelView = 'main' | 'all' | 'settings'

/**
 * Human interval for the repeat pill, e.g. 120 → "2h".
 *
 * Through `fmtUnit` rather than `${n}h`: the unit has to be translated and the
 * digits localized, and the space (or lack of one) between number and unit is a
 * per-locale decision — `2h` is right in English and wrong in several others.
 */
function repeatLabel(mins: number): string {
  if (mins === 1440) return i18nT('apps.crewCompanion.view.daily')
  const unit = mins % 1440 === 0 ? fmtUnit(mins / 1440, 'day')
    : mins % 60 === 0 ? fmtUnit(mins / 60, 'hour')
    : fmtUnit(mins, 'minute')
  return i18nT('apps.crewCompanion.view.every', { unit })
}

/** Back row: the only way out of a secondary view besides Escape. */
export const ViewHeader: React.FC<{ title: string; onBack: () => void }> =
({ title, onBack }) => {
  const skin = useSkin()
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '10px 14px 6px' }}>
      <button
        onClick={onBack}
        aria-label={i18nT('apps.crewCompanion.view.back')}
        title={i18nT('apps.crewCompanion.view.back')}
        style={{
          // Opt out of the title's drag region below, or the click is swallowed.
          // This was a SECOND `style` attribute in the desktop app, where JSX kept
          // only the last one and dropped the opt-out entirely.
          WebkitAppRegion: 'no-drag',
          // 28px, matching the panel's other icon buttons.
          width: 28, height: 28, borderRadius: '50%',
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          background: 'none', border: 'none', cursor: 'pointer',
          color: skin.muted, fontSize: 15, lineHeight: 1, padding: 0, fontFamily: PANEL_FONT,
        } as AppRegionStyle}
        onMouseEnter={(e) => {
          (e.currentTarget as HTMLElement).style.background = skin.hairline
          ;(e.currentTarget as HTMLElement).style.color = skin.ink
        }}
        onMouseLeave={(e) => {
          (e.currentTarget as HTMLElement).style.background = 'none'
          ;(e.currentTarget as HTMLElement).style.color = skin.muted
        }}
      ><ArrowLeft size={11} aria-hidden="true" /></button>
      {/*
        Drag handle for the secondary views. The only other one is in the main
        view's heading, so Settings and All reminders had no way to move a
        frameless window. It covers the title and the space after it — NOT the
        back button, because Electron builds drag rectangles in the compositor
        from element geometry, and an overlapping rectangle swallows the click
        even when the button says `no-drag`.
      */}
      <span style={{
        flex: 1,
        fontSize: 12.5, fontWeight: 700,
        WebkitAppRegion: 'drag', cursor: 'grab',
        // A drag across a text node otherwise leaves the title selected.
        userSelect: 'none', WebkitUserSelect: 'none',
      } as AppRegionStyle}>{title}</span>
    </div>
  )
}

export const AllRemindersView: React.FC<{
  /** Persist a reminder — the same handler the main view uses. */
  onAdd?: (text: string, fireAtIso: string, everyMinutes?: number) => Promise<boolean>
}> = ({ onAdd }) => {
  const skin = useSkin()
  const [items, setItems] = useState<Reminder[] | null>(null)

  const refresh = useCallback(async () => {
    const list: Reminder[] = (await api?.remindersList?.().catch(() => [])) ?? []
    // Chronological, with already-fired one-offs sunk rather than hidden — seeing
    // what just fired is part of why you opened the full list.
    setItems([...list].sort((a, b) => {
      if (!!a.done !== !!b.done) return a.done ? 1 : -1
      return Date.parse(a.fireAt) - Date.parse(b.fireAt)
    }))
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const remove = async (id: string) => {
    await api?.remindersRemove?.(id)
    void refresh()
  }

  const now = new Date()

  /*
    Adding belongs here too. Looking at your reminders is exactly when you think of
    another one, and the empty state used to tell you to go back to the previous
    screen to type it — an instruction to navigate away from the obvious place.

    Wrapped so the list re-reads itself the moment something is saved.
  */
  const add = onAdd
    ? async (text: string, fireAtIso: string, everyMinutes?: number) => {
      const ok = await onAdd(text, fireAtIso, everyMinutes)
      if (ok) void refresh()
      return ok
    }
    : undefined

  const composer = <ReminderInput onAdd={add} />

  if (items === null) {
    return (
      <div>
        {composer}
        <div style={{ fontSize: 11, color: skin.muted, padding: '4px 2px' }}>{i18nT('apps.crewCompanion.view.loading')}</div>
      </div>
    )
  }
  if (items.length === 0) {
    return (
      <div>
        {composer}
        <div style={{
          background: skin.row, borderRadius: skin.rowRadius,
          padding: '14px 10px', textAlign: 'center',
        }}>
          <div style={{ fontSize: 11.5, fontWeight: 600 }}>{i18nT('apps.crewCompanion.panel.empty.title')}</div>
          <div style={{ fontSize: 10, color: skin.muted, marginTop: 3 }}>{i18nT('apps.crewCompanion.view.emptyHint')}</div>
        </div>
      </div>
    )
  }

  return (
    <div>
    {composer}
    <div style={{ background: skin.row, borderRadius: skin.rowRadius, overflow: 'hidden' }}>
      {items.map((r, i) => {
        const l = labelFor(r.fireAt, now)
        return (
          <div
            key={r.id}
            style={{
              display: 'flex', alignItems: 'center', gap: 7, padding: '8px 9px',
              borderTop: i === 0 ? 'none' : `1px solid ${skin.hairline}`,
              opacity: r.done ? 0.55 : 1,
            }}
          >
            <span style={{
              fontSize: 10.5, fontWeight: 700, fontVariantNumeric: 'tabular-nums', minWidth: 52,
            }}>{l.absLabel ?? l.relLabel}</span>
            <span style={{
              flex: 1, fontSize: 11, overflow: 'hidden',
              textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{r.text}</span>
            {r.recurrence && (
              <span style={{
                fontSize: 8.5, fontWeight: 700, padding: '2px 5px', borderRadius: 999,
                background: skin.accent, color: skin.onAccent, whiteSpace: 'nowrap',
              }}>{repeatLabel(r.recurrence.everyMinutes)}</span>
            )}
            <button
              onClick={() => remove(r.id)}
              aria-label={i18nT('apps.crewCompanion.view.remove')}
              title={i18nT('apps.crewCompanion.view.remove')}
              style={{
                width: 24, height: 24, borderRadius: '50%',
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                background: 'none', border: 'none', cursor: 'pointer',
                color: skin.muted, fontSize: 11, padding: 0, flexShrink: 0, fontFamily: PANEL_FONT,
              }}
            ><X size={11} aria-hidden="true" /></button>
          </div>
        )
      })}
    </div>
    </div>
  )
}

const Toggle: React.FC<{
  on: boolean; onChange: (v: boolean) => void; label: string; hint: string
}> = ({ on, onChange, label, hint }) => {
  const skin = useSkin()
  return (
    <label style={{
      display: 'flex', alignItems: 'flex-start', gap: 9, padding: '9px 10px',
      // No background/radius of its own: a Toggle may sit alone or grouped with
      // other rows in one container, and the host owns that surface.
      cursor: 'pointer',
    }}>
      <span style={{ flex: 1, minWidth: 0 }}>
        <span style={{ display: 'block', fontSize: 11.5, fontWeight: 600 }}>{label}</span>
        <span style={{
          display: 'block', fontSize: 10, color: skin.muted, marginTop: 2, lineHeight: 1.35,
        }}>{hint}</span>
      </span>
      {/*
        A switch, not a checkbox. A checkbox reads as "select this item" (one of a
        set you then confirm); a switch reads as "this is on or off right now",
        which is what these are — they take effect immediately.

        Built from a real <input type="checkbox"> kept visually hidden rather than a
        <div role="switch">: that keeps native focus, keyboard toggling and label
        association for free instead of re-implementing them.
      */}
      <span style={{
        position: 'relative', flexShrink: 0, width: 30, height: 18, marginTop: 1,
      }}>
        <input
          type="checkbox"
          role="switch"
          checked={on}
          onChange={(e) => onChange(e.target.checked)}
          style={{
            position: 'absolute', inset: 0, width: '100%', height: '100%',
            margin: 0, opacity: 0, cursor: 'pointer',
          }}
        />
        <span
          aria-hidden
          style={{
            display: 'block', width: 30, height: 18, borderRadius: 999,
            background: on ? skin.accent : skin.hairline,
            transition: 'background 160ms ease',
          }}
        />
        <span
          aria-hidden
          style={{
            position: 'absolute', top: 3, left: on ? 15 : 3,
            width: 12, height: 12, borderRadius: '50%',
            background: on ? skin.onAccent : skin.muted,
            transition: 'left 160ms cubic-bezier(.4,.0,.4,1), background 160ms ease',
            pointerEvents: 'none',
          }}
        />
      </span>
    </label>
  )
}

export const SettingsView: React.FC = () => {
  // The interval picker below styles itself from the skin. Omitting this threw
  // `ReferenceError: skin is not defined` on render, which unmounted the whole card.
  const skin = useSkin()
  const [breakOn, setBreakOn] = useState(true)
  const [sessionOn, setSessionOn] = useState(true)
  const [breakMins, setBreakMins] = useState(45)
  // Draft for the custom field, kept separate from breakMins so a half-typed
  // number never becomes the live interval.
  /**
   * The in-progress custom interval. `null` means "not editing" — distinct from
   * `''`, which means the user has cleared the field and is about to type. Using
   * `''` for both made the field un-editable once a non-preset value was set: the
   * fallback re-displayed the stored number on every keystroke that emptied it.
   */
  const [customDraft, setCustomDraft] = useState<string | null>(null)

  /**
   * Read once on mount, then follow `config:updated`.
   *
   * The subscription is not redundant with the mount read: the SAME settings are
   * editable on the dashboard app page, and a change made there reaches this window
   * only as a broadcast. Without it, this view kept whatever it read when it opened
   * and its pills claimed an interval that was no longer set — the footer next to it
   * already followed the broadcast, so the panel would contradict itself.
   *
   * `apply` is shared by both paths so they cannot drift in which keys they honour.
   */
  useEffect(() => {
    const apply = (c: any) => {
      if (typeof c?.breakNudgesEnabled === 'boolean') setBreakOn(c.breakNudgesEnabled)
      if (typeof c?.sessionNotificationsEnabled === 'boolean') setSessionOn(c.sessionNotificationsEnabled)
      if (typeof c?.breakReminderMins === 'number') {
        setBreakMins(c.breakReminderMins)
        // Drop a half-typed draft: it describes a value the user is no longer
        // setting, and leaving it would show a number the config does not hold.
        setCustomDraft(null)
      }
    }
    api?.getCrewCompanionConfig?.().then(apply).catch(() => {})
    // The change notification carries no payload — re-read on each one.
    const off = api?.onConfigUpdated?.(() => {
      void api?.getCrewCompanionConfig?.().then(apply).catch(() => {})
    })
    return () => off?.()
  }, [])

  return (
    <div style={{ display: 'grid', gap: 7 }}>
      {/*
        Break nudges and their interval share one container: the interval exists
        only because the toggle above is on, so two separate cards implied two
        unrelated settings.
      */}
      <div style={{ background: skin.row, borderRadius: skin.rowRadius, overflow: 'hidden' }}>
        <Toggle
          on={breakOn}
          onChange={(v) => { setBreakOn(v); api?.updateConfig?.({ breakNudgesEnabled: v }) }}
          label={i18nT('apps.crewCompanion.view.breaksLabel')}
          hint={i18nT('apps.crewCompanion.view.breaksHint')}
        />
        {breakOn && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '8px 10px', borderTop: `1px solid ${skin.hairline}`,
          }}>
            <span style={{ flex: 1, fontSize: 11, color: skin.muted }}>{i18nT('apps.crewCompanion.view.everyHowOften')}</span>
            {BREAK_PRESETS.map((m) => {
              const active = breakMins === m
              return (
                <button
                  key={m}
                  onClick={() => {
                    setBreakMins(m); setCustomDraft(null)
                    api?.updateConfig?.({ breakReminderMins: m })
                  }}
                  aria-pressed={active}
                  style={{
                    minWidth: 30, height: 24, borderRadius: 999, border: 'none',
                    cursor: 'pointer', fontFamily: PANEL_FONT,
                    fontSize: 10, fontWeight: 700,
                    background: active ? skin.accent : 'transparent',
                    color: active ? skin.onAccent : skin.muted,
                  }}
                >{m}</button>
              )
            })}
            {/*
              Any interval, not just the four presets. It shows the live value when
              that value is custom, so the row always says what is actually set.
            */}
            <input
              type="number"
              min={BREAK_MIN_MINS}
              max={BREAK_MAX_MINS}
              aria-label={i18nT('apps.crewCompanion.view.everyCustom')}
              placeholder={i18nT('apps.crewCompanion.view.everyMinShort')}
              value={customDraft !== null ? customDraft
                : BREAK_PRESETS.includes(breakMins) ? '' : String(breakMins)}
              onFocus={() => setCustomDraft(
                BREAK_PRESETS.includes(breakMins) ? '' : String(breakMins),
              )}
              onChange={(e) => setCustomDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
              onBlur={() => {
                const next = clampBreakMins(customDraft ?? '')
                setCustomDraft(null)
                if (next === null) return
                setBreakMins(next)
                api?.updateConfig?.({ breakReminderMins: next })
              }}
              style={{
                width: 42, height: 24, borderRadius: 999, textAlign: 'center',
                fontFamily: PANEL_FONT, fontSize: 10, fontWeight: 700,
                background: BREAK_PRESETS.includes(breakMins) ? 'transparent' : skin.accent,
                color: BREAK_PRESETS.includes(breakMins) ? skin.muted : skin.onAccent,
                border: `1px solid ${skin.hairline}`,
                MozAppearance: 'textfield',
              }}
            />
          </div>
        )}
      </div>

      <div style={{ background: skin.row, borderRadius: skin.rowRadius, overflow: 'hidden' }}>
        <Toggle
          on={sessionOn}
          onChange={(v) => { setSessionOn(v); api?.updateConfig?.({ sessionNotificationsEnabled: v }) }}
          label={i18nT('apps.crewCompanion.view.sessionLabel')}
          hint={i18nT('apps.crewCompanion.view.sessionHint')}
        />
      </div>

      {/*
        A note about the whole section, not about the toggle above it. The caveat
        used to sit inside that toggle's hint, which made a short label read as an
        overall switch over every session notification. Stated once, out here, it
        applies to both groups and the label can stay plain.
      */}
      <div style={{
        fontSize: 10, color: skin.faint, lineHeight: 1.45, padding: '2px 10px 0',
      }}>{i18nT('apps.crewCompanion.view.notifyNote')}</div>
    </div>
  )
}


/**
 * Guard around a panel view.
 *
 * Without this, one bad render emptied #root: the window stayed open but painted
 * nothing, so the panel looked gone AND the pet's click toggled an already-open
 * window instead of showing it. A visible failure is far better than a blank one.
 */
export class ViewBoundary extends React.Component<
  { children: React.ReactNode; onBack: () => void; label: string; back: string },
  { failed: boolean }
> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(err: unknown) {
    console.error('[panel] view failed to render:', err)
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div style={{ padding: '10px 2px', fontSize: 11, lineHeight: 1.5 }}>
        <div style={{ fontWeight: 600, marginBottom: 6 }}>{this.props.label}</div>
        <button
          onClick={this.props.onBack}
          style={{
            fontSize: 11, padding: '5px 10px', borderRadius: 999, border: 'none',
            cursor: 'pointer', fontFamily: PANEL_FONT,
          }}
        >{this.props.back}</button>
      </div>
    )
  }
}
