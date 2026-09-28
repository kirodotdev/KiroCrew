import { useState, useRef } from 'react'
import { Download, Upload, FileArchive, AlertCircle, CheckCircle } from 'lucide-react'
import { Card, CardTitle } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'

import { i18nT } from '../../i18n/t'
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

/** A bounded list of names, with the left-out count as "N more". */
function joinWithMore(names: string[], more: number): string {
  return [...names, ...(more ? [i18nT('app.n_more', { count: more })] : [])].join(', ')
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
  const [importStatus, setImportStatus] = useState<{ type: 'idle' | 'loading' | 'ok' | 'error'; msg: string }>({ type: 'idle', msg: '' })
  const [exportWarning, setExportWarning] = useState('')
  const [importWarning, setImportWarning] = useState('')
  const [preview, setPreview] = useState<Manifest | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [mode, setMode] = useState<'merge' | 'replace'>('merge')
  const fileRef = useRef<HTMLInputElement>(null)

  const handleExport = async () => {
    setExportStatus({ type: 'loading', msg: i18nT('pages.overview.portabilityTab.generating_export') })
    setExportWarning('')
    try {
      const resp = await fetch('/api/portability/export')
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
    } catch (e: unknown) {
      setExportStatus({ type: 'error', msg: e instanceof Error ? e.message : i18nT('pages.overview.portabilityTab.network_error') })
    }
  }

  const handleFileChange = async () => {
    const file = fileRef.current?.files?.[0]
    setPreview(null)
    setPreviewError('')
    setImportStatus({ type: 'idle', msg: '' })
    setImportWarning('')
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
    setImportWarning('')
    const fd = new FormData()
    fd.append('file', file)
    try {
      const resp = await fetch(`/api/portability/import?mode=${mode}`, { method: 'POST', body: fd })
      const data = await resp.json()
      if (data.ok) {
        const items = data.summary?.items || []
        setImportStatus({ type: 'ok', msg: `Import complete (${items.length} items). Restart gateway to apply all changes.` })
        const missing: { crew: string; kiro_agent: string }[] = data.summary?.missing_agent_templates || []
        if (missing.length) {
          const more: number = data.summary?.missing_agent_templates_more || 0
          const crews = joinWithMore(missing.map(m => `${m.crew} → ${m.kiro_agent}`), more)
          setImportWarning(i18nT('pages.overview.portabilityTab.import_templates_missing', { crews }))
        }
      } else {
        setImportStatus({
          type: 'error',
          msg: refusalText(resp.status, data, i18nT('pages.overview.portabilityTab.import_failed')),
        })
      }
    } catch (e: unknown) {
      setImportStatus({ type: 'error', msg: e instanceof Error ? e.message : i18nT('pages.overview.portabilityTab.network_error') })
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardTitle>{i18nT('pages.overview.portabilityTab.export_configuration')}</CardTitle>
        <p className="text-muted text-[13px] mb-3">
          {i18nT('pages.overview.portabilityTab.download_all_settings_memory_skills_crons_and_le')}
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
        <WarnLine msg={importWarning} testId="portability-import-warning" />
      </Card>
    </div>
  )
}
