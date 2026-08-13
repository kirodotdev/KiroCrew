/**
 * The natural-language add row — shared by the panel's main view and the full
 * reminders list.
 *
 * Extracted rather than duplicated: it owns parsing, the ask-for-a-time fallback and
 * the confirmation wording. A second copy would be a second place for "Got it —
 * every 2h" to drift from what was actually saved.
 */
import React, { useCallback, useState } from 'react'
import { Plus } from 'lucide-react'
import { useSkin, PANEL_FONT } from './panelSkin'
import { i18nT } from '../../i18n/t'
import { fmtTime, fmtUnit, fmtDateFields } from '../../i18n/format'
import { parseReminder } from './reminderParse'

const FONT = PANEL_FONT

interface Notice { kind: 'ok' | 'ask' | 'error'; message: string }

/**
 * Offered when the parser found no time signal at all.
 *
 * These deliberately span BOTH kinds. An earlier version offered only intervals
 * (1h/3h/daily), which meant a one-off task like "buy milk" could only be saved as
 * a repeating reminder — the picker forced the wrong kind on it.
 */
interface AskChoice {
  /**
   * FULL catalog key, so the pill follows the UI language.
   *
   * Fully namespaced deliberately. Ported as the desktop app's own root-relative
   * `'panel.ask.in1h'`, it resolved to nothing here — i18next answers a missing key
   * with the key itself, so the three pills rendered the literal strings
   * "panel.ask.in1h", "panel.ask.tomorrow", "panel.ask.daily" on screen. Nothing
   * failed: the label was wrong but the button still worked, which is why it reached
   * a user rather than a gate. The key checker could not catch it either — it reads
   * the literal at the `i18nT` call site, and this one arrives through a variable.
   */
  key:
    | 'apps.crewCompanion.panel.ask.in1h'
    | 'apps.crewCompanion.panel.ask.tomorrow'
    | 'apps.crewCompanion.panel.ask.daily'
  /** null for a one-time reminder. */
  everyMinutes: number | null
  /** When it first fires. */
  at: (now: Date) => Date
}

const ASK_CHOICES: ReadonlyArray<AskChoice> = [  {
    key: 'apps.crewCompanion.panel.ask.in1h' as const, everyMinutes: null,
    at: (n) => new Date(n.getTime() + 60 * 60_000),
  },
  {
    key: 'apps.crewCompanion.panel.ask.tomorrow' as const, everyMinutes: null,
    at: (n) => { const d = new Date(n); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); return d },
  },
  {
    key: 'apps.crewCompanion.panel.ask.daily' as const, everyMinutes: 1440,
    at: (n) => new Date(n.getTime() + 1440 * 60_000),
  },
]

/**
 * The pill keys, exported so a test can prove each one resolves.
 *
 * Exported rather than re-listed in the test: a copy would drift, and drift is the
 * whole defect being guarded against.
 */
export const ASK_CHOICE_KEYS = ASK_CHOICES.map((c) => c.key)

/**
 * Short confirmation of when something will fire.
 *
 * Separate from `labelFor` in shared/reminders: that formats a row in a list of
 * pending items, whereas this reads back a just-made decision, where the repeat is
 * the salient part ("every 2h" matters more than the first fire time).
 */
function confirmLabel(
  fireAt: Date, everyMinutes: number | undefined, now: Date,
): string {
  const time = fmtTime(fireAt)
  if (everyMinutes) {
    if (everyMinutes === 1440) return i18nT('apps.crewCompanion.panel.confirm.daily', { time })
    // fmtUnit rather than `${n}h`: the digits localize, the unit comes from the
    // locale's own narrow form, and the gap between them is whatever that locale
    // uses -- a welded Latin suffix is none of those things.
    const unit = everyMinutes % 1440 === 0
      ? fmtUnit(everyMinutes / 1440, 'day')
      : everyMinutes % 60 === 0
        ? fmtUnit(everyMinutes / 60, 'hour')
        : fmtUnit(everyMinutes, 'minute')
    return i18nT('apps.crewCompanion.panel.confirm.every', { unit })
  }
  const sameDay = fireAt.toDateString() === now.toDateString()
  return sameDay ? time : `${fmtDateFields(fireAt, { weekday: 'short' })} ${time}`
}

export const ReminderInput: React.FC<{
  /** Persist a reminder. Resolves true on success. */
  onAdd?: (text: string, fireAtIso: string, everyMinutes?: number) => Promise<boolean>
}> = ({ onAdd }) => {
  const skin = useSkin()
  const [draft, setDraft] = useState('')
  const [notice, setNotice] = useState<Notice | null>(null)
  /**
   * Parse the draft and persist it.
   *
   * `choice` is supplied only when the user picks from the ask row — the parser
   * found no time signal, so rather than guessing we asked. The choice carries the
   * KIND too, so a one-off stays a one-off.
   */
  const submitDraft = useCallback(async (choice?: AskChoice) => {
    const raw = draft.trim()
    if (!raw) return

    const now = new Date()
    const parsed = parseReminder(raw, now)

    let fireAtIso = parsed.fireAt
    let every = parsed.recurrence?.everyMinutes

    if (choice) {
      every = choice.everyMinutes ?? undefined
      fireAtIso = choice.at(now).toISOString()
    } else if (parsed.needsSchedule) {
      setNotice({ kind: 'ask', message: i18nT('apps.crewCompanion.panel.ask.when', { text: parsed.text }) })
      return
    }

    if (!fireAtIso) return
    const ok = await onAdd?.(parsed.text, fireAtIso, every)
    if (!ok) {
      setNotice({ kind: 'error', message: i18nT('apps.crewCompanion.panel.confirm.error') })
      return
    }

    setDraft('')
    setNotice({
      kind: 'ok',
      message: i18nT('apps.crewCompanion.panel.confirm.ok', {
        when: confirmLabel(new Date(fireAtIso), every, now),
      }),
    })
  }, [draft, onAdd])

  return (
    <>
        <form
          onSubmit={(e) => { e.preventDefault(); submitDraft() }}
          style={{
            /**
             * Full width, no horizontal margin of its own.
             *
             * This used to carry `margin: 9px 14px`, which was right for the main
             * card (no padding on that section) and wrong inside the full list,
             * whose container already pads 12px — the composer ended up inset 26px
             * while the rows below it sat at 12px. Horizontal inset is the HOST's
             * decision; the component only owns its vertical rhythm.
             */
            display: 'flex', alignItems: 'center', gap: 8, width: '100%',
            margin: '9px 0', background: skin.row,
            borderRadius: skin.rowRadius, padding: '9px 10px',
          }}
        >
          <input
            value={draft}
            onChange={(e) => { setDraft(e.target.value); setNotice(null) }}
            placeholder={i18nT('apps.crewCompanion.panel.add.placeholder')}
            aria-label={i18nT('apps.crewCompanion.panel.add.aria')}
            style={{
              flex: 1, fontSize: 11.5, color: skin.ink, background: 'none',
              border: 'none', outline: 'none', fontFamily: FONT, minWidth: 0,
            }}
          />
          <button
            type="submit"
            aria-label={i18nT('apps.crewCompanion.panel.add.submit')}
            disabled={!draft.trim()}
            style={{
              // 26px, not 20px: a 20px box with a 50% radius is under the 24px
              // minimum AND has dead corners outside the circle. This one is
              // filled, so the circle is the visible control — 26px is the
              // smallest size that clears the minimum without looking heavy.
              width: 26, height: 26, borderRadius: '50%', background: skin.accent,
              color: skin.onAccent, display: 'inline-flex', alignItems: 'center',
              justifyContent: 'center', fontSize: 13, fontWeight: 700, border: 'none',
              cursor: draft.trim() ? 'pointer' : 'default',
              opacity: draft.trim() ? 1 : 0.45, fontFamily: FONT, padding: 0,
            }}
          >
            <Plus size={14} className="lucide-inline" aria-hidden="true" />
          </button>
        </form>

        {/*
          Confirmation lives here rather than as a phantom UP NEXT row: a reminder
          for next Tuesday would otherwise animate into a list headed "up next" and
          then vanish on the next refresh. When the parser could not find a time
          this is also where it ASKS, instead of inventing one.
        */}
        {notice && (
          <div
            role="status"
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              margin: '-4px 0 6px', fontSize: 10.5,
              color: notice.kind === 'ask' ? skin.ink : skin.accentText,
            }}
          >
            <span style={{ flex: 1 }}>{notice.message}</span>
            {notice.kind === 'ask' && (
              <>
                {ASK_CHOICES.map(c => (
                  <button
                    key={c.key}
                    onClick={() => submitDraft(c)}
                    style={{
                      fontSize: 10, fontWeight: 700, color: skin.onAccent,
                      background: skin.accent, border: 'none', borderRadius: 5,
                      padding: '2px 6px', cursor: 'pointer', fontFamily: FONT,
                    }}
                  >{i18nT(c.key)}</button>
                ))}
              </>
            )}
          </div>
        )}
    </>
  )
}
