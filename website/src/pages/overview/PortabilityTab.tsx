import { useState, useRef } from 'react'
import { Download, Upload, FileArchive, AlertCircle, CheckCircle } from 'lucide-react'
import { Card, CardTitle } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'

import { i18nT } from '../../i18n/t'
interface Manifest {
  version: number
  created_at: string
  hostname: string
  user: string
  contents: Record<string, number>
}

export default function PortabilityTab() {
  const [exportStatus, setExportStatus] = useState<{ type: 'idle' | 'loading' | 'ok' | 'error'; msg: string }>({ type: 'idle', msg: '' })
  const [importStatus, setImportStatus] = useState<{ type: 'idle' | 'loading' | 'ok' | 'error'; msg: string }>({ type: 'idle', msg: '' })
  const [preview, setPreview] = useState<Manifest | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [mode, setMode] = useState<'merge' | 'replace'>('merge')
  const fileRef = useRef<HTMLInputElement>(null)

  const handleExport = async () => {
    setExportStatus({ type: 'loading', msg: i18nT('pages.overview.portabilityTab.generating_export') })
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
    } catch (e: unknown) {
      setExportStatus({ type: 'error', msg: e instanceof Error ? e.message : i18nT('pages.overview.portabilityTab.network_error') })
    }
  }

  const handleFileChange = async () => {
    const file = fileRef.current?.files?.[0]
    setPreview(null)
    setPreviewError('')
    setImportStatus({ type: 'idle', msg: '' })
    if (!file) return

    const fd = new FormData()
    fd.append('file', file)
    try {
      const resp = await fetch('/api/portability/preview', { method: 'POST', body: fd })
      const data = await resp.json()
      if (data.ok) {
        setPreview(data.manifest)
      } else {
        setPreviewError(data.error || i18nT('pages.overview.portabilityTab.invalid_archive'))
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
    const fd = new FormData()
    fd.append('file', file)
    try {
      const resp = await fetch(`/api/portability/import?mode=${mode}`, { method: 'POST', body: fd })
      const data = await resp.json()
      if (data.ok) {
        const items = data.summary?.items || []
        setImportStatus({ type: 'ok', msg: `Import complete (${items.length} items). Restart gateway to apply all changes.` })
      } else {
        setImportStatus({ type: 'error', msg: data.error || i18nT('pages.overview.portabilityTab.import_failed') })
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

        {previewError && (
          <div className="mt-3 text-danger text-[12px] inline-flex items-center gap-1">
            <AlertCircle size={12} /> {previewError}
          </div>
        )}

        {importStatus.msg && (
          <div className={`mt-3 text-[12px] inline-flex items-center gap-1 ${importStatus.type === 'ok' ? 'text-ok' : importStatus.type === 'error' ? 'text-danger' : 'text-muted'}`}>
            {importStatus.type === 'ok' && <CheckCircle size={12} />}
            {importStatus.type === 'error' && <AlertCircle size={12} />}
            {importStatus.msg}
          </div>
        )}
      </Card>
    </div>
  )
}
