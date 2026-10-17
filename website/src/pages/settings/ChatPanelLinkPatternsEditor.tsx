/**
 * The `dashboard.link_patterns` rule editor Settings ▸ Chat ▸ Transcript mounts,
 * and the validation hint it (and the panel's compaction field) render.
 * `ChatPanel.tsx` re-exports `LinkPatternsEditor` and its types.
 */
import { useState, useRef, useEffect } from 'react'
import { SettingsField } from '../../components/settings'
import { Btn, Input } from '../../components/ui'
import { Plus, Trash2 } from 'lucide-react'
import { configPatternRefused, configUrlTemplateOk } from '../../utils/autolinkRules'
import { i18nT } from '../../i18n/t'

/** One `dashboard.link_patterns` rule as it travels the config wire; the
 * renderer-side validation lives in `utils/autolinkRules.setConfigAutolinkRules`. */
export interface LinkPatternRule {
  pattern: string
  url: string
}

function invalidRegex(pattern: string): boolean {
  if (!pattern.trim()) return false
  try {
    new RegExp(pattern)
    return false
  } catch {
    return true
  }
}

/** Row editor for `dashboard.link_patterns` (regex -> URL template rewrite
 * rules the transcript renderer applies at display time).
 *
 * Carries `label` itself and renders the SettingsField frame internally --
 * the composite-primitive contract (same as TagListEditor) that makes the
 * settings-registry extractor and deep-link highlighting see it as one row.
 *
 * Commit points are blur and remove/add — not keystrokes — so one edit is one
 * PUT. Only persistable rows (non-empty pattern, http(s) url) go on the wire;
 * a half-typed row stays local until it qualifies. An invalid regex still
 * SAVES (the renderer skips rules the browser rejects) so a typo cannot eat
 * the rule text; the row flags it instead. Exported for its regression tests. */
/**
 * Client-side validation hint for a rule-editor field. Deliberately NOT
 * `ErrorNotice`: nothing has failed — these describe input still being
 * typed, and the errors-use-error-notice rule forbids dressing validation
 * hints as errors (`role="alert"`, danger styling, agent hand-off).
 * `role="status"` announces politely, matching the AboutPanel status idiom.
 */
export function FieldHint({ message }: { message: string }) {
  if (!message) return null
  return <div className="text-[12px] text-warn mt-0.5" role="status">{message}</div>
}

export interface LinkPatternsDraft {
  rows: LinkPatternRule[]
  // Live refs, not snapshots: a save that settles after the editor unmounts
  // must still move the remounted editor's watermarks and in-flight count.
  adoptedKeyRef: { current: string }
  pendingKeyRef: { current: string | null }
  savesInFlightRef: { current: number }
  saveChainRef: { current: Promise<unknown> }
}

export function LinkPatternsEditor({ label, description, hint, configKey, rules, onSave, disabled, draft }: {
  label: string
  description?: string
  hint?: string
  configKey?: string
  rules: readonly LinkPatternRule[]
  /**
   * Persist the cleaned rules. Returning the save's settlement promise lets
   * the editor advance its adopted watermark once the write is CONFIRMED —
   * without it, a later external change back to the pre-save value would be
   * indistinguishable from a failed save's rollback and silently swallowed,
   * leaving stale rows to overwrite the external change on the next blur.
   */
  onSave: (next: LinkPatternRule[]) => void | Promise<unknown>
  disabled?: boolean
  /** Holder owned by the host that outlives this editor. A settings rail
   *  unmounts the editor on a page switch; without this, a half-typed row the
   *  commit gate refused to save would be gone on return. */
  draft?: { current: LinkPatternsDraft | null }
}) {
  const [rows, setRows] = useState<LinkPatternRule[]>(() => draft?.current?.rows ?? rules.map(r => ({ ...r })))
  // A commit swallowed by the half-edited gate, so the editor can say so at
  // the commit point instead of relying on the offending row's own hint
  // (which may be scrolled out of view when a DIFFERENT row was edited).
  const [saveBlocked, setSaveBlocked] = useState(false)
  const serverKey = JSON.stringify(rules)
  // Two watermarks decide whether a server-value movement may replace local
  // rows. `adopted` is the last server value local rows were built from —
  // re-captured at each save, because that displayed value is exactly where
  // a FAILED save's optimistic overlay rolls back to. `pending` is the value
  // a save submitted, i.e. our own edit echoing back (the optimistic mask,
  // then the confirmed write). Neither may overwrite rows: the rollback
  // arriving as a "change" is how a rejected save (e.g. 400 on a duplicate
  // pattern) would silently erase everything typed since the last save.
  // A restored draft keeps the baseline its rows were built from, so a server
  // change made while the editor was unmounted still merges through the
  // adopt effect below instead of being masked by the old rows.
  const [restored] = useState(() => draft?.current ?? null)
  const ownAdoptedKeyRef = useRef(serverKey)
  // Save serialization state: how many PUTs are unsettled, and the tail of
  // the chain a new save must launch behind while any are in flight.
  const ownSavesInFlightRef = useRef(0)
  const ownSaveChainRef = useRef<Promise<unknown>>(Promise.resolve())
  const ownPendingKeyRef = useRef<string | null>(null)
  const adoptedKeyRef = restored?.adoptedKeyRef ?? ownAdoptedKeyRef
  const savesInFlightRef = restored?.savesInFlightRef ?? ownSavesInFlightRef
  const saveChainRef = restored?.saveChainRef ?? ownSaveChainRef
  const pendingKeyRef = restored?.pendingKeyRef ?? ownPendingKeyRef
  const rowsRef = useRef(rows)
  rowsRef.current = rows
  useEffect(() => () => {
    if (draft) draft.current = { rows: rowsRef.current, adoptedKeyRef, pendingKeyRef, savesInFlightRef, saveChainRef }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- stash once, on unmount
  }, [])
  // Adopt an external change (another tab, `kirocrew config set`) whenever
  // the server VALUE moves somewhere new; identity-only refetches leave
  // local drafts alone because the key is the serialized value.
  useEffect(() => {
    if (serverKey === adoptedKeyRef.current) return // unmoved, or a failed save's rollback: rows win
    if (serverKey === pendingKeyRef.current) return // our own save echoing: rows already show it
    // Rows typed since the last sync are absent from the outgoing baseline;
    // carry them across the adopt instead of erasing them. A clean row the
    // external change deleted IS in the baseline, so it still goes. The
    // surviving draft rendered beside the adopted rules is the conflict
    // surface: the user sees both and the next blur commits the merge.
    const baseline: LinkPatternRule[] = JSON.parse(adoptedKeyRef.current)
    adoptedKeyRef.current = serverKey
    pendingKeyRef.current = null
    setRows(prev => {
      const same = (a: LinkPatternRule, b: LinkPatternRule) =>
        a.pattern === b.pattern && a.url.trim() === b.url.trim()
      const drafts = prev.filter(r =>
        (r.pattern.trim() !== '' || r.url.trim() !== '') &&
        !baseline.some(b => same(b, r)) &&
        !rules.some(s => same(s, r)))
      return [...rules.map(r => ({ ...r })), ...drafts]
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- serverKey IS the value identity of `rules`
  }, [serverKey])
  const persistable = (list: readonly LinkPatternRule[]) => list
    // Pattern text goes on the wire EXACTLY as typed — whitespace in a regex
    // is load-bearing, so trimming here would broaden what the operator wrote
    // (the server preserves it verbatim too and trims only URLs).
    .map(r => ({ pattern: r.pattern, url: r.url.trim() }))
    .filter(r => r.pattern.trim().length > 0 && configUrlTemplateOk(r.url))
  // Inline flags for the two mistakes that otherwise fail silently: a URL the
  // commit filter treats as half-edited (the row LOOKS accepted but is never
  // PUT and vanishes on the next server adopt), and a duplicate pattern (the
  // one editor-typable error that reaches the server's 400, which surfaces
  // only as the generic failed-to-save banner). The URL check is the
  // registry's own acceptance rule (`configUrlTemplateOk`), so userinfo and a
  // `{match}` in the authority — templates a bare scheme regex passes but
  // registration refuses — flag here instead of saving-then-never-linkifying.
  const urlIncomplete = (url: string) =>
    url.trim() !== '' && !configUrlTemplateOk(url.trim())
  const duplicatePattern = (list: readonly LinkPatternRule[], i: number) => {
    // Exact comparison, mirroring the server's dedup: patterns differing only
    // in edge whitespace are DIFFERENT regexes and both may be saved.
    const p = list[i].pattern
    return p.trim() !== '' && list.some((r, j) => j < i && r.pattern === p)
  }
  const commit = (next: readonly LinkPatternRule[]) => {
    // A half-edited row (some text, not yet persistable) blocks the whole
    // save: clearing a URL to retype it must not delete the stored rule on
    // blur. Deleting is the remove button's job, never a blur side effect.
    // "Complete" is the registry's own acceptance rule (`configUrlTemplateOk`)
    // so a template registration would refuse — userinfo, `{match}` in the
    // authority — is withheld here exactly like an empty half, not silently
    // filtered out of the PUT (which would delete the stored rule on blur).
    const halfEdited = next.some(r => {
      const pattern = r.pattern.trim()
      const url = r.url.trim()
      const complete = pattern.length > 0 && configUrlTemplateOk(url)
      const empty = pattern.length === 0 && url.length === 0
      return !complete && !empty
    })
    // Surface the block at the commit point, not only on the offending row:
    // the row a user just edited may be far from the half-filled one, and a
    // silently swallowed save reads as data loss.
    setSaveBlocked(halfEdited)
    if (halfEdited) return
    const cleaned = persistable(next)
    if (JSON.stringify(cleaned) !== JSON.stringify(persistable(rules))) {
      // The currently-displayed server value is where the optimistic overlay
      // rolls back to if this save is rejected; the submitted value is what
      // echoes back if it lands. Mark both so the adopt effect can tell a
      // rollback and our own echo apart from a genuine external change.
      const submittedKey = JSON.stringify(cleaned)
      adoptedKeyRef.current = serverKey
      pendingKeyRef.current = submittedKey
      // Serialize whole-list PUTs: two in-flight saves (edit-blur racing a
      // remove) can settle out of order server-side, and last-writer-wins
      // would resurrect the removed rule. A save launched while another is
      // in flight defers behind it (failure included — both chain arms keep
      // the tail alive); an idle chain launches inline, so single saves keep
      // their synchronous shape. The pending/adopted marks above are
      // per-launch and already guard a superseded save's handlers.
      const settled = savesInFlightRef.current > 0
        ? saveChainRef.current.then(() => onSave(cleaned))
        : onSave(cleaned)
      // The echo alone cannot advance the adopted watermark: the optimistic
      // mask echoes the same value BEFORE the server accepts it, and doing so
      // there would make a failed save's rollback look external and erase the
      // typed rows. Only the settlement says which side of that fork we are
      // on. Both handlers are guarded on the pending mark so a superseded
      // save (edit + blur while the first save is in flight) cannot clobber
      // the newer save's marks.
      if (settled && typeof (settled as Promise<unknown>).then === 'function') {
        savesInFlightRef.current += 1
        const dec = () => {
          savesInFlightRef.current -= 1
        }
        saveChainRef.current = (settled as Promise<unknown>).then(dec, dec)
        ;(settled as Promise<unknown>).then(
          () => {
            // Confirmed: the submitted value is now the base rows are built
            // from, so a later external change back to the pre-save value is
            // adopted instead of being swallowed as a rollback.
            if (pendingKeyRef.current === submittedKey) {
              adoptedKeyRef.current = submittedKey
              pendingKeyRef.current = null
            }
          },
          () => {
            // Rejected: the server never took the value, so it can only
            // reappear as a genuine external change — drop the echo mark.
            // The rollback itself still matches the adopted (pre-save)
            // watermark, so the typed rows survive.
            if (pendingKeyRef.current === submittedKey) pendingKeyRef.current = null
          },
        )
      }
    }
  }
  const update = (i: number, field: 'pattern' | 'url', value: string) => {
    setRows(rs => rs.map((r, j) => (j === i ? { ...r, [field]: value } : r)))
  }
  const remove = (i: number) => {
    const next = rows.filter((_, j) => j !== i)
    setRows(next)
    commit(next)
  }
  return (
    <SettingsField label={label} description={description} hint={hint} configKey={configKey}>
      <div className="flex flex-col gap-1.5">
      {rows.map((row, i) => (
        // Narrow-first: fields stack below the `sm` breakpoint — side-by-side
        // at ~320px leaves each input ~100px, unusable for a regex. The URL
        // input and remove button share a nested row so delete stays reachable
        // without a third stacked line.
        <div key={i} className="flex flex-col sm:flex-row items-stretch sm:items-start gap-1.5">
          <div className="flex-1 min-w-0">
            <Input
              value={row.pattern}
              onChange={e => update(i, 'pattern', e.target.value)}
              onBlur={() => commit(rows)}
              placeholder={i18nT('pages.settings.chatPanel.link_patterns_pattern_ph')}
              aria-label={i18nT('pages.settings.chatPanel.link_patterns_pattern_aria')}
              disabled={disabled}
              className="w-full font-mono"
            />
            <FieldHint message={invalidRegex(row.pattern) ? i18nT('pages.settings.chatPanel.link_patterns_invalid_regex') : ''} />
            <FieldHint message={!invalidRegex(row.pattern) && row.pattern.trim() !== '' && configPatternRefused(row.pattern) ? i18nT('pages.settings.chatPanel.link_patterns_unsafe_pattern') : ''} />
            <FieldHint message={duplicatePattern(rows, i) ? i18nT('pages.settings.chatPanel.link_patterns_duplicate') : ''} />
            <FieldHint message={row.pattern.trim() === '' && row.url.trim() !== '' ? i18nT('pages.settings.chatPanel.link_patterns_row_incomplete') : ''} />
          </div>
          <div className="flex flex-1 min-w-0 items-start gap-1.5">
          <div className="flex-1 min-w-0">
          <Input
            value={row.url}
            onChange={e => update(i, 'url', e.target.value)}
            onBlur={() => commit(rows)}
            // The catalog value carries `{{placeholder}}` (well-formed i18next
            // interpolation, identical in every locale) and the literal
            // `{match}` token arrives through interpolation — the i18n identity
            // gate refuses raw single-brace tokens in catalog values, and the
            // added-lines gate refuses a hardcoded attribute string here.
            placeholder={i18nT('pages.settings.chatPanel.link_patterns_url_ph', { placeholder: '{match}' })}
            aria-label={i18nT('pages.settings.chatPanel.link_patterns_url_aria')}
            disabled={disabled}
            className="w-full"
          />
          <FieldHint message={urlIncomplete(row.url) ? i18nT('pages.settings.chatPanel.link_patterns_url_invalid', { placeholder: '{match}' }) : ''} />
          {/* A pattern with no URL yet blocks every commit from this editor
              (the half-edited guard in `commit`), and nothing else marks it:
              `urlIncomplete` only fires once the URL field holds text. */}
          <FieldHint message={row.pattern.trim() !== '' && row.url.trim() === '' ? i18nT('pages.settings.chatPanel.link_patterns_row_incomplete') : ''} />
          </div>
          <Btn
            onClick={() => remove(i)}
            aria-label={i18nT('pages.settings.chatPanel.link_patterns_remove')}
            disabled={disabled}
          ><Trash2 className="lucide-inline" /></Btn>
          </div>
        </div>
      ))}
      <div>
        <Btn
          onClick={() => setRows(rs => [...rs, { pattern: '', url: '' }])}
          disabled={disabled}
        ><Plus className="lucide-inline" /> {i18nT('pages.settings.chatPanel.link_patterns_add')}</Btn>
        <FieldHint message={saveBlocked ? i18nT('pages.settings.chatPanel.link_patterns_row_incomplete') : ''} />
      </div>
      </div>
    </SettingsField>
  )
}
