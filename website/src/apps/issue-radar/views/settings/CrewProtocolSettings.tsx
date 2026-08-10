/**
 * CrewProtocolSettings — the repo-wide rules every crew in one repository
 * negotiates by: how long a claim lives, the label a crew applies when a call is
 * a human's to make, and the trailer each crew signs its commits with.
 *
 * ## Why this is a settings page and not part of a crew
 *
 * These three values are PER-REPO, not per-crew: the API behind them is
 * `GET`/`PUT /crews/settings?owner&repo`, and two crews in one repo negotiating
 * with different TTLs is exactly how a short-TTL crew steals a long-TTL crew's
 * live work. The needs-human label is the same kind of value — a crew that hits a
 * decision it cannot make comments on the issue and applies that label, and two
 * crews applying two different labels would leave a repo with no single thing to
 * filter on. So they belong beside the repo's other preferences, where a value
 * that governs the whole repository is expected to live.
 *
 * ## One key per write
 *
 * Every commit sends a ONE-KEY merge patch, never the whole document.
 * `putCrewSettings` merges server-side, so a one-key patch needs no revision
 * guard and two tabs editing different fields cannot erase each other. That is
 * what makes this page safe to leave open next to another surface reading the
 * same document.
 *
 * ## A rejected value keeps its text, and says why
 *
 * `crew_store.write_settings` accepts the numeric field only when the value is
 * `> 0`, and a trailer or a label only when it is non-blank; anything else it
 * drops on the floor. Dropping the draft to match would snap the field back to
 * the SAVED value with no message — which reads exactly like a successful save of
 * a value the store never took. So an invalid commit keeps the user's text on
 * screen and puts the constraint under the field, and the write is simply not
 * sent.
 *
 * ## A draft outlives its submit, and is released by the SERVER's answer
 *
 * The draft is the only copy of what the user typed, so it is held until the
 * write actually lands. A write that fails leaves the text in the field with the
 * failure beside it, which is the difference between correcting one character and
 * retyping a value from memory — and the values here govern the whole repository.
 * A commit that changes nothing has no write to wait for, so it releases its
 * draft at once rather than leaving the field looking edited forever.
 *
 * The section chrome (icon, heading, description, footnote) is supplied by
 * `RepoSettings`, so this renders the FIELDS only and inherits that page's look
 * instead of re-deriving it.
 */
import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { Input } from '../../../../components/ui'
import {
  issueRadarApi,
  type CrewSettings, type CrewSettingsPatch, type CrewSettingsResponse, type RepoRef,
} from '../../api'
import { repoScopeKey } from '../../lib/links'

/** The free-text fields. Both commit the same way — trim, refuse a blank, send
 *  one key — so they share one renderer and one branch in `commit`. */
type TextField = 'needs_human_label' | 'commit_trailer'

/** Which settings field a draft edit belongs to. */
type SettingsField = 'claim_ttl_hours' | TextField

/** The store's floor for the numeric field.
 *
 * `crew_store.write_settings` takes a value only `if val > 0` and then stores
 * `int(val)`, so the smallest number that survives a round trip is 1 — one hour
 * of claim TTL. It is also the `min` attribute on the input, so the spinner and
 * the validation message cannot disagree. */
const MIN_UNITS = 1
/** Mirrors the store's ``MAX_SETTING_TEXT``: past it the backend keeps the
 *  default and answers 200, so the client must refuse rather than let a
 *  successful-looking write discard what the user typed. */
const MAX_TEXT = 200

export default function CrewProtocolSettings({
  repoRef, settings,
}: {
  repoRef: RepoRef
  /** The repo's saved protocol settings, or `undefined` while they load — every
   *  field is disabled until they arrive, because a commit needs the old value to
   *  tell a real edit from a no-op. */
  settings: CrewSettings | undefined
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const scope = repoScopeKey(repoRef)
  // An OVERLAY of uncommitted edits, not a copy of the record: the displayed
  // value falls back to the server's whenever no edit is in flight, so a poll
  // that lands while a field is untouched is picked up without an effect and
  // without clobbering what is being typed in a different field.
  const [drafts, setDrafts] = useState<Partial<Record<SettingsField, string>>>({})
  /** Per-field validation message, set when a commit was REFUSED locally. Keyed by
   *  field rather than held as one string: the three fields commit independently,
   *  so a rejected trailer must not blank the message under the claim TTL. */
  const [invalid, setInvalid] = useState<Partial<Record<SettingsField, string>>>({})
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  /** The draft each field currently has a write OUTSTANDING for.
   *
   *  A ref rather than state: nothing renders from it, and a commit has to read
   *  it in the same tick the blur fires. It exists because a draft now outlives
   *  its submit — `Enter` followed by a blur would otherwise send the same
   *  one-key patch twice. Emptied when the write settles either way, so a failed
   *  write can be retried by blurring the field again. */
  const inFlight = useRef<Partial<Record<SettingsField, string>>>({})

  /** Release the draft a landed write carried.
   *
   * Keyed off the PATCH rather than a per-call `mutate` callback: React Query
   * keeps one callback slot per observer, so a second field committed before the
   * first write settles would drop the first field's callback — and that is one
   * Tab key away on a three-field form. A patch is one key, and that key names
   * the field.
   *
   * A field typed into again while its write was in flight keeps the NEWER text:
   * that text is the only copy of that edit, for the same reason the first one
   * was held. */
  const release = (patch: CrewSettingsPatch) => {
    const field = (Object.keys(patch) as SettingsField[])[0]
    if (!field) return
    const sent = inFlight.current[field]
    delete inFlight.current[field]
    if (sent === undefined) return
    setDrafts((d) => {
      if (d[field] !== sent) return d
      const next = { ...d }
      delete next[field]
      return next
    })
  }

  /** Let a field be committed again after its write FAILED. The text is still on
   *  screen, so blurring the field once more is the retry. */
  const forget = (patch: CrewSettingsPatch) => {
    const field = (Object.keys(patch) as SettingsField[])[0]
    if (field) delete inFlight.current[field]
  }

  const save = useMutation({
    mutationFn: (patch: CrewSettingsPatch) => issueRadarApi.putCrewSettings(repoRef, patch),
    onMutate: () => { setError(''); setSaved(false) },
    onSuccess: (res, patch) => {
      // The record first, then the draft it replaces, so the field falls back to
      // the value the write returned and never shows the pre-edit one in between.
      //
      // Only the field THIS write owns, never the whole response. Writes are one
      // per field, not one overall, so two fields can be in flight at once — and
      // each response carries the entire document as the server saw it when that
      // write landed. Storing `res` wholesale therefore lets a response that
      // arrives late overwrite a SIBLING field with its pre-edit value: save the
      // trailer then the label, have the trailer's reply land second, and the
      // label snaps back on screen having been saved correctly. Merging one key
      // keeps every field's newest answer, whatever order the replies arrive in.
      const field = (Object.keys(patch) as SettingsField[])[0]
      queryClient.setQueryData(
        ['issue-radar', 'crew-settings', scope],
        (prev: CrewSettingsResponse | undefined) => {
          if (!prev || !field) return res
          const canonical = res.settings?.[field]
          // No canonical value for this key means the server refused it (an
          // over-long label reads as "not configured"): keep what is cached
          // rather than writing `undefined` into the record.
          if (canonical === undefined) return prev
          return { ...prev, settings: { ...prev.settings, [field]: canonical } }
        },
      )
      release(patch)
      setSaved(true)
      void queryClient.invalidateQueries({ queryKey: ['issue-radar', 'crews', scope] })
    },
    onError: (e: unknown, patch: CrewSettingsPatch) => {
      forget(patch)
      setError(e instanceof Error ? e.message : String(e))
    },
  })

  const shown = (field: SettingsField): string => {
    const draft = drafts[field]
    if (draft !== undefined) return draft
    if (!settings) return ''
    return String(settings[field])
  }

  const edit = (field: SettingsField, value: string) => {
    setDrafts((d) => ({ ...d, [field]: value }))
    // Typing is the user answering the message, so it goes away on the first
    // keystroke rather than sitting there until the next blur.
    setInvalid((v) => {
      if (v[field] === undefined) return v
      const next = { ...v }
      delete next[field]
      return next
    })
  }

  /** Drop the field's draft so the displayed value falls back to the server's. */
  const clearDraft = (field: SettingsField) =>
    setDrafts((d) => {
      const next = { ...d }
      delete next[field]
      return next
    })

  /** Send one field's committed value, and hold its draft until the write LANDS.
   *
   * Releasing it on submit is what lost the user's text: the draft is the only
   * copy of what was typed, so letting go before the server answers falls the
   * field back to the old saved value on a failure, with nothing left to recover
   * from — and this card edits repo-wide rules, so that is a claim TTL or a
   * commit trailer silently reverting under the person who changed it.
   *
   * `sent` is the draft as typed, not the trimmed value, because it is what the
   * field is displaying and therefore what `release` has to match. */
  const send = (field: SettingsField, sent: string, patch: CrewSettingsPatch) => {
    // ONE outstanding write per field, and the test is presence — not whether the
    // text matches. Comparing to `sent` only skipped an identical re-send: a NEWER
    // value overwrote the recorded draft and fired a second write, so when the
    // FIRST one landed `release` matched the newer text and deleted that draft.
    // The newer edit was the only copy of itself, which is the exact loss the
    // hold-until-landed design exists to prevent.
    //
    // The newer text is not lost by refusing here: it stays on screen as the
    // draft (and `release` cannot delete it, because it only clears a draft equal
    // to the value that was actually sent). Committing it again once the write
    // settles is the path, the same retry `forget` documents for a failed write.
    if (inFlight.current[field] !== undefined) return
    inFlight.current[field] = sent
    save.mutate(patch)
  }

  /** Commit ONE field as a one-key merge patch.
   *
   * One key, never the whole document: `putCrewSettings` merges server-side, so
   * sending only what changed means two tabs editing different fields cannot
   * erase each other. A no-op edit sends nothing at all. The patch is built from
   * a literal key per branch rather than a computed one, so its type is checked
   * against `CrewSettingsPatch` instead of asserted onto it.
   *
   * A value the store would refuse is refused HERE instead, and the draft is
   * kept: reverting it silently is indistinguishable from a save that worked.
   *
   * A NO-OP commit is the one case that drops the draft immediately: there is no
   * write to wait for, and a draft equal to the saved value would leave the
   * field looking edited for the rest of the session. */
  const commit = (field: SettingsField) => {
    const raw = drafts[field]
    if (raw === undefined || !settings) return
    const reject = (message: string) => setInvalid((v) => ({ ...v, [field]: message }))
    if (field !== 'claim_ttl_hours') {
      const text = raw.trim()
      if (!text) {
        reject(
          field === 'commit_trailer'
            ? t('apps.issueRadar.views.crews.desk.trailer_required')
            : t('apps.issueRadar.views.crews.desk.needs_human_required'),
        )
        return
      }
      if (text === settings[field]) {
        clearDraft(field)
        return
      }
      // The backend reads anything longer than MAX_TEXT as "not configured" and
      // keeps the default, answering 200 with the setting UNCHANGED. Without this
      // guard that success releases the draft, so an over-long label silently
      // reverts and the form looks like it saved. Rejected here so the user is
      // told, rather than discovering it on the forge as a queue nobody watches.
      if (text.length > MAX_TEXT) {
        reject(t('apps.issueRadar.views.crews.desk.text_too_long', { max: MAX_TEXT }))
        return
      }
      send(
        field, raw,
        field === 'commit_trailer' ? { commit_trailer: text } : { needs_human_label: text },
      )
      return
    }
    // Number(), not parseInt(): parseInt stops at the first non-digit, so the
    // scientific notation an <input type="number"> accepts and returns verbatim
    // ("1e2") parses to 1 — a hundred-hour TTL silently stored as one. Number()
    // reads it as 100, and the integer check rejects the fractions it also now
    // admits ("1.5") instead of truncating them behind the user's back.
    const n = Number(raw)
    if (!Number.isInteger(n) || n < MIN_UNITS) {
      reject(t('apps.issueRadar.views.crews.desk.claim_ttl_min'))
      return
    }
    if (n === settings.claim_ttl_hours) {
      clearDraft(field)
      return
    }
    send(field, raw, { claim_ttl_hours: n })
  }

  /** Hint, or the validation message in its place.
   *
   * The message REPLACES the hint rather than stacking under it: the hint says
   * what the field is for, which is not what the user needs while the value is
   * being refused, and two lines under one control read as two unrelated facts. */
  const footnote = (field: SettingsField, errorId: string, testId: string, hint: string) => {
    const message = invalid[field]
    return message ? (
      <p id={errorId} role="alert" data-testid={`${testId}-error`} className="m-0 text-[13px] font-medium text-danger">
        {message}
      </p>
    ) : (
      <div className="text-[13px] text-muted">{hint}</div>
    )
  }

  const numericField = (field: 'claim_ttl_hours', label: string, hint: string, unit: string, testId: string) => {
    const message = invalid[field]
    const errorId = `crew-desk-${field}-error`
    return (
      <div className="flex flex-col gap-1.5">
        <label className="text-[13px] font-semibold text-text-strong" htmlFor={`crew-desk-${field}`}>{label}</label>
        <div className="flex items-center gap-2">
          <Input
            id={`crew-desk-${field}`}
            type="number"
            min={MIN_UNITS}
            value={shown(field)}
            aria-invalid={message ? true : undefined}
            aria-describedby={message ? errorId : undefined}
            onChange={(e) => edit(field, e.target.value)}
            onBlur={() => commit(field)}
            onKeyDown={(e) => { if (e.key === 'Enter') commit(field) }}
            disabled={!settings}
            className="max-w-[140px] flex-none"
            data-testid={testId}
          />
          <span className="text-[13px] text-muted">{unit}</span>
        </div>
        {footnote(field, errorId, testId, hint)}
      </div>
    )
  }

  /** A free-text field. `testId` doubles as the DOM id, so the label's `htmlFor`,
   *  the `aria-describedby` target and the harness all address one string.
   *
   *  `mono` is OPT-IN and only for a value that is verbatim code: the app font is
   *  user-configurable through `--font-body`, while Tailwind's `font-mono` reads
   *  `--mono`, so a field that hardcodes it ignores the user's choice. */
  const textField = (
    field: TextField,
    label: string,
    hint: string,
    testId: string,
    mono?: boolean,
  ) => {
    const message = invalid[field]
    const errorId = `${testId}-error`
    return (
      <div className="flex flex-col gap-1.5">
        <label className="text-[13px] font-semibold text-text-strong" htmlFor={testId}>{label}</label>
        <Input
          id={testId}
          value={shown(field)}
          aria-invalid={message ? true : undefined}
          aria-describedby={message ? errorId : undefined}
          onChange={(e) => edit(field, e.target.value)}
          onBlur={() => commit(field)}
          onKeyDown={(e) => { if (e.key === 'Enter') commit(field) }}
          disabled={!settings}
          className={`w-full${mono ? ' font-mono' : ''}`}
          data-testid={testId}
        />
        {footnote(field, errorId, testId, hint)}
      </div>
    )
  }

  const ttl = settings?.claim_ttl_hours ?? 0

  // The `crew-desk-*` element ids and test ids are kept verbatim from where this
  // block used to render, so a harness or test that already addresses a field
  // keeps addressing the same one across the move.
  return (
    <div data-testid="crew-desk-protocol">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4">
        {numericField(
          'claim_ttl_hours',
          t('apps.issueRadar.views.crews.desk.claim_ttl_label'),
          t('apps.issueRadar.views.crews.desk.claim_ttl_hint'),
          t('apps.issueRadar.views.crews.desk.unit_hours', { count: ttl }),
          'crew-desk-claim-ttl',
        )}
        {textField(
          'needs_human_label',
          t('apps.issueRadar.views.crews.desk.needs_human_label'),
          t('apps.issueRadar.views.crews.desk.needs_human_hint'),
          'crew-desk-needs-human',
        )}
      </div>
      <div className="mt-4">
        {/* Mono on purpose, and the one field here that keeps it: the value is a
            git trailer template written verbatim into commit messages, so it is
            code, not prose. */}
        {textField(
          'commit_trailer',
          t('apps.issueRadar.views.crews.desk.trailer_label'),
          t('apps.issueRadar.views.crews.desk.trailer_hint'),
          'crew-desk-commit-trailer',
          true,
        )}
      </div>
      {/* Updates in place after a save, so it must announce itself. */}
      <div
        aria-live="polite"
        className="mt-3 text-[13px] min-h-[1.25rem]"
        data-testid="crew-desk-protocol-status"
        data-state={save.isPending ? 'saving' : error ? 'failed' : saved ? 'saved' : 'idle'}
      >
        {save.isPending && <span className="text-muted">{t('apps.issueRadar.views.crews.desk.settings_saving')}</span>}
        {!save.isPending && error && <span className="text-danger">{t('apps.issueRadar.views.crews.desk.settings_failed', { error })}</span>}
        {!save.isPending && !error && saved && <span className="text-ok">{t('apps.issueRadar.views.crews.desk.settings_saved')}</span>}
      </div>
    </div>
  )
}
