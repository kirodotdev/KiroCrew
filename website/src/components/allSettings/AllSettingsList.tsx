/**
 * Settings -> Advanced: every config key, with a control for the ones that have
 * none elsewhere.
 *
 * The rows come from `GET /api/config/schema`: the gateway marks each key it
 * lists with an `allSettings` record (`config_all_settings.py`), so this
 * file holds no list of keys. A key that has its own Settings control links to
 * it instead of offering a second one. A key the gateway marks read-only shows
 * its value only. Every write goes through `PATCH /api/config/kirocrew`, the
 * route every Settings control saves through.
 *
 * Lives under `components/` with bare controls on purpose: its rows are drawn
 * from data, so the settings-search extractor could not index them anyway, and
 * the page's own search entry (`settingsManual.ts`) is what search finds.
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, RotateCcw } from 'lucide-react'
import { api } from '../../api/client'
import { Badge, Btn, Input, SearchInput, Toggle } from '../ui'
import ErrorNotice from '../ErrorNotice'
import SimpleSelect from '../SimpleSelect'
import { SettingsLink } from '../SettingsLink'
import { useConfigSchemaQuery } from '../settingRef/useConfigSchema'
import type { AllSettingsRecord, SchemaEntry } from '../settingRef/resolveSettingRef'
import { SETTINGS_REGISTRY } from '../commandPalette/settingsRegistry.gen'
import { settingsTabLabel } from '../commandPalette/settingsTabLabel'
import { i18nT } from '../../i18n/t'

type Row = SchemaEntry & { allSettings: AllSettingsRecord }

/** The `previous` field of a generic PATCH reply: what `config.json` held at the path. */
type StoredLeaf = { stored: false } | { stored: true; value: unknown }

const REGISTRY_BY_ID = new Map(SETTINGS_REGISTRY.map(e => [e.id, e]))

/** The value at dotted *path* in the masked config view, or `undefined`. */
export function valueAt(config: unknown, path: string): unknown {
  let node: unknown = config
  for (const part of path.split('.')) {
    if (node === null || typeof node !== 'object') return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

function shown(value: unknown): string {
  if (value === undefined || value === null || value === '') return i18nT('settings.advanced.empty_value')
  if (value === true) return i18nT('settings.advanced.value_on')
  if (value === false) return i18nT('settings.advanced.value_off')
  if (Array.isArray(value)) return value.length ? value.join(', ') : i18nT('settings.advanced.empty_value')
  return String(value)
}

function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null)
}

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** A text or number field that saves on Enter or when it loses focus. */
function DraftInput({ value, numeric, label, onSave, disabled, invalid }: { value: unknown; numeric: boolean; label: string; onSave: (v: unknown) => void; disabled: boolean; invalid: boolean }) {
  const initial = value === undefined || value === null ? '' : String(value)
  const [draft, setDraft] = useState(initial)
  const [seen, setSeen] = useState(initial)
  if (seen !== initial) { setSeen(initial); setDraft(initial) }
  const commit = () => {
    if (draft === initial) return
    if (!numeric) { onSave(draft); return }
    const n = Number(draft)
    // An unparsable number is sent as typed: the route names what is wrong.
    onSave(draft.trim() !== '' && Number.isFinite(n) ? n : draft)
  }
  return (
    <Input
      aria-label={label}
      className={`w-full sm:w-48 font-mono ${invalid ? 'border-danger' : ''}`}
      aria-invalid={invalid || undefined}
      inputMode={numeric ? 'decimal' : undefined}
      value={draft}
      disabled={disabled}
      onChange={e => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={e => { if (e.key === 'Enter') commit() }}
    />
  )
}

/** One item per line; saves when it loses focus, like every other field here. */
function ListEditor({ value, label, onSave, disabled, invalid }: { value: unknown; label: string; onSave: (v: string[]) => void; disabled: boolean; invalid: boolean }) {
  const initial = Array.isArray(value) ? value.join('\n') : ''
  const [draft, setDraft] = useState(initial)
  const [seen, setSeen] = useState(initial)
  if (seen !== initial) { setSeen(initial); setDraft(initial) }
  const commit = () => {
    const items = draft.split('\n').map(s => s.trim()).filter(Boolean)
    if (items.join('\n') !== initial) onSave(items)
  }
  return (
    <div className="flex flex-col gap-1.5 w-full sm:w-64">
      <textarea
        aria-label={label}
        aria-invalid={invalid || undefined}
        rows={3}
        className={`w-full bg-bg-elevated border rounded-md px-2 py-1 text-[13px] font-mono text-text focus-visible:border-accent focus:outline-hidden ${invalid ? 'border-danger' : 'border-border'}`}
        value={draft}
        disabled={disabled}
        onChange={e => setDraft(e.target.value)}
        onBlur={commit}
      />
      <span className="text-[12px] text-muted">{i18nT('settings.advanced.list_hint')}</span>
    </div>
  )
}

/** A link to the Settings control that owns the key: an exact registry row, or
 *  the page that writes the key's section. One look for both. */
function OwnControlLink({ rec }: { rec: AllSettingsRecord }) {
  const entry = rec.settingId ? REGISTRY_BY_ID.get(rec.settingId) : undefined
  const target = entry
    ? { tab: entry.tab, params: entry.params, highlight: entry.id }
    : rec.page
      ? { tab: rec.page.tab, sub: rec.page.sub, highlight: rec.page.highlight }
      : null
  if (!target) return null
  return (
    <SettingsLink {...target} className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline">
      {i18nT('settings.advanced.open_setting', { page: settingsTabLabel(target.tab) })}
      <ArrowRight className="lucide-inline" />
    </SettingsLink>
  )
}

/** Why a read-only row cannot be changed here, in plain words. One static key
 *  per reason, so the catalog gate can see each one. */
function readOnlyReason(reason: AllSettingsRecord['reason']): string {
  switch (reason) {
    case 'safety': return i18nT('settings.advanced.why_safety')
    case 'sign_in': return i18nT('settings.advanced.why_sign_in')
    case 'reach': return i18nT('settings.advanced.why_reach')
    case 'share': return i18nT('settings.advanced.why_share')
    case 'owned': return i18nT('settings.advanced.why_owned')
    case 'marker': return i18nT('settings.advanced.why_marker')
    case 'alias': return i18nT('settings.advanced.why_alias')
    case 'locked': return i18nT('settings.advanced.why_locked')
    case 'compound': return i18nT('settings.advanced.why_compound')
    default: return i18nT('settings.advanced.why_text')
  }
}

function SettingRow({ row, value }: { row: Row; value: unknown }) {
  const qc = useQueryClient()
  // Remounting a draft field shows the saved value again (after a refused save,
  // and after every successful one).
  const [draftKey, setDraftKey] = useState(0)
  // What a successful save (a reset too) replaced, so the row can offer to put
  // it back; cleared once that undo saves. The stored state comes from the PATCH
  // reply (`previous`, read under the write lock): the config view fills
  // defaults in, so it cannot tell an absent key from one stored at its default.
  const [undo, setUndo] = useState<{ previous: StoredLeaf; shownBefore: unknown } | null>(null)
  const save = useMutation({
    mutationFn: (args: { value?: unknown; unset?: boolean; before: unknown; isUndo?: boolean }) =>
      api.patchConfig(row.path, args.value, args.unset ? { unset: true } : undefined) as Promise<{ previous?: StoredLeaf } | undefined>,
    // After every save, the field shows what the gateway now holds: the loader may
    // clamp a value, so a save can leave the stored value where it was.
    onSuccess: async (data, args) => {
      await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      setDraftKey(k => k + 1)
      const previous = data?.previous
      // Nothing to put back: an undo, an older gateway, or a reset of a key that was not stored.
      const nothing = args.isUndo || !previous || (args.unset && !previous.stored)
      setUndo(nothing ? null : { previous, shownBefore: args.before })
    },
    onError: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })
  const rec = row.allSettings
  const label = row.label || row.path
  // The gateway decides which keys have a control of their own (registry
  // configKey, a dashboard route, or the page that writes the section).
  const linked = rec.mode === 'control'
  const editable = rec.mode === 'edit'
  const busy = save.isPending
  const send = (v: unknown) => save.mutate({ value: v, before: value })
  const putBack = () => { save.reset(); setDraftKey(k => k + 1) }
  // A key that was absent before the save goes back to absent, not to a copy of its default.
  const undoSave = () => {
    if (!undo) return
    save.mutate(undo.previous.stored
      ? { value: undo.previous.value, before: value, isUndo: true }
      : { unset: true, before: value, isUndo: true })
  }
  // Hidden when the save left the value where it was (the loader clamped it back).
  const canUndo = !save.isError && undo !== null && !sameValue(undo.shownBefore, value)
  let control: React.ReactNode = null
  if (linked) control = (
    <div className="flex flex-col gap-0.5 sm:items-end">
      <OwnControlLink rec={rec} />
      <code className="text-[12px] font-mono text-muted break-all">{i18nT('settings.advanced.current_label', { value: shown(value) })}</code>
    </div>
  )
  else if (editable && rec.control === 'bool') control = <Toggle checked={value === true} onChange={send} disabled={busy} label={label} />
  else if (editable && rec.control === 'enum') control = <SimpleSelect aria-label={label} options={(rec.values ?? []).map(String)} value={value == null ? '' : String(value)} onChange={send} triggerFallback={shown(value)} />
  else if (editable && (rec.control === 'int' || rec.control === 'float' || rec.control === 'str')) control = <DraftInput key={draftKey} value={value} numeric={rec.control !== 'str'} label={label} onSave={send} disabled={busy} invalid={save.isError} />
  else if (editable && rec.control === 'str_list') control = <ListEditor key={draftKey} value={value} label={label} onSave={send} disabled={busy} invalid={save.isError} />
  // A shown-only value is labelled, so it does not read as the default printed twice.
  else control = <code className="text-[13px] font-mono text-muted break-all">{i18nT('settings.advanced.current_label', { value: shown(value) })}</code>
  const changed = row.default !== undefined && !sameValue(value, row.default)
  const readOnly = rec.mode === 'readonly'
  const resettable = editable && rec.resettable === true
  return (
    <li data-setting-key={row.path} data-setting-label={label} className="flex flex-col gap-2 py-2.5 border-b border-border sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[13px] font-semibold text-text">{label}</span>
          {/* Restart only matters for a value this row can change; a linked key's
              own page says it there. */}
          {row.requiresRestart && editable && <Badge variant="warn">{i18nT('settings.advanced.restart')}</Badge>}
          {readOnly && <Badge variant="muted">{i18nT('settings.advanced.read_only')}</Badge>}
        </div>
        <code className="text-[12px] font-mono text-muted break-all">{row.path}</code>
        {readOnly && <p className="text-[12px] text-text mt-0.5">{readOnlyReason(rec.reason)}</p>}
        {row.help && <p className="text-[12px] text-muted mt-0.5">{row.help}</p>}
        <div className="text-[12px] text-muted mt-0.5">{i18nT('settings.advanced.default_label', { value: shown(row.default) })}</div>
        {/* No hand-off: a failed save leaves the typed draft in its field for a retry. */}
        <ErrorNotice title={i18nT('settings.advanced.not_saved')} message={save.error ? errorText(save.error) : null} variant="inline" messagePlacement="below" />
        {save.isError && (
          <Btn className="mt-1" onClick={putBack}>{i18nT('settings.advanced.put_back', { value: shown(value) })}</Btn>
        )}
        {canUndo && (
          <Btn className="mt-1" disabled={busy} onClick={undoSave}>{i18nT('settings.advanced.put_back', { value: shown(undo.shownBefore) })}</Btn>
        )}
      </div>
      <div className="flex flex-col gap-1.5 sm:items-end">
        <div className="flex items-center gap-2">{control}</div>
        {/* Shown before any change too, so the undo is visible on a first visit;
            disabled while the value is already the default. */}
        {editable && (
          <div className="flex items-center gap-2">
            {!save.isError && <span className="text-[12px] text-muted">{i18nT('settings.advanced.saves_now')}</span>}
            {resettable && (
              <Btn aria-label={i18nT('settings.advanced.reset_aria', { key: row.path })} disabled={busy || !changed} onClick={() => save.mutate({ unset: true, before: value })}>
                <RotateCcw className="lucide-inline" /> {i18nT('settings.advanced.reset')}
              </Btn>
            )}
          </div>
        )}
      </div>
    </li>
  )
}

export function AllSettingsList() {
  const { schema, error: schemaError } = useConfigSchemaQuery()
  const config = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const [query, setQuery] = useState('')
  const rows = useMemo(() => {
    const out: Row[] = []
    for (const entry of schema?.values() ?? []) if (entry.allSettings) out.push(entry as Row)
    return out
  }, [schema])
  const needle = query.trim().toLowerCase()
  const visible = needle
    ? rows.filter(r => [r.path, r.label ?? '', r.help ?? ''].some(s => s.toLowerCase().includes(needle)))
    : rows
  const groups = new Map<string, Row[]>()
  for (const row of visible) {
    const section = row.path.includes('.') ? row.path.split('.')[0] : ''
    groups.set(section, [...(groups.get(section) ?? []), row])
  }
  const loadError = schemaError ?? (config.error as Error | null)
  return (
    <div className="flex flex-col gap-3">
      <p className="text-[13px] text-muted">{i18nT('settings.advanced.intro')}</p>
      <SearchInput aria-label={i18nT('settings.advanced.search_placeholder')} placeholder={i18nT('settings.advanced.search_placeholder')} value={query} onChange={e => setQuery(e.target.value)} />
      {loadError && <ErrorNotice title={i18nT('settings.advanced.load_error')} message={errorText(loadError)} askAgent />}
      {schema && config.data !== undefined && visible.length === 0 && <p className="text-[13px] text-muted">{i18nT('settings.advanced.empty')}</p>}
      {schema && config.data !== undefined && [...groups].map(([section, items]) => (
        <section key={section || '-'} aria-label={section || i18nT('settings.advanced.top_level')}>
          <h3 className="text-[12px] font-semibold uppercase tracking-wide text-muted mt-2">
            <code className="font-mono">{section || i18nT('settings.advanced.top_level')}</code>
          </h3>
          <ul>{items.map(row => <SettingRow key={row.path} row={row} value={valueAt(config.data, row.path)} />)}</ul>
        </section>
      ))}
    </div>
  )
}
