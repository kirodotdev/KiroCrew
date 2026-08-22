import React, { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trash2 } from 'lucide-react'

import { SettingsCard, SettingsSection } from '../../components/settings'
import { Btn, IconButton, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { api, ApiError, type VariablesView, type VariablesWrite } from '../../api/client'
import { useIsMobile } from '../../hooks/useIsMobile'
import { i18nT } from '../../i18n/t'

/* ── Validation, mirrored from the backend ──
 *
 * `src/kiro_crew/variables.py` is the authority: `validate_pair` refuses the same
 * four shapes with the same limits. It is duplicated here rather than round-tripped
 * because the round trip is the whole cost of a typo — the user learns that
 * `2FA_TOKEN` cannot be a name only after a save has failed, and a 400 naming the
 * key is a worse teacher than the field itself. The backend check is NOT removed by
 * this: a hand-edited `config.json` never passes through this file. */

/** `^[A-Za-z][A-Za-z0-9_]*$` — ASCII identifiers, so the same spelling is legal in a
 *  shell, a URL and a JSON key. Mirrors `variables.NAME_RE`. */
const NAME_RE = /^[A-Za-z][A-Za-z0-9_]*$/

/** Mirrors `variables.RESERVED_TOKENS` — prompt tokens the gateway resolves itself,
 *  so a user variable of the same name would be an inert shadow. Case-sensitive,
 *  exactly as the backend's frozenset membership test is. */
const RESERVED_NAMES = ['MAX_SUBAGENTS', 'VERBOSITY_BLOCK', 'WIDGET_BLOCK', 'STOP_FILE', 'ALIAS', 'bot_name']

/** Mirrors `variables.MAX_VALUE_LEN`. */
const MAX_VALUE_LEN = 4096

/** C0 controls and DEL, tab excluded. Mirrors `variables._FORBIDDEN_CHARS`: a value
 *  spanning lines could forge a context header in the assembled prompt, so newline
 *  is refused too. */
// eslint-disable-next-line no-control-regex
const FORBIDDEN_CHARS_RE = /[\u0000-\u0008\u000A-\u001F\u007F]/

/** Why a pair was refused. Codes rather than sentences so the check stays pure and
 *  the copy lives in the catalog. */
export type VariableError = 'name-pattern' | 'name-reserved' | 'name-duplicate' | 'value-length' | 'value-control'

/** First reason `name` cannot be used at a scope that already holds `existing`. */
export function nameError(name: string, existing: readonly string[] = []): VariableError | null {
  if (!NAME_RE.test(name)) return 'name-pattern'
  if (RESERVED_NAMES.includes(name)) return 'name-reserved'
  if (existing.includes(name)) return 'name-duplicate'
  return null
}

/** First reason `value` cannot be stored. */
export function valueError(value: string): VariableError | null {
  if (value.length > MAX_VALUE_LEN) return 'value-length'
  if (FORBIDDEN_CHARS_RE.test(value)) return 'value-control'
  return null
}

function errorText(code: VariableError): string {
  switch (code) {
    case 'name-pattern': return i18nT('pages.settings.variablesPanel.error_name_pattern')
    case 'name-reserved': return i18nT('pages.settings.variablesPanel.error_name_reserved')
    case 'name-duplicate': return i18nT('pages.settings.variablesPanel.error_name_duplicate')
    case 'value-length': return i18nT('pages.settings.variablesPanel.error_value_length', { max: MAX_VALUE_LEN })
    case 'value-control': return i18nT('pages.settings.variablesPanel.error_value_control')
  }
}

/* ── Scope vocabulary ── */

const SCOPES = ['global', 'workspace', 'crew', 'session'] as const
type Scope = typeof SCOPES[number]

/** Broad → narrow, matching `loader.resolve_variables`' merge order. A LOWER rank is
 *  broader, so a row is shadowed exactly when its own rank is below the winner's. */
const RANK: Record<string, number> = { global: 0, workspace: 1, crew: 2, session: 3 }

function scopeLabel(scope: string): string {
  switch (scope) {
    case 'global': return i18nT('pages.settings.variablesPanel.scope_global')
    case 'workspace': return i18nT('pages.settings.variablesPanel.scope_workspace')
    case 'crew': return i18nT('pages.settings.variablesPanel.scope_crew')
    case 'session': return i18nT('pages.settings.variablesPanel.scope_session')
    default: return scope
  }
}

/* ── Data ── */

/**
 * View state with a load flag, so "nothing defined" and "could not read" stay
 * different things on screen. Collapsing them would report a missing route as an
 * empty config, which is the one reading a user cannot act on.
 */
type PanelData = VariablesView & { unavailable: boolean }

const EMPTY: PanelData = { global: {}, workspaces: {}, effective: {}, winning_scope: {}, unavailable: true }

/**
 * Read the cascade, tolerating an absent client method.
 *
 * `api.variables?.()` and the `.catch()` are both load-bearing: dozens of test
 * files mock `../../api/client` PARTIALLY, so the method is `undefined` there, and
 * the route ships after this panel — an install running an older gateway answers
 * 404. Either way the panel renders its empty state instead of throwing.
 */
function fetchVariables(): Promise<PanelData> {
  try {
    return Promise.resolve(api.variables?.())
      .then(v => (v ? { ...EMPTY, ...v, unavailable: false } : EMPTY))
      .catch(() => EMPTY)
  } catch {
    return Promise.resolve(EMPTY)
  }
}

/* ── One scope's editable table ── */

interface TableProps {
  /** Which layer these pairs are STORED at — not necessarily the winning one. */
  scope: Scope
  pairs: Record<string, string>
  /** Winning scope per name, for the shadow indicators. */
  winning: Record<string, string>
  /** Names defined at every broader scope, so a row can say what it overrides. */
  broader: Record<string, Scope>
  /**
   * Apply a PER-KEY change. `set` writes named pairs, `delete` removes named keys;
   * anything unnamed is left alone server-side. Sending the whole map instead is
   * what previously let one tab's save discard another's unrelated edit.
   *
   * `onLanded` runs only after the write actually succeeds, so a form is cleared on
   * success rather than on dispatch — a refused save must leave the user's input
   * intact to correct.
   */
  onSave: (
    change: { set?: Record<string, string>; delete?: string[] },
    onLanded?: () => void,
  ) => void
  busy: boolean
  /** id of the plain-text warning, threaded to every input's `aria-describedby`. */
  noteId: string
}

function VariableTable({
  scope, pairs, winning, broader, onSave, busy, noteId,
}: TableProps) {
  const uid = React.useId()
  // Narrow branch driven by a width signal rather than by switching `display` on
  // the table: `display: block` on a <tr>/<td> drops the row/column association
  // that the scope="row" and scope="col" headers below exist to provide.
  const isMobile = useIsMobile()
  const nameHeadId = `${uid}-name`
  const valueHeadId = `${uid}-value`
  const [newName, setNewName] = useState('')
  const [newValue, setNewValue] = useState('')
  const [error, setError] = useState<VariableError | null>(null)
  // Value being typed, keyed by name. A row commits on blur or Enter, so a
  // half-typed value is never written and the input never fights the query.
  const [drafts, setDrafts] = useState<Record<string, string>>({})

  const names = Object.keys(pairs).sort()

  const commit = (name: string) => {
    const draft = drafts[name]
    if (draft === undefined || draft === pairs[name]) return
    const bad = valueError(draft)
    if (bad) { setError(bad); return }
    setError(null)
    // The draft is cleared only once the save AND its refetch have landed. Keeping
    // it after a successful write left the row holding a value that is no longer
    // current: if another writer changed the key in the meantime, the refetch
    // brought the new value in while the stale draft still shadowed it, and the next
    // blur committed the old one back. Clearing on failure instead would discard
    // what the user typed, so it is deliberately tied to success.
    onSave({ set: { [name]: draft } }, () =>
      setDrafts(d => {
        const copy = { ...d }
        delete copy[name]
        return copy
      }),
    )
  }

  /**
   * Commit on blur UNLESS focus is moving to this row's Remove button.
   *
   * Clicking Remove with an edited value fired both: blur sent a `set` and the
   * click sent a `delete`. Two in-flight writes for one key, and if the server
   * completed them in the other order the deleted key came back. The delete is the
   * user's actual intent, so the set is dropped — and the draft is cleared so a
   * later blur cannot resurrect it either.
   */
  const commitUnlessRemoving = (name: string, next: EventTarget | null) => {
    const target = next as HTMLElement | null
    if (target?.dataset?.removeFor === name) {
      setDrafts(d => { const copy = { ...d }; delete copy[name]; return copy })
      return
    }
    commit(name)
  }

  const remove = (name: string) => {
    setDrafts(d => { const copy = { ...d }; delete copy[name]; return copy })
    setError(null)
    onSave({ delete: [name] })
  }

  const add = () => {
    const bad = nameError(newName, names) ?? valueError(newValue)
    if (bad) { setError(bad); return }
    setError(null)
    // The fields are NOT cleared here. A save can be refused — a reserved name the
    // client grammar does not know, a gateway error — and clearing on dispatch threw
    // away what the user typed, leaving them an error message and an empty form with
    // nothing to correct or retry. The caller clears them once the write has actually
    // landed.
    onSave({ set: { [newName]: newValue } }, () => { setNewName(''); setNewValue('') })
  }

  return (
    <div className="flex flex-col gap-2">
      {names.length === 0 ? (
        <div className="text-[12px] text-muted">{i18nT('pages.settings.variablesPanel.no_variables_yet')}</div>
      ) : isMobile ? (
        /* One stacked card per variable: the name, then a full-width value field
           with its own visible label, then the scope line, then the action. Same
           data and the same actions as the table — a phone loses no capability. */
        <ul className="flex flex-col gap-3 list-none p-0 m-0">
          {names.map(name => {
            const rowValueId = `${uid}-m-${name}`
            const winner = winning[name] ?? scope
            const shadowed = (RANK[winner] ?? 0) > RANK[scope]
            const overridden = broader[name]
            return (
              <li key={name} className="flex flex-col gap-1 border-t border-border pt-2">
                <div className="flex items-start justify-between gap-2">
                  <span className="font-mono text-[12px] text-text break-all">{name}</span>
                  <IconButton
                    variant="danger"
                    aria-label={i18nT('pages.settings.variablesPanel.remove_variable', { name })}
                    data-remove-for={name}
                    disabled={busy}
                    onClick={() => remove(name)}
                  >
                    <Trash2 size={14} />
                  </IconButton>
                </div>
                <label htmlFor={rowValueId} className="text-[12px] font-semibold text-text">
                  {i18nT('pages.settings.variablesPanel.value')}
                </label>
                <Input
                  id={rowValueId}
                  aria-describedby={noteId}
                  value={drafts[name] ?? pairs[name]}
                  disabled={busy}
                  onChange={e => setDrafts(d => ({ ...d, [name]: e.target.value }))}
                  onBlur={e => commitUnlessRemoving(name, e.relatedTarget)}
                  onKeyDown={e => { if (e.key === 'Enter') commit(name) }}
                  className="w-full font-mono text-[12px] py-1"
                />
                <div className="text-[12px]">
                  <span className="text-text">{scopeLabel(winner)}</span>
                  {shadowed && (
                    <span className="block text-warn">{i18nT('pages.settings.variablesPanel.shadowed_by', { scope: scopeLabel(winner) })}</span>
                  )}
                  {!shadowed && overridden && (
                    <span className="block text-muted">{i18nT('pages.settings.variablesPanel.overrides', { scope: scopeLabel(overridden) })}</span>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      ) : (
        <table className="w-full text-[13px] border-collapse">
          <thead>
            <tr className="text-left text-[12px] text-muted">
              <th id={nameHeadId} scope="col" className="font-semibold pb-1 pr-3">{i18nT('pages.settings.variablesPanel.name')}</th>
              <th id={valueHeadId} scope="col" className="font-semibold pb-1 pr-3">{i18nT('pages.settings.variablesPanel.value')}</th>
              <th scope="col" className="font-semibold pb-1 pr-3">{i18nT('pages.settings.variablesPanel.scope')}</th>
              {/* Named rather than left blank: an unnamed column header is a hole in
                  the table's own description for a screen-reader user. */}
              <th scope="col" className="font-semibold pb-1">{i18nT('pages.settings.variablesPanel.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {names.map(name => {
              const rowNameId = `${uid}-row-${name}`
              const winner = winning[name] ?? scope
              const shadowed = (RANK[winner] ?? 0) > RANK[scope]
              const overridden = broader[name]
              return (
                <tr key={name} className="border-t border-border align-top">
                  <th id={rowNameId} scope="row" className="py-1.5 pr-3 font-mono text-[12px] text-text font-normal text-left">{name}</th>
                  <td className="py-1.5 pr-3">
                    <Input
                      // Labelled by the row's own name plus the column header, so the
                      // accessible name is "BASE_URL Value" — the visible text of both,
                      // which is what a table's cell semantics promise anyway.
                      aria-labelledby={`${rowNameId} ${valueHeadId}`}
                      aria-describedby={noteId}
                      value={drafts[name] ?? pairs[name]}
                      disabled={busy}
                      onChange={e => setDrafts(d => ({ ...d, [name]: e.target.value }))}
                      onBlur={e => commitUnlessRemoving(name, e.relatedTarget)}
                      onKeyDown={e => { if (e.key === 'Enter') commit(name) }}
                      className="w-full font-mono text-[12px] py-1"
                    />
                  </td>
                  <td className="py-1.5 pr-3 text-[12px]">
                    <span className="text-text">{scopeLabel(winner)}</span>
                    {shadowed && (
                      <span className="block text-warn">{i18nT('pages.settings.variablesPanel.shadowed_by', { scope: scopeLabel(winner) })}</span>
                    )}
                    {!shadowed && overridden && (
                      <span className="block text-muted">{i18nT('pages.settings.variablesPanel.overrides', { scope: scopeLabel(overridden) })}</span>
                    )}
                  </td>
                  <td className="py-1.5">
                    <IconButton
                      variant="danger"
                      aria-label={i18nT('pages.settings.variablesPanel.remove_variable', { name })}
                    data-remove-for={name}
                      disabled={busy}
                      onClick={() => remove(name)}
                    >
                      <Trash2 size={14} />
                    </IconButton>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      <div className="flex flex-col md:flex-row md:flex-wrap md:items-end gap-2 pt-1">
        <div className="flex flex-col gap-1">
          <label htmlFor={`${uid}-new-name`} className="text-[12px] font-semibold text-text">
            {i18nT('pages.settings.variablesPanel.name')}
          </label>
          <Input
            id={`${uid}-new-name`}
            value={newName}
            disabled={busy}
            aria-describedby={noteId}
            onChange={e => setNewName(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') add() }}
            className="font-mono text-[12px] py-1 w-full md:w-40"
          />
        </div>
        <div className="flex flex-col gap-1 flex-1 w-full md:min-w-[10rem]">
          <label htmlFor={`${uid}-new-value`} className="text-[12px] font-semibold text-text">
            {i18nT('pages.settings.variablesPanel.value')}
          </label>
          <Input
            id={`${uid}-new-value`}
            value={newValue}
            disabled={busy}
            aria-describedby={noteId}
            onChange={e => setNewValue(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') add() }}
            className="font-mono text-[12px] py-1 w-full"
          />
        </div>
        <Btn type="button" disabled={busy} onClick={add}>
          {i18nT('pages.settings.variablesPanel.add')}
        </Btn>
      </div>

      {/* `role="alert"` so a rejection reaches a screen-reader user the moment it
          appears — the field they just left is where the fix goes. */}
      {error && (
        <div role="alert" className="text-[12px] text-danger">{errorText(error)}</div>
      )}
    </div>
  )
}

/* ── Panel ── */

/**
 * Settings → Environment Variables: the global layer of the `{{name}}` cascade,
 * plus each workspace's own pairs.
 *
 * Values are pasted verbatim into agent-facing text, so the panel says out loud
 * that they are stored in plain text and are not a secret store — the alternative
 * is a user learning it from a config file in a shared repo.
 */
export function VariablesPanel() {
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')
  const noteId = React.useId()

  const q = useQuery<PanelData>({ queryKey: ['variables'], queryFn: fetchVariables })
  const data = q.data ?? EMPTY

  const saveMut = useMutation({
    mutationFn: async (body: VariablesWrite): Promise<{ ok?: boolean; error?: string; key?: string }> => {
      // `?.` for the partially-mocked / older-gateway case; an absent method reports
      // nothing rather than throwing past the panel.
      const r = await Promise.resolve(api.saveVariables?.(body))
      return r ?? {}
    },
    onSuccess: () => {
      setSaveError('')
      // RETURNED, not fired and forgotten: react-query keeps the mutation pending
      // until the promise settles, and `busy` (which gates every input) is derived
      // from `isPending`. Dropping the return re-enables the table while it is
      // still showing pre-save pairs, so a second save PUTs stale data and silently
      // discards the first — this write replaces named keys, so a stale read is a
      // lost update, not a stale display.
      return qc.invalidateQueries({ queryKey: ['variables'] })
    },
    onError: (err: unknown) => {
      // The backend's own explanation, when it sent one. Every refusal here is a
      // 4xx/5xx and the shared `j()` helper THROWS an `ApiError` carrying the raw
      // response as `body` — so a refusal never reaches onSuccess and a
      // 200-with-error branch would be dead code.
      //
      // The field is `body`. An earlier version of this read `detail`, which
      // `ApiError` does not define, so the whole unwrap was dead and every refusal
      // still showed the generic message instead of the reason the backend gave.
      const body = err instanceof ApiError ? err.body : ''
      if (body) {
        try {
          const parsed = JSON.parse(body) as { error?: string }
          if (parsed?.error) { setSaveError(parsed.error); return }
        } catch {
          // Not JSON — fall through to the generic message rather than showing a
          // raw response body.
        }
      }
      setSaveError(i18nT('pages.settings.variablesPanel.save_failed'))
    },
  })

  const busy = q.isLoading || saveMut.isPending
  const workspaceNames = Object.keys(data.workspaces).sort()

  return (
    <>
      <SettingsSection title={i18nT('pages.settings.variablesPanel.global_environment_variables')}>
        <SettingsCard>
          <p id={noteId} className="text-[12px] text-muted">
            {i18nT('pages.settings.variablesPanel.plain_text_note')}
          </p>
          {data.unavailable && !q.isLoading && (
            <p className="text-[12px] text-muted">{i18nT('pages.settings.variablesPanel.unavailable')}</p>
          )}
          <VariableTable
            scope="global"
            pairs={data.global}
            winning={data.winning_scope}
            broader={{}}
            busy={busy}
            noteId={noteId}
            onSave={(change, onLanded) => saveMut.mutate({ scope: 'global', ...change }, { onSuccess: onLanded })}
          />
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.variablesPanel.workspace_environment_variables')}>
        <SettingsCard index={1}>
          {workspaceNames.length === 0 ? (
            <p className="text-[12px] text-muted">{i18nT('pages.settings.variablesPanel.no_workspace_variables')}</p>
          ) : (
            <div className="flex flex-col gap-5">
              {workspaceNames.map(ws => {
                // Only `global` is broader than a workspace, so a workspace row that
                // wins while a global pair of the same name exists is an override.
                const broader: Record<string, Scope> = {}
                for (const name of Object.keys(data.workspaces[ws])) {
                  if (name in data.global) broader[name] = 'global'
                }
                return (
                  // Explicit `role="region"` rather than a bare <section>: the implicit
                  // mapping depends on the section having an accessible name, and the
                  // house convention (ActivityViewer, PinnedMessagesPanel) is to state
                  // it so assistive tech and tests agree on the landmark.
                  <div key={ws} role="region" aria-label={i18nT('pages.settings.variablesPanel.workspace_named', { name: ws })} className="flex flex-col gap-2">
                    <h5 className="text-[13px] font-semibold text-text-strong">{ws}</h5>
                    <VariableTable
                      scope="workspace"
                      pairs={data.workspaces[ws]}
                      // `winning_scope` describes the ACTIVE cascade only, so
                      // applying it to every workspace labelled a row "shadowed by
                      // Crew" when nothing shadows it there. An inactive
                      // workspace's rows get no shadow claim rather than another
                      // context's.
                      winning={ws === data.active_workspace ? data.winning_scope : {}}
                      broader={broader}
                      busy={busy}
                      noteId={noteId}
                      onSave={(change, onLanded) => saveMut.mutate({ scope: 'workspace', workspace: ws, ...change }, { onSuccess: onLanded })}
                    />
                  </div>
                )
              })}
            </div>
          )}
        </SettingsCard>
      </SettingsSection>

      <ErrorNotice message={saveError} className="mt-2" />
    </>
  )
}
