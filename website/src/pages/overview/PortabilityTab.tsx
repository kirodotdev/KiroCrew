import { useEffect, useState, useRef } from 'react'
import { Download, Upload, FileArchive, AlertCircle, CheckCircle } from 'lucide-react'
import { Card, CardTitle } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'

import { i18nT } from '../../i18n/t'
import { adoptHostUiPrefsOnNextLoad, pauseUiPrefsSync, resumeUiPrefsSync } from '../../lib/uiPrefs'

/**
 * Where a settings restore leaves its result for the load it triggers.
 *
 * A restore of browser settings reloads the page at once (the sync stays paused
 * until a load adopts the host copy), which would wipe the success line and the
 * warnings before anyone reads them; the tab re-shows them from here once, then
 * drops the key.
 */
export const IMPORT_RESULT_KEY = 'kc-portability-import-result'

interface ImportResult {
  msg: string
  warnings: string[]
  /** Failures inside an import that otherwise succeeded: items it refused and left unchanged. */
  errors: string[]
  /** What the import did with the archive's chats (`chatImportLines`); absent when it carried none. */
  chats?: string[]
}

/** Non-empty strings only; anything else a stored record holds is dropped. */
function storedStrings(raw: unknown): string[] {
  return Array.isArray(raw) ? raw.filter((w): w is string => typeof w === 'string' && w !== '') : []
}

/** The result a restore carried across its reload, or null if none (or unreadable). */
export function readCarriedImportResult(): ImportResult | null {
  try {
    const raw: unknown = JSON.parse(sessionStorage.getItem(IMPORT_RESULT_KEY) ?? 'null')
    if (!raw || typeof raw !== 'object') return null
    const { msg, warnings, errors, chats } = raw as { msg?: unknown; warnings?: unknown; errors?: unknown; chats?: unknown }
    if (typeof msg !== 'string' || !msg) return null
    return { msg, warnings: storedStrings(warnings), errors: storedStrings(errors), chats: storedStrings(chats) }
  } catch {
    return null
  }
}

function carryImportResult(result: ImportResult): void {
  try {
    sessionStorage.setItem(IMPORT_RESULT_KEY, JSON.stringify(result))
  } catch {
    /* storage blocked: the reload still has to happen, only the record is lost */
  }
}

function dropCarriedImportResult(): void {
  try {
    sessionStorage.removeItem(IMPORT_RESULT_KEY)
  } catch {
    /* storage blocked: nothing was carried */
  }
}

interface Manifest {
  version: number
  created_at: string
  hostname: string
  user: string
  contents: Record<string, number>
}

/**
 * The message to show for a refused portability call.
 *
 * A 5xx from these endpoints answers with deliberately opaque boilerplate
 * ("Export failed", "Import failed", "Preview failed") — English produced in
 * Python that never passes through the i18n catalog, and that says no more than
 * *fallback* already says in the reader's language. A 4xx carries the archive
 * validator's own detail ("missing manifest.json"), which is the whole value of
 * the message, so it is preserved.
 *
 * Gated on `code` as well as status: a refusal with no machine-readable
 * identity may be from something other than these handlers, and there the prose
 * can be the only detail available.
 */
export function refusalText(
  status: number,
  data: { error?: string; code?: string },
  fallback: string,
): string {
  if (data.code && status >= 500) return fallback
  return data.error || fallback
}

/**
 * The export's warning header: a JSON list of the agent templates the bundle does
 * not carry, ending in `"+N"` when the server left N more out.
 */
export function unbundledTemplates(header: string | null): { names: string[]; more: number } {
  try {
    const raw: unknown = JSON.parse(header || '[]')
    const names = Array.isArray(raw) ? raw.filter((n): n is string => typeof n === 'string') : []
    const tail = /^\+(\d+)$/.exec(names[names.length - 1] ?? '')
    return tail ? { names: names.slice(0, -1), more: Number(tail[1]) } : { names, more: 0 }
  } catch {
    return { names: [], more: 0 }
  }
}

/**
 * The export's `X-Kirocrew-Sessions-Skipped-Size` header: how many chats the server
 * left out so the archive stays within what import accepts. A bare non-negative
 * integer; anything else reads as 0.
 */
export function skippedChats(header: string | null): number {
  return header && /^\d{1,9}$/.test(header) ? Number(header) : 0
}

/** A bounded list of names, with the left-out count as "N more". */
function joinWithMore(names: string[], more: number): string {
  return [...names, ...(more ? [i18nT('app.n_more', { count: more })] : [])].join(', ')
}

/** Strings only, empty ones and repeats dropped, in first-seen order. */
function distinctNames(raw: unknown): string[] {
  if (!Array.isArray(raw)) return []
  return [...new Set(raw.filter((n): n is string => typeof n === 'string' && n !== ''))]
}

/**
 * The summary's `settings_kept`: the archive's settings documents a Merge left
 * alone because this install keeps its own. The server only ever names its four
 * fixed settings files, so four is the bound by construction; the cap here keeps
 * a malformed response from growing the line.
 */
export function keptSettingsFiles(raw: unknown): string[] {
  return distinctNames(raw).slice(0, 4)
}

/**
 * What a kept settings file is called in the warning: the thing it holds, in the
 * reader's words, not its file name. A name the server does not send today falls
 * back to itself, so a new file still shows up rather than vanishing.
 */
export function settingsFileLabel(file: string): string {
  switch (file) {
    case 'config.json': return i18nT('pages.overview.portabilityTab.settings_kept_name_config')
    case 'config.local.json': return i18nT('pages.overview.portabilityTab.settings_kept_name_config_local')
    case 'ui-prefs.json': return i18nT('pages.overview.portabilityTab.settings_kept_name_ui_prefs')
    case 'notification_settings.json': return i18nT('pages.overview.portabilityTab.settings_kept_name_notifications')
    default: return file
  }
}

/** How many refused items are named before the rest become "N more". */
const REFUSED_SHOWN = 6

/**
 * The summary's `refused_merges`: the items an import refused and left unchanged
 * (fixed labels such as `config` or `crons`), bounded with an "N more" overflow.
 */
export function refusedItems(raw: unknown): { names: string[]; more: number } {
  const all = distinctNames(raw)
  return { names: all.slice(0, REFUSED_SHOWN), more: Math.max(0, all.length - REFUSED_SHOWN) }
}

/**
 * The parenthetical an item carries when the import did NOT apply it. These are
 * the openers `portability.py` really emits, nothing broader: a refused document
 * or store ("<label> (skipped: why)", "(SKIPPED: why)", "hooks (skipped, already
 * exists)"), a settings file a Merge left as this install's ("(kept this
 * install's; ...)", "memory_stores/<x> (kept the existing store; ...)"), the
 * overlay a Merge never installs ("config.local (not installed: ...)"), and an
 * archive copy with no storable entry ("ui-prefs (nothing to restore...)").
 * Anchored to the opening paren so "skills (merged, auto/ skipped)" -- an item
 * that WAS applied -- still counts.
 */
const NOT_APPLIED_ITEM = /\((?:skipped\b|kept\b|not installed\b|nothing to restore\b)/i

/**
 * How many summary items the import actually applied. An item it refused, kept,
 * did not install or found nothing to restore from rides the list with one of the
 * `NOT_APPLIED_ITEM` openers and is reported on its own warning line instead --
 * counting it here too would tell the user N items were imported while the very
 * next line says some of those N were left untouched.
 */
export function importedItemCount(items: unknown): number {
  if (!Array.isArray(items)) return 0
  return items.filter(i => !(typeof i === 'string' && NOT_APPLIED_ITEM.test(i))).length
}

/** A non-negative integer from the import summary, or null when the field is absent or malformed. */
function summaryCount(raw: unknown): number | null {
  return typeof raw === 'number' && Number.isInteger(raw) && raw >= 0 ? raw : null
}

/**
 * The lines an import reports about chats: how many it added and left as they
 * were, then, only when there were any, how many it would not install. Empty
 * when the archive carried no chats (the summary then has no `sessions_added`).
 */
export function chatImportLines(summary: Record<string, unknown>): string[] {
  const added = summaryCount(summary.sessions_added)
  if (added === null) return []
  const existing = summaryCount(summary.sessions_skipped_existing) ?? 0
  const withheld = summaryCount(summary.sessions_withheld) ?? 0
  const lines = [i18nT('pages.overview.portabilityTab.import_chats_result', { added, existing })]
  if (withheld) lines.push(i18nT('pages.overview.portabilityTab.import_chats_withheld', { withheld }))
  return lines
}

/** A status line for a warning the call still succeeded through. */
function WarnLine({ msg, testId }: { msg: string; testId: string }) {
  if (!msg) return null
  return (
    <div role="status" data-testid={testId} className="mt-3 text-[12px] inline-flex items-start gap-1 text-warn">
      <AlertCircle size={12} className="mt-0.5 shrink-0" />
      {msg}
    </div>
  )
}

export default function PortabilityTab() {
  const [exportStatus, setExportStatus] = useState<{ type: 'idle' | 'loading' | 'ok' | 'error'; msg: string }>({ type: 'idle', msg: '' })
  // Read during render without side effects; the key is dropped by the effect
  // below, so a later mount does not show the same result again.
  const [carried] = useState(readCarriedImportResult)
  const [importStatus, setImportStatus] = useState<{ type: 'idle' | 'loading' | 'ok' | 'error'; msg: string }>(
    carried ? { type: 'ok', msg: carried.msg } : { type: 'idle', msg: '' },
  )
  const [exportWarning, setExportWarning] = useState('')
  const [exportChatsSkipped, setExportChatsSkipped] = useState('')
  const [importWarnings, setImportWarnings] = useState<string[]>(carried?.warnings ?? [])
  const [importErrors, setImportErrors] = useState<string[]>(carried?.errors ?? [])
  const [preview, setPreview] = useState<Manifest | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [mode, setMode] = useState<'merge' | 'replace'>('merge')
  const [includeChats, setIncludeChats] = useState(false)
  const [chatLines, setChatLines] = useState<string[]>(carried?.chats ?? [])
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    dropCarriedImportResult()
  }, [])

  const handleExport = async () => {
    setExportStatus({ type: 'loading', msg: i18nT('pages.overview.portabilityTab.generating_export') })
    setExportWarning('')
    setExportChatsSkipped('')
    try {
      const resp = await fetch(includeChats ? '/api/portability/export?include_sessions=true' : '/api/portability/export')
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: resp.statusText }))
        setExportStatus({ type: 'error', msg: err.error || resp.statusText })
        return
      }
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const cd = resp.headers.get('Content-Disposition') || ''
      const m = cd.match(/filename="?([^"]+)"?/)
      a.download = m ? m[1] : 'kirocrew-export.zip'
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      setExportStatus({ type: 'ok', msg: i18nT('pages.overview.portabilityTab.download_started') })
      const { names, more } = unbundledTemplates(resp.headers.get('X-Kirocrew-Unbundled-Templates'))
      if (names.length) setExportWarning(i18nT('pages.overview.portabilityTab.export_templates_not_included', { names: joinWithMore(names, more) }))
      const skipped = includeChats ? skippedChats(resp.headers.get('X-Kirocrew-Sessions-Skipped-Size')) : 0
      if (skipped > 0) setExportChatsSkipped(i18nT('pages.overview.portabilityTab.export_chats_skipped_size', { chats: skipped }))
    } catch (e: unknown) {
      setExportStatus({ type: 'error', msg: e instanceof Error ? e.message : i18nT('pages.overview.portabilityTab.network_error') })
    }
  }

  const handleFileChange = async () => {
    const file = fileRef.current?.files?.[0]
    setPreview(null)
    setPreviewError('')
    setImportStatus({ type: 'idle', msg: '' })
    setImportWarnings([])
    setImportErrors([])
    setChatLines([])
    if (!file) return

    const fd = new FormData()
    fd.append('file', file)
    try {
      const resp = await fetch('/api/portability/preview', { method: 'POST', body: fd })
      const data = await resp.json()
      if (data.ok) {
        setPreview(data.manifest)
      } else {
        setPreviewError(refusalText(resp.status, data, i18nT('pages.overview.portabilityTab.invalid_archive')))
      }
    } catch {
      setPreviewError(i18nT('pages.overview.portabilityTab.network_error_during_preview'))
    }
  }

  const handleImport = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) return
    if (mode === 'replace' && !confirm(i18nT('pages.overview.portabilityTab.replace_mode_will_overwrite_existing_data_contin'))) return

    setImportStatus({ type: 'loading', msg: i18nT('pages.overview.portabilityTab.importing') })
    setImportWarnings([])
    setImportErrors([])
    setChatLines([])
    const fd = new FormData()
    fd.append('file', file)
    // The import may rewrite the host's copy of this browser's preferences, so
    // no flush may race it (see pauseUiPrefsSync). Resumed below unless the
    // copy really changed, in which case the page reloads to adopt it.
    await pauseUiPrefsSync()
    let adopting = false
    try {
      const resp = await fetch(`/api/portability/import?mode=${mode}`, { method: 'POST', body: fd })
      const data = await resp.json()
      if (data.ok) {
        const summary = data.summary || {}
        const warnings: string[] = []
        const missing: { crew: string; kiro_agent: string }[] = summary.missing_agent_templates || []
        if (missing.length) {
          const more: number = summary.missing_agent_templates_more || 0
          const crews = joinWithMore(missing.map(m => `${m.crew} → ${m.kiro_agent}`), more)
          warnings.push(i18nT('pages.overview.portabilityTab.import_templates_missing', { crews }))
        }
        // A refused item is a failure, not a caveat: it renders as an error even
        // though the import as a whole succeeded.
        const errors: string[] = []
        const refused = refusedItems(summary.refused_merges)
        if (refused.names.length) {
          errors.push(i18nT('pages.overview.portabilityTab.import_items_refused', { names: joinWithMore(refused.names, refused.more) }))
        }
        const keptFiles = keptSettingsFiles(summary.settings_kept)
        if (keptFiles.length) {
          warnings.push(i18nT('pages.overview.portabilityTab.import_settings_kept', { files: keptFiles.map(settingsFileLabel).join(', ') }))
        }
        const msg = i18nT('pages.overview.portabilityTab.import_complete_restart_gateway', { count: importedItemCount(summary.items) })
        const chats = chatImportLines(summary)
        setImportWarnings(warnings)
        setImportErrors(errors)
        setChatLines(chats)
        setImportStatus({ type: 'ok', msg })
        if (summary.ui_prefs_restored) {
          // The restored browser settings live in localStorage, which only a
          // fresh load re-reads from the host; config-owned ones (theme,
          // language) are re-read by the same load. Adopting leaves the sync
          // paused so nothing overwrites the restored copy, which is only safe
          // if this page goes away now: a preference changed on it would be
          // replaced by the host copy on the next load. So it reloads only
          // after adoption is armed, and carries the result across for the next
          // mount. A failed adoption leaves the result and warnings here.
          if (!await adoptHostUiPrefsOnNextLoad()) return
          adopting = true
          // Said before the reload, so the page does not just blink: the full
          // result is re-shown from sessionStorage once the next load mounts.
          setImportStatus({ type: 'ok', msg: i18nT('pages.overview.portabilityTab.import_reloading_display_settings') })
          carryImportResult({ msg, warnings, errors, ...(chats.length ? { chats } : {}) })
          window.location.reload()
        }
      } else {
        setImportStatus({
          type: 'error',
          msg: refusalText(resp.status, data, i18nT('pages.overview.portabilityTab.import_failed')),
        })
      }
    } catch (e: unknown) {
      setImportStatus({ type: 'error', msg: e instanceof Error ? e.message : i18nT('pages.overview.portabilityTab.network_error') })
    } finally {
      if (!adopting) resumeUiPrefsSync()
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardTitle>{i18nT('pages.overview.portabilityTab.export_configuration')}</CardTitle>
        <p className="text-muted text-[13px] mb-3">
          {i18nT('pages.overview.portabilityTab.download_all_settings_memory_skills_crons_and_le')}
        </p>
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-[13px]">
          <input
            type="checkbox"
            aria-label={i18nT('pages.overview.portabilityTab.include_chat_history')}
            className="h-4 w-4 shrink-0 accent-accent"
            checked={includeChats}
            disabled={exportStatus.type === 'loading'}
            aria-describedby="portability-include-chats-hint"
            onChange={event => setIncludeChats(event.target.checked)}
          />
          {i18nT('pages.overview.portabilityTab.include_chat_history')}
        </label>
        <p id="portability-include-chats-hint" className="text-text text-[12px] mb-3">
          {i18nT('pages.overview.portabilityTab.include_chat_history_hint')}
        </p>
        <div className="flex items-center gap-3">
          <button
            onClick={handleExport}
            disabled={exportStatus.type === 'loading'}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-semibold font-body cursor-pointer bg-accent text-accent-fg border-none hover:bg-accent-hover transition-colors disabled:opacity-60"
          >
            <Download size={14} />
            {exportStatus.type === 'loading' ? i18nT('pages.overview.portabilityTab.generating') : i18nT('pages.overview.portabilityTab.download_export_zip')}
          </button>
          {exportStatus.msg && (
            <span className={`text-[12px] inline-flex items-center gap-1 ${exportStatus.type === 'ok' ? 'text-ok' : exportStatus.type === 'error' ? 'text-danger' : 'text-muted'}`}>
              {exportStatus.type === 'ok' && <CheckCircle size={12} />}
              {exportStatus.type === 'error' && <AlertCircle size={12} />}
              {exportStatus.msg}
            </span>
          )}
        </div>
        <WarnLine msg={exportWarning} testId="portability-export-warning" />
        <WarnLine msg={exportChatsSkipped} testId="portability-export-chats-skipped" />
      </Card>

      <Card>
        <CardTitle>{i18nT('pages.overview.portabilityTab.import_configuration')}</CardTitle>
        <p className="text-muted text-[13px] mb-3">
          {i18nT('pages.overview.portabilityTab.upload_a_kirocrew_export_zip_to_restore_settings')}
        </p>
        <div className="flex items-center gap-3 flex-wrap">
          <label htmlFor="portability-import-file" className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-semibold font-body cursor-pointer bg-bg-elevated border border-border hover:border-accent transition-colors">
            <Upload size={14} />
            {i18nT('pages.overview.portabilityTab.choose_file')}
            <input
              id="portability-import-file"
              ref={fileRef}
              type="file"
              accept=".zip"
              aria-label={i18nT('pages.overview.portabilityTab.choose_import_file')}
              onChange={handleFileChange}
              className="hidden"
            />
          </label>
          <SimpleSelect
            aria-label={i18nT('pages.overview.portabilityTab.mode')}
            options={['merge', 'replace']}
            optionLabels={[i18nT('pages.overview.portabilityTab.merge'), i18nT('pages.overview.portabilityTab.replace')]}
            value={mode}
            onChange={v => setMode(v as 'merge' | 'replace')}
          />
          <button
            onClick={handleImport}
            disabled={!preview || importStatus.type === 'loading'}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-semibold font-body cursor-pointer bg-accent text-accent-fg border-none hover:bg-accent-hover transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <FileArchive size={14} />
            {importStatus.type === 'loading' ? i18nT('pages.overview.portabilityTab.importing') : i18nT('pages.overview.portabilityTab.import')}
          </button>
        </div>

        {preview && (
          <div className="mt-3 p-3 rounded-lg bg-bg-elevated border border-border text-[12px] font-mono space-y-1">
            <div className="font-semibold text-text mb-1">{i18nT('pages.overview.portabilityTab.archive_contents')}</div>
            {preview.contents['config.json'] != null && <div>{i18nT('pages.overview.portabilityTab.config')} {(preview.contents['config.json'] / 1024).toFixed(1)} {i18nT('pages.overview.portabilityTab.kb')}</div>}
            {preview.contents['memory.db'] != null && <div>{i18nT('pages.overview.portabilityTab.memory_db')} {(preview.contents['memory.db'] / 1024).toFixed(1)} {i18nT('pages.overview.portabilityTab.kb')}</div>}
            {preview.contents['crons.json'] != null && <div>{i18nT('pages.overview.portabilityTab.crons')} {(preview.contents['crons.json'] / 1024).toFixed(1)} {i18nT('pages.overview.portabilityTab.kb')}</div>}
            {preview.contents.workspace_files != null && <div>{i18nT('pages.overview.portabilityTab.workspace_files')} {preview.contents.workspace_files}</div>}
            {preview.contents.skill_count != null && <div>{i18nT('pages.overview.portabilityTab.skills')} {preview.contents.skill_count}</div>}
            {preview.contents.plan_memory_files != null && <div>{i18nT('pages.overview.portabilityTab.plan_memory_files')} {preview.contents.plan_memory_files}</div>}
            {preview.contents.session_count != null
              ? <div>{i18nT('pages.overview.portabilityTab.chats_count', { chats: preview.contents.session_count })}</div>
              : <div data-testid="portability-preview-no-chats" className="text-muted">{i18nT('pages.overview.portabilityTab.archive_has_no_chats')}</div>}
            <div className="pt-1 border-t border-border mt-1 text-muted">
              {i18nT('pages.overview.portabilityTab.created')} {preview.created_at} {i18nT('pages.overview.portabilityTab.from')} {preview.user}@{preview.hostname}
            </div>
          </div>
        )}

        {/* No hand-off: the chosen archive lives in the file input above and in
            `preview`, neither of which is saved anywhere durable. The hand-off
            unmounts this tab, and a `File` cannot be restored programmatically,
            so the user would have to pick the archive again. */}
        <ErrorNotice variant="inline" message={previewError} className="mt-3" testId="portability-preview-error" />

        {/* No hand-off: same unsaved archive selection as the preview error
            above. Only the failure branch moves to `ErrorNotice`; the success
            and progress lines are not errors and must not be dressed as one. */}
        {importStatus.type === 'error'
          ? <ErrorNotice variant="inline" message={importStatus.msg} className="mt-3" testId="portability-import-error" />
          : importStatus.msg && (
            <div className={`mt-3 text-[12px] inline-flex items-center gap-1 ${importStatus.type === 'ok' ? 'text-ok' : 'text-muted'}`}>
              {importStatus.type === 'ok' && <CheckCircle size={12} />}
              {importStatus.msg}
            </div>
          )}
        {/* No hand-off: the same unsaved archive selection as the import error
            above -- the chosen file cannot be restored after this tab unmounts. */}
        {importErrors.map((e, i) => (
          <ErrorNotice key={i} variant="inline" message={e} className="mt-3" testId="portability-import-refused" />
        ))}
        {chatLines.length > 0 && (
          <div className="flex flex-col items-start">
            {chatLines.map((line, i) => (
              <div key={i} role="status" data-testid="portability-import-chats" className="mt-3 text-[12px] text-muted">
                {line}
              </div>
            ))}
          </div>
        )}
        {importWarnings.length > 0 && (
          <div className="flex flex-col items-start">
            {importWarnings.map((w, i) => <WarnLine key={i} msg={w} testId="portability-import-warning" />)}
          </div>
        )}
      </Card>
    </div>
  )
}
