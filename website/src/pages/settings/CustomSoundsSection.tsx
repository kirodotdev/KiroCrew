import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { Play, Plus, Trash2, X } from 'lucide-react'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { Btn, IconButton, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import {
  CUSTOM_TONE_LIMITS, CUSTOM_FILE_LIMITS, validateCustomTone, validateCustomFile, allCustomSounds,
  loadSoundSettings, playPreset, playSoundFile, customSoundId, fileSoundId,
  type CustomTones, type CustomFiles, type CustomFileMeta, type ToneStep,
} from '../../hooks/useNotificationSound'
import { isPastGrace, pruneSoundFiles, readAudioFile } from '../../hooks/customSoundFiles'
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
  file_size: 'pages.settings.notificationsPanel.custom_sound_error_file_size',
  file_length: 'pages.settings.notificationsPanel.custom_sound_error_file_length',
  file_decode: 'pages.settings.notificationsPanel.custom_sound_error_file_decode',
  file_gone: 'pages.settings.notificationsPanel.custom_sound_error_file_gone',
}

const problemText = (code: string): string => i18nT(PROBLEM_KEY[code], {
  max: code === 'name' ? L.maxNameLength
    : code === 'count' ? L.maxTones
      : code === 'freq' ? L.maxFreq
        : code === 'dur' ? L.maxDur
          : code === 'start' ? L.maxLength
            : code === 'file_size' ? CUSTOM_FILE_LIMITS.maxBytes / 1024
              : code === 'file_length' ? CUSTOM_FILE_LIMITS.maxSeconds
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
export type UndoResult = 'ok' | 'full' | 'taken' | 'gone' | 'failed'

/** Which save failed, for the one error notice the pane shows. Refusals by
 *  the bounds are hints, never this notice. */
type Failure = 'add' | 'delete' | 'undo'
const FAILURE_KEY: Record<Failure, string> = {
  add: 'pages.settings.notificationsPanel.custom_sound_error_save',
  delete: 'pages.settings.notificationsPanel.custom_sound_error_delete',
  undo: 'pages.settings.notificationsPanel.custom_sound_error_undo',
}

/**
 * One titled card of the pane, exposed as a named region so a screen reader
 * (and a test) can tell the two ways of making a sound apart: both forms use
 * the same field and button words on purpose, and the card title is what says
 * which way the user is using.
 */
function TitledCard({ index, title, children }: { index?: number; title: string; children: ReactNode }) {
  const headingId = useId()
  return (
    <SettingsCard index={index}>
      {/* The title sits a step above the field labels, with room above every
          card (the first included, so it does not read as part of the pane's
          description), so a card title never reads as one more field. */}
      <section aria-labelledby={headingId} className="mt-4 flex flex-col gap-1.5">
        <h3 id={headingId} className="text-[15px] font-semibold text-text">{title}</h3>
        {children}
      </section>
    </SettingsCard>
  )
}

/** "Saved <name>." plus the next step: a sound is only heard once a category
 *  is set to it, so the confirmation leads straight to where that is done. */
function SavedNotice({ name, onChooseWhereItPlays }: { name: string; onChooseWhereItPlays: () => void }) {
  return (
    <div role="status" className="flex flex-wrap items-center gap-x-1.5 text-[12px] text-text">
      <span>{i18nT('pages.settings.notificationsPanel.custom_sound_saved', { name })}</span>
      <Btn
        type="button"
        className="rounded-sm border-none bg-transparent px-0 py-0 text-[12px] text-accent hover:bg-transparent hover:underline focus-ring"
        onClick={onChooseWhereItPlays}
      >
        {i18nT('pages.settings.notificationsPanel.custom_sound_choose_where')}
      </Btn>
    </div>
  )
}

/**
 * Add, audition and delete the user's own named sounds: from an audio file
 * picked from disk, or built here from tones. A saved sound is listed in every
 * sound picker under Per-category sounds beside the built-ins.
 */
export function CustomSoundsSection({ customTones, customFiles, volume, enabled, onAdd, onAddFile, onRemove, onMissingFiles, onChooseWhereItPlays }: {
  customTones: CustomTones
  customFiles: CustomFiles
  volume: number
  enabled: boolean
  /** Saves the sound, checked against what is saved now. Returns the problems
   *  found (empty when saved), or null when the save itself failed. */
  onAdd: (name: string, tones: ToneStep[]) => string[] | null
  /** Saves a sound file the same way, its bytes in this browser only. */
  onAddFile: (name: string, file: Blob, meta: CustomFileMeta) => Promise<string[] | null>
  /** Deletes the sound; returns an undo that restores it, or null when the
   *  delete could not be saved. */
  onRemove: (name: string) => (() => UndoResult | Promise<UndoResult>) | null
  /** Forgets sound files whose bytes are not in this browser. */
  onMissingFiles: (keys: ReadonlySet<string>) => void
  /** Opens Per-category sounds, where a saved sound is set to play. */
  onChooseWhereItPlays: () => void
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
  const [removed, setRemoved] = useState<{ name: string; undo: () => UndoResult | Promise<UndoResult> } | null>(null)
  /** The delete notice showing now, readable from a slow Undo's callback. */
  const removedRef = useRef(removed)
  useEffect(() => { removedRef.current = removed }, [removed])
  /** Why Undo was refused by a bound (limit reached, name since used). */
  const [undoHint, setUndoHint] = useState<string | null>(null)
  /** A sound file's Undo is checking its bytes; Undo is off meanwhile. */
  const [undoBusy, setUndoBusy] = useState(false)
  const names = [...Object.keys(customTones), ...Object.keys(customFiles)]
  const existing = allCustomSounds({ customTones, customFiles })
  const nameId = useId()
  const hintsId = useId()
  const nameHintId = useId()
  const canPlay = enabled && volume > 0
  const tones = rowsToTones(rows)

  // Deleting a sound file keeps its bytes so Undo can restore them. Opening the
  // pane is when the bytes and the records are brought back in line: orphaned
  // bytes are deleted, and a record whose bytes are not in this browser
  // (settings restored from a backup made elsewhere) is dropped rather than
  // listed as a sound that plays nothing. Records are re-read at every step,
  // because another tab may add or undo a sound while this runs, and a record
  // too new for its bytes to be settled is never judged missing.
  useEffect(() => {
    const referencedKeys = () => new Set(Object.values(loadSoundSettings().customFiles ?? {}).map(m => m.key))
    void pruneSoundFiles(referencedKeys).then(stored => {
      if (!stored) return
      const present = new Set(stored)
      const now = Date.now()
      const missing = new Set([...referencedKeys()].filter(k => !present.has(k) && isPastGrace(k, now)))
      if (missing.size > 0) onMissingFiles(missing)
    })
    // Once per opening of the pane, by design.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
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
    const problems = validateCustomTone(trimmed, tones, existing)
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
    // One Undo at a time: the button is off while one runs, so a second click
    // while a sound file's bytes are being checked cannot run a second restore.
    if (!removed) return
    // The delete this Undo belongs to. Delete and Save stay usable while a
    // slow Undo runs, so by the time it answers the notice may be about another
    // sound, or gone: then this Undo's answer is about nothing on screen, and
    // saying it would pin Door's outcome on Beep's notice.
    const target = removed
    const handle = (result: UndoResult) => {
      setUndoBusy(false)
      if (removedRef.current !== target) return
      setFailure(result === 'failed' ? 'undo' : null)
      if (result === 'ok') {
        setRemoved(null)
        setUndoHint(null)
      } else if (result !== 'failed') {
        // A bound refused it, or the file is gone, so it is a hint, not an error.
        setUndoHint(problemText(result === 'full' ? 'too_many' : result === 'gone' ? 'file_gone' : 'name_taken'))
        // A used name stays used and a gone file stays gone, so a second Undo
        // would be refused again. At the limit, deleting another sound makes
        // room, so Undo stays.
        if (result !== 'full') setRemoved(null)
      }
    }
    // A tone sound's Undo answers at once; a sound file's first checks its
    // bytes are still stored.
    const result = target.undo()
    if (typeof result === 'string') {
      handle(result)
      return
    }
    setUndoBusy(true)
    void result.then(handle, () => handle('failed'))
  }

  return (
    <SettingsSection title={i18nT('pages.settings.notificationsPanel.custom_sounds')}>
      <div className="text-[12px] text-muted">{i18nT('pages.settings.notificationsPanel.custom_sounds_description')}</div>
      <TitledCard title={i18nT('pages.settings.notificationsPanel.custom_sound_list_title')}>
        {names.length === 0 ? (
          <div className="text-[13px] text-muted py-1.5">{i18nT('pages.settings.notificationsPanel.custom_sound_none')}</div>
        ) : (
          <ul className="flex flex-col gap-1 py-1.5" aria-label={i18nT('pages.settings.notificationsPanel.custom_sound_list_title')}>
            {names.map(n => {
              const isFile = Object.hasOwn(customFiles, n)
              return (
                <li key={n} className="flex items-center gap-2">
                  <span className="min-w-0 truncate text-[13px] text-text">{n}</span>
                  {/* Which way it was made, so the two kinds are told apart. */}
                  <span className="flex-1 text-[11px] text-muted">
                    {i18nT(isFile ? 'pages.settings.notificationsPanel.custom_sound_kind_file' : 'pages.settings.notificationsPanel.custom_sound_kind_tones')}
                  </span>
                  <IconButton
                    aria-label={i18nT('pages.settings.notificationsPanel.custom_sound_play', { name: n })}
                    onClick={() => playPreset(isFile ? fileSoundId(n) : customSoundId(n), volume, customTones)}
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
              )
            })}
          </ul>
        )}
        {removed && (
          <div role="status" className="flex flex-col gap-1 py-1.5 text-[13px] text-text">
            <div className="flex items-center gap-2">
              <span className="flex-1 min-w-0">{i18nT('pages.settings.notificationsPanel.custom_sound_deleted', { name: removed.name })}</span>
              <Btn type="button" onClick={undoRemove} disabled={undoBusy}>
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
             page, losing the unsaved drafts in the two forms below. */
          <ErrorNotice message={i18nT(FAILURE_KEY[failure])} />
        )}
      </TitledCard>
      {/* The file card comes first: picking a file is the quicker way. */}
      <SoundFileForm
        existing={existing}
        volume={volume}
        canPlay={canPlay}
        onAddFile={onAddFile}
        onChooseWhereItPlays={onChooseWhereItPlays}
        onSaved={n => {
          // As for a tone sound: a sound saved under the deleted name replaces
          // it, so its Undo would only be refused.
          if (removed && removed.name.toLowerCase() === n.toLowerCase()) {
            setRemoved(null)
            setUndoHint(null)
          }
        }}
      />
      <TitledCard index={2} title={i18nT('pages.settings.notificationsPanel.custom_sound_tones_title')}>
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
        <div className="flex gap-2 py-1.5">
          <Btn type="button" onClick={preview} disabled={!canPlay}>
            <Play size={14} /> {i18nT('pages.settings.notificationsPanel.custom_sound_preview')}
          </Btn>
          {/* Off only while a shown problem still stands; an empty name is
              reported on click rather than leaving the button off unexplained. */}
          <Btn type="button" primary onClick={add} disabled={hints.length > 0 || nameHints.length > 0 || hasRowProblem}>
            {i18nT('pages.settings.notificationsPanel.custom_sound_add')}
          </Btn>
        </div>
        {/* Below the buttons, as in the file card. */}
        {saved && <SavedNotice name={saved} onChooseWhereItPlays={onChooseWhereItPlays} />}
      </TitledCard>
    </SettingsSection>
  )
}

/**
 * Add a sound from an audio file on disk. The file is checked (size, length,
 * playable) as soon as it is picked, can be heard before it is saved, and is
 * kept in this browser only.
 *
 * Its field and button words are the tone form's ("Name", "Play", "Save
 * sound"): the card title says which way the user is making a sound, so one
 * set of words is all there is to learn.
 */
function SoundFileForm({ existing, volume, canPlay, onAddFile, onSaved, onChooseWhereItPlays }: {
  existing: Record<string, unknown>
  volume: number
  canPlay: boolean
  onAddFile: (name: string, file: Blob, meta: CustomFileMeta) => Promise<string[] | null>
  onSaved: (name: string) => void
  onChooseWhereItPlays: () => void
}) {
  const [name, setName] = useState('')
  const [picked, setPicked] = useState<{ file: File; meta: CustomFileMeta } | null>(null)
  /** Why the picked file cannot be used. Shown under the picker it is about,
   *  and the only thing that marks the picker invalid. */
  const [pickProblem, setPickProblem] = useState<string | null>(null)
  /** Problems with the sound as a whole (the limit), not with the file. */
  const [hints, setHints] = useState<string[]>([])
  const [nameHints, setNameHints] = useState<string[]>([])
  const [checking, setChecking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const [saved, setSaved] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  /** Bumped per pick, so a slow check of an earlier file cannot win. */
  const pickSeq = useRef(0)
  const nameId = useId()
  const pickHintId = useId()
  const limitsId = useId()
  const hintsId = useId()
  const nameHintId = useId()

  const show = (problems: string[]) => {
    setHints(problems.filter(p => !NAME_CODES.has(p)).map(problemText))
    setNameHints(problems.filter(p => NAME_CODES.has(p)).map(problemText))
  }

  // Once the saved sound is deleted from the list above, "Saved <name>." is
  // over: forget it, so an Undo of that delete does not bring the notice back.
  const savedGone = saved !== null && !Object.hasOwn(existing, saved)
  useEffect(() => { if (savedGone) setSaved(null) }, [savedGone])

  const pick = async (file: File | undefined) => {
    const seq = ++pickSeq.current
    setPicked(null)
    setPickProblem(null)
    setSaved(null)
    setFailed(false)
    setHints([])
    if (!file) { setChecking(false); return }
    setChecking(true)
    const result = await readAudioFile(file)
    if (seq !== pickSeq.current) return
    setChecking(false)
    if ('problem' in result) {
      setPickProblem(problemText(result.problem))
      return
    }
    setPicked({ file, meta: result })
    // The file's own name is a sensible default the user can change. Read the
    // name as it is NOW: the user may have typed one while the file was checked.
    const derived = file.name.replace(/\.[^.]*$/, '').replace(/[^\p{L}\p{N} _-]/gu, ' ').trim()
      .slice(0, CUSTOM_TONE_LIMITS.maxNameLength).trim()
    setName(cur => (cur.trim() === '' ? derived : cur))
  }

  const preview = () => {
    if (!picked || typeof URL.createObjectURL !== 'function') return
    const url = URL.createObjectURL(picked.file)
    playSoundFile(url, volume, () => URL.revokeObjectURL(url))
  }

  const add = async () => {
    // Save is off until a file has passed its checks, so this only runs with one.
    if (!picked) return
    const trimmed = name.trim()
    setSaved(null)
    setFailed(false)
    const problems = validateCustomFile(trimmed, picked.meta, existing)
    if (problems.length > 0) { show(problems); return }
    setBusy(true)
    const result = await onAddFile(trimmed, picked.file, picked.meta)
    setBusy(false)
    if (result === null) { setFailed(true); return }
    if (result.length > 0) { show(result); return }
    show([])
    setName('')
    setPicked(null)
    if (fileInput.current) fileInput.current.value = ''
    setSaved(trimmed)
    onSaved(trimmed)
  }

  return (
    <TitledCard index={1} title={i18nT('pages.settings.notificationsPanel.custom_sound_file_title')}>
      <div id={limitsId} className="text-[12px] text-muted">
        {i18nT('pages.settings.notificationsPanel.custom_sound_file_hint', {
          maxKb: CUSTOM_FILE_LIMITS.maxBytes / 1024, maxSeconds: CUSTOM_FILE_LIMITS.maxSeconds,
        })}
      </div>
      <div className="flex flex-col gap-1.5 py-1.5">
        <label className="flex flex-col gap-1.5 text-[13px] font-semibold text-text">
          {i18nT('pages.settings.notificationsPanel.custom_sound_file_pick')}
          <Input
            ref={fileInput}
            type="file"
            accept="audio/*"
            aria-label={i18nT('pages.settings.notificationsPanel.custom_sound_file_pick')}
            aria-describedby={pickProblem ? `${limitsId} ${pickHintId}` : limitsId}
            aria-invalid={pickProblem ? true : undefined}
            disabled={busy}
            className="flex-none rounded-none border-0 bg-transparent p-0 text-[13px] font-normal text-muted file:mr-2 file:py-1 file:px-3 file:rounded-md file:border file:border-border file:bg-bg-elevated file:text-text file:text-sm file:cursor-pointer"
            onChange={e => { void pick(e.target.files?.[0]) }}
          />
        </label>
        {/* A refused file is a failure of THIS control: said right under it,
            in the danger colour, and kept until another file is picked. */}
        <div id={pickHintId} aria-live="polite" className="text-[12px] text-danger-text">
          {pickProblem}
        </div>
      </div>
      <div className="flex flex-col gap-1.5 py-1.5">
        <label htmlFor={nameId} className="text-[13px] font-semibold text-text">{i18nT('pages.settings.notificationsPanel.custom_sound_name')}</label>
        <Input
          id={nameId}
          value={name}
          maxLength={CUSTOM_TONE_LIMITS.maxNameLength}
          className={nameHints.length > 0 ? 'border-danger' : undefined}
          aria-invalid={nameHints.length > 0 || undefined}
          aria-describedby={nameHints.length > 0 ? nameHintId : undefined}
          // Off while saving, as the picker is: the save uses the name as it
          // was clicked, and clears the field when it finishes.
          disabled={busy}
          onChange={e => { setName(e.target.value); setNameHints([]); setHints([]); setSaved(null); setFailed(false) }}
        />
        <ul id={nameHintId} aria-live="polite" className="text-[12px] text-text">
          {nameHints.map(h => <li key={h}>{h}</li>)}
        </ul>
      </div>
      <ul id={hintsId} aria-live="polite" className="text-[12px] text-text">
        {hints.map(h => <li key={h}>{h}</li>)}
      </ul>
      {failed && (
        /* No hand-off: the picked file and the name typed above are an unsaved draft. */
        <ErrorNotice message={i18nT(FAILURE_KEY.add)} />
      )}
      <div className="flex gap-2 py-1.5">
        <Btn type="button" onClick={preview} disabled={!canPlay || !picked}>
          <Play size={14} /> {i18nT('pages.settings.notificationsPanel.custom_sound_preview')}
        </Btn>
        {/* Off until a file has passed its checks: with no usable file there is
            nothing to save, and the reason is already shown under the picker. */}
        <Btn type="button" primary onClick={() => { void add() }} disabled={busy || checking || !picked}>
          {i18nT('pages.settings.notificationsPanel.custom_sound_add')}
        </Btn>
      </div>
      {/* Below the buttons it follows from, so the greyed-out Save sound above
          does not read as a save that failed. Only while that sound still
          exists: deleting it must not leave "Saved <name>." claiming otherwise. */}
      {saved && !savedGone && <SavedNotice name={saved} onChooseWhereItPlays={onChooseWhereItPlays} />}
    </TitledCard>
  )
}
