import { useId, useState } from 'react'
import { Play, Plus, Trash2, X } from 'lucide-react'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { Btn, IconButton, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import {
  CUSTOM_TONE_LIMITS, validateCustomTone, playPreset, customSoundId,
  type CustomTones, type ToneStep,
} from '../../hooks/useNotificationSound'
import { i18nT } from '../../i18n/t'

/** One editable tone row. Kept as the strings the user typed, so a half-typed
 *  number ("0.") is not rewritten under the cursor. */
interface ToneRow { freq: string; start: string; dur: string; gain: string }

/** A new sound starts as a short two-note chime the user can hear and change,
 *  so the form is never empty. */
const STARTER_ROWS: ToneRow[] = [
  { freq: '523', start: '0', dur: '0.2', gain: '1' },
  { freq: '784', start: '0.2', dur: '0.3', gain: '0.9' },
]

/** Empty or non-numeric text becomes NaN, which the validator rejects. */
const toNumber = (s: string): number => (s.trim() === '' ? Number.NaN : Number(s))
const rowsToTones = (rows: ToneRow[]): ToneStep[] =>
  rows.map(r => ({ freq: toNumber(r.freq), start: toNumber(r.start), dur: toNumber(r.dur), gain: toNumber(r.gain) }))

const L = CUSTOM_TONE_LIMITS

/** The id a draft is auditioned under; it is never stored. */
const PREVIEW_NAME = 'preview'

/** Field codes `validateCustomTone` reports for a single tone. */
const ROW_FIELDS = new Set<string>(['freq', 'start', 'dur', 'gain'])
/** Codes about the name, shown under the Name field. */
const NAME_CODES = new Set<string>(['name', 'name_taken'])

/** The fields of one tone that break a bound. Taken from `validateCustomTone`
 *  on that tone alone, so the marks can never disagree with what saving checks. */
const badFields = (t: ToneStep): Array<keyof ToneRow> =>
  validateCustomTone(PREVIEW_NAME, [t]).filter(p => ROW_FIELDS.has(p)) as Array<keyof ToneRow>

/** Catalog keys for each problem code `validateCustomTone` returns, as full
 *  literals so the i18n key check can resolve them. */
const PROBLEM_KEY: Record<string, string> = {
  name: 'pages.settings.notificationsPanel.custom_sound_error_name',
  name_taken: 'pages.settings.notificationsPanel.custom_sound_error_name_taken',
  count: 'pages.settings.notificationsPanel.custom_sound_error_count',
  freq: 'pages.settings.notificationsPanel.custom_sound_error_freq',
  dur: 'pages.settings.notificationsPanel.custom_sound_error_dur',
  gain: 'pages.settings.notificationsPanel.custom_sound_error_gain',
  start: 'pages.settings.notificationsPanel.custom_sound_error_start',
  too_many: 'pages.settings.notificationsPanel.custom_sound_error_too_many',
}

const problemText = (code: string): string => i18nT(PROBLEM_KEY[code], {
  max: code === 'name' ? L.maxNameLength
    : code === 'count' ? L.maxTones
      : code === 'freq' ? L.maxFreq
        : code === 'dur' ? L.maxDur
          : code === 'start' ? L.maxLength
            : L.maxSounds,
  min: code === 'freq' ? L.minFreq : L.minDur,
})

/** Columns of a tone row: field, catalog label key, input step. */
const FIELDS: Array<{ field: keyof ToneRow; label: string; step: string }> = [
  { field: 'freq', label: 'pages.settings.notificationsPanel.custom_tone_pitch', step: '1' },
  { field: 'start', label: 'pages.settings.notificationsPanel.custom_tone_start', step: '0.05' },
  { field: 'dur', label: 'pages.settings.notificationsPanel.custom_tone_length', step: '0.05' },
  { field: 'gain', label: 'pages.settings.notificationsPanel.custom_tone_volume', step: '0.1' },
]

/** What an undo came to: restored, refused because the user is at the sound
 *  limit or has since used the name, or the save failed. */
export type UndoResult = 'ok' | 'full' | 'taken' | 'failed'

/** Which save failed, for the one error notice the pane shows. Refusals by
 *  the bounds are hints, never this notice. */
type Failure = 'add' | 'delete' | 'undo'
const FAILURE_KEY: Record<Failure, string> = {
  add: 'pages.settings.notificationsPanel.custom_sound_error_save',
  delete: 'pages.settings.notificationsPanel.custom_sound_error_delete',
  undo: 'pages.settings.notificationsPanel.custom_sound_error_undo',
}

/**
 * Add, audition and delete the user's own named sounds. A saved sound is
 * listed in every sound picker on this page beside the built-ins.
 */
export function CustomSoundsSection({ customTones, volume, enabled, onAdd, onRemove }: {
  customTones: CustomTones
  volume: number
  enabled: boolean
  /** Saves the sound, checked against what is saved now. Returns the problems
   *  found (empty when saved), or null when the save itself failed. */
  onAdd: (name: string, tones: ToneStep[]) => string[] | null
  /** Deletes the sound; returns an undo that restores it, or null when the
   *  delete could not be saved. */
  onRemove: (name: string) => (() => UndoResult) | null
}) {
  const [name, setName] = useState('')
  const [rows, setRows] = useState<ToneRow[]>(STARTER_ROWS)
  /** Problems not tied to one tone row: name, tone count, sound limit. */
  const [hints, setHints] = useState<string[]>([])
  /** Problems with the name, shown under the Name field. */
  const [nameHints, setNameHints] = useState<string[]>([])
  /** Mark bad fields only once the user has tried to listen or save. */
  const [checked, setChecked] = useState(false)
  const [failure, setFailure] = useState<Failure | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const [removed, setRemoved] = useState<{ name: string; undo: () => UndoResult } | null>(null)
  /** Why Undo was refused by a bound (limit reached, name since used). */
  const [undoHint, setUndoHint] = useState<string | null>(null)
  const names = Object.keys(customTones)
  const nameId = useId()
  const hintsId = useId()
  const nameHintId = useId()
  const canPlay = enabled && volume > 0
  const tones = rowsToTones(rows)
  const bad = checked ? tones.map(badFields) : []
  const hasRowProblem = bad.some(b => b.length > 0)
  /** The form still holds the example it starts from, not a saved sound. */
  const isStarter = rows.length === STARTER_ROWS.length
    && rows.every((r, i) => (Object.keys(r) as Array<keyof ToneRow>).every(k => r[k] === STARTER_ROWS[i][k]))

  /** Any edit clears the hints, so "Add sound" is never off beside a stale one. */
  const edited = () => { setHints([]); setNameHints([]); setFailure(null); setSaved(null) }
  const setCell = (i: number, field: keyof ToneRow, value: string) => {
    setRows(rs => rs.map((r, j) => (j === i ? { ...r, [field]: value } : r)))
    edited()
  }

  /** Whole-sound problems; per-tone bounds are shown under their own row and
   *  name problems under the Name field. */
  const generalHints = (problems: string[]) =>
    problems.filter(p => !ROW_FIELDS.has(p) && !NAME_CODES.has(p)).map(problemText)
  const showProblems = (problems: string[]) => {
    setHints(generalHints(problems))
    setNameHints(problems.filter(p => NAME_CODES.has(p)).map(problemText))
  }

  const preview = () => {
    const problems = validateCustomTone(PREVIEW_NAME, tones)
    setChecked(true)
    setSaved(null)
    setHints(generalHints(problems))
    if (problems.length === 0) playPreset(customSoundId(PREVIEW_NAME), volume, { [PREVIEW_NAME]: tones })
  }

  const add = () => {
    const trimmed = name.trim()
    const problems = validateCustomTone(trimmed, tones, customTones)
    setChecked(true)
    setFailure(null)
    setSaved(null)
    if (problems.length > 0) {
      showProblems(problems)
      return
    }
    const saveProblems = onAdd(trimmed, tones)
    if (saveProblems === null) {
      setFailure('add')
      return
    }
    if (saveProblems.length > 0) {
      showProblems(saveProblems)
      return
    }
    showProblems([])
    setChecked(false)
    setName('')
    setRows(STARTER_ROWS)
    setSaved(trimmed)
    if (!removed) setUndoHint(null)
    // A sound saved under the deleted name replaces it, so its Undo would
    // only be refused: drop it rather than leave a notice the list contradicts.
    if (removed && removed.name.toLowerCase() === trimmed.toLowerCase()) {
      setRemoved(null)
      setUndoHint(null)
    }
  }

  const remove = (n: string) => {
    const undo = onRemove(n)
    setSaved(null)
    setUndoHint(null)
    setFailure(undo ? null : 'delete')
    setRemoved(undo ? { name: n, undo } : null)
  }

  const undoRemove = () => {
    if (!removed) return
    const result = removed.undo()
    setFailure(result === 'failed' ? 'undo' : null)
    if (result === 'ok') {
      setRemoved(null)
      setUndoHint(null)
    } else if (result !== 'failed') {
      // A bound refused it, so it is a hint, not an error.
      setUndoHint(problemText(result === 'full' ? 'too_many' : 'name_taken'))
      // A used name stays used, so a second Undo would be refused again.
      // At the limit, deleting another sound makes room, so Undo stays.
      if (result === 'taken') setRemoved(null)
    }
  }

  return (
    <SettingsSection title={i18nT('pages.settings.notificationsPanel.custom_sounds')}>
      <SettingsCard>
        <div className="text-[12px] text-muted">{i18nT('pages.settings.notificationsPanel.custom_sounds_description')}</div>
        {names.length === 0 ? (
          <div className="text-[13px] text-muted py-1.5">{i18nT('pages.settings.notificationsPanel.custom_sound_none')}</div>
        ) : (
          <ul className="flex flex-col gap-1 py-1.5" aria-label={i18nT('pages.settings.notificationsPanel.custom_sounds')}>
            {names.map(n => (
              <li key={n} className="flex items-center gap-2">
                <span className="flex-1 min-w-0 truncate text-[13px] text-text">{n}</span>
                <IconButton
                  aria-label={i18nT('pages.settings.notificationsPanel.custom_sound_play', { name: n })}
                  onClick={() => playPreset(customSoundId(n), volume, customTones)}
                  disabled={!canPlay}
                >
                  <Play size={14} />
                </IconButton>
                <IconButton
                  variant="danger"
                  aria-label={i18nT('pages.settings.notificationsPanel.custom_sound_delete', { name: n })}
                  onClick={() => remove(n)}
                >
                  <Trash2 size={14} />
                </IconButton>
              </li>
            ))}
          </ul>
        )}
        {removed && (
          <div role="status" className="flex flex-col gap-1 py-1.5 text-[13px] text-text">
            <div className="flex items-center gap-2">
              <span className="flex-1 min-w-0">{i18nT('pages.settings.notificationsPanel.custom_sound_deleted', { name: removed.name })}</span>
              <Btn type="button" onClick={undoRemove}>
                {i18nT('pages.settings.notificationsPanel.custom_sound_undo')}
              </Btn>
            </div>
            {undoHint && <div className="text-[12px]">{undoHint}</div>}
          </div>
        )}
        {!removed && undoHint && (
          <div role="status" className="py-1.5 text-[12px] text-text">{undoHint}</div>
        )}
        {(failure === 'delete' || failure === 'undo') && (
          /* Beside the list it is about. No hand-off: a hand-off leaves the
             page, losing the unsaved draft name and tone rows in the add form
             below. */
          <ErrorNotice message={i18nT(FAILURE_KEY[failure])} />
        )}
      </SettingsCard>
      <SettingsCard index={1}>
        {/* Plain fields, not Settings* primitives: this is a form that creates
            an entry, so nothing here is a setting search should deep-link to. */}
        <div className="flex flex-col gap-1.5 py-1.5">
          <label htmlFor={nameId} className="text-[13px] font-semibold text-text">{i18nT('pages.settings.notificationsPanel.custom_sound_name')}</label>
          <Input
            id={nameId}
            value={name}
            maxLength={CUSTOM_TONE_LIMITS.maxNameLength}
            className={nameHints.length > 0 ? 'border-danger' : undefined}
            aria-invalid={nameHints.length > 0 || undefined}
            aria-describedby={nameHints.length > 0 ? nameHintId : undefined}
            onChange={e => { setName(e.target.value); edited() }}
          />
          {/* The name hint sits right under the field it is about. */}
          <ul id={nameHintId} aria-live="polite" className="text-[12px] text-text">
            {nameHints.map(h => <li key={h}>{h}</li>)}
          </ul>
        </div>
        <fieldset className="flex flex-col gap-1.5 py-1.5" aria-describedby={hintsId}>
          <legend className="text-[13px] font-semibold text-text">{i18nT('pages.settings.notificationsPanel.custom_sound_tones')}</legend>
          <div className="text-[12px] text-muted">{i18nT('pages.settings.notificationsPanel.custom_sound_tones_hint')}</div>
          {isStarter && (
            <div className="text-[12px] text-muted">{i18nT('pages.settings.notificationsPanel.custom_sound_starter')}</div>
          )}
          {rows.map((row, i) => {
            const rowBad = bad[i] ?? []
            const rowHintId = `${hintsId}-row-${i}`
            return (
              <div key={i} className="flex flex-col gap-1" role="group" aria-label={i18nT('pages.settings.notificationsPanel.custom_tone_row', { n: i + 1 })}>
                {/* A visible heading per tone, with its remove button on the same
                    line, so it is clear which tone the button removes. */}
                <div className="flex items-center gap-2">
                  <span className="flex-1 text-[12px] font-semibold text-text">{i18nT('pages.settings.notificationsPanel.custom_tone_row', { n: i + 1 })}</span>
                  <IconButton
                    aria-label={i18nT('pages.settings.notificationsPanel.custom_tone_remove', { n: i + 1 })}
                    onClick={() => { setRows(rs => rs.filter((_, j) => j !== i)); edited() }}
                    disabled={rows.length <= 1}
                  >
                    <X size={14} />
                  </IconButton>
                </div>
                {/* Two fields per line on a narrow pane, all four in one line from md up. */}
                <div className="grid grid-cols-2 md:grid-cols-4 items-end gap-2">
                  {FIELDS.map(({ field, label, step }) => {
                    const invalid = rowBad.includes(field)
                    return (
                      <label key={field} className="flex min-w-0 flex-col gap-0.5 text-[12px] text-muted">
                        {i18nT(label)}
                        <Input
                          type="number"
                          inputMode="decimal"
                          className={invalid ? 'w-full border-danger' : 'w-full'}
                          aria-invalid={invalid || undefined}
                          aria-describedby={invalid ? rowHintId : undefined}
                          step={step}
                          value={row[field]}
                          onChange={e => setCell(i, field, e.target.value)}
                        />
                      </label>
                    )
                  })}
                </div>
                {/* The hint sits right under the row it is about. */}
                {rowBad.length > 0 && (
                  <ul id={rowHintId} className="text-[12px] text-text">
                    {rowBad.map(p => <li key={p}>{problemText(p)}</li>)}
                  </ul>
                )}
              </div>
            )
          })}
          <div>
            <Btn type="button" onClick={() => { setRows(rs => [...rs, { ...rs[rs.length - 1] ?? STARTER_ROWS[0] }]); edited() }} disabled={rows.length >= L.maxTones}>
              <Plus size={14} /> {i18nT('pages.settings.notificationsPanel.custom_tone_add')}
            </Btn>
          </div>
        </fieldset>
        {/* Validation hints, not errors: nothing has failed yet. */}
        <ul id={hintsId} aria-live="polite" className="text-[12px] text-text">
          {hints.map(h => <li key={h}>{h}</li>)}
        </ul>
        {failure === 'add' && (
          /* No hand-off: the name and tones typed above are an unsaved draft. */
          <ErrorNotice message={i18nT(FAILURE_KEY.add)} />
        )}
        {saved && (
          <div role="status" className="text-[12px] text-text">
            {i18nT('pages.settings.notificationsPanel.custom_sound_saved', { name: saved })}
          </div>
        )}
        <div className="flex gap-2 py-1.5">
          <Btn type="button" onClick={preview} disabled={!canPlay}>
            <Play size={14} /> {i18nT('pages.settings.notificationsPanel.test')}
          </Btn>
          {/* Off only while a shown problem still stands; an empty name is
              reported on click rather than leaving the button off unexplained. */}
          <Btn type="button" onClick={add} disabled={hints.length > 0 || nameHints.length > 0 || hasRowProblem}>
            {i18nT('pages.settings.notificationsPanel.custom_sound_add')}
          </Btn>
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}
