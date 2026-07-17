import { useState, useRef } from 'react'
import { Download, Upload, FileArchive, AlertCircle, CheckCircle } from 'lucide-react'
import { Card, CardTitle } from '../../components/ui'

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
    setExportStatus({ type: 'loading', msg: 'Generating export...' })
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
      setExportStatus({ type: 'ok', msg: 'Download started.' })
    } catch (e: unknown) {
      setExportStatus({ type: 'error', msg: e instanceof Error ? e.message : 'Network error' })
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
        setPreviewError(data.error || 'Invalid archive')
      }
    } catch {
      setPreviewError('Network error during preview')
    }
  }

  const handleImport = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) return
    if (mode === 'replace' && !confirm('Replace mode will overwrite existing data. Continue?')) return

    setImportStatus({ type: 'loading', msg: 'Importing...' })
    const fd = new FormData()
    fd.append('file', file)
    try {
      const resp = await fetch(`/api/portability/import?mode=${mode}`, { method: 'POST', body: fd })
      const data = await resp.json()
      if (data.ok) {
        const items = data.summary?.items || []
        setImportStatus({ type: 'ok', msg: `Import complete (${items.length} items). Restart gateway to apply all changes.` })
      } else {
        setImportStatus({ type: 'error', msg: data.error || 'Import failed' })
      }
    } catch (e: unknown) {
      setImportStatus({ type: 'error', msg: e instanceof Error ? e.message : 'Network error' })
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardTitle>Export Configuration</CardTitle>
        <p className="text-muted text-[13px] mb-3">
          Download all settings, memory, skills, crons, and lessons as a portable zip file.
          Credentials and session secrets are excluded.
        </p>
        <div className="flex items-center gap-3">
          <button
            onClick={handleExport}
            disabled={exportStatus.type === 'loading'}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-semibold font-body cursor-pointer bg-accent text-accent-fg border-none hover:bg-accent-hover transition-colors disabled:opacity-60"
          >
            <Download size={14} />
            {exportStatus.type === 'loading' ? 'Generating...' : 'Download Export (.zip)'}
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
        <CardTitle>Import Configuration</CardTitle>
        <p className="text-muted text-[13px] mb-3">
          Upload a KiroCrew export zip to restore settings on this instance.
          Existing data will be merged by default (duplicates are skipped).
        </p>
        <div className="flex items-center gap-3 flex-wrap">
          <label htmlFor="portability-import-file" className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-semibold font-body cursor-pointer bg-bg-elevated border border-border hover:border-accent transition-colors">
            <Upload size={14} />
            Choose file
            <input
              id="portability-import-file"
              ref={fileRef}
              type="file"
              accept=".zip"
              aria-label="Choose import file"
              onChange={handleFileChange}
              className="hidden"
            />
          </label>
          <select
            value={mode}
            onChange={e => setMode(e.target.value as 'merge' | 'replace')}
            className="h-9 px-3 rounded-lg bg-bg-elevated border border-border text-[13px] text-text font-body focus:border-accent focus:outline-none"
          >
            <option value="merge">Merge</option>
            <option value="replace">Replace</option>
          </select>
          <button
            onClick={handleImport}
            disabled={!preview || importStatus.type === 'loading'}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-semibold font-body cursor-pointer bg-accent text-accent-fg border-none hover:bg-accent-hover transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <FileArchive size={14} />
            {importStatus.type === 'loading' ? 'Importing...' : 'Import'}
          </button>
        </div>

        {preview && (
          <div className="mt-3 p-3 rounded-lg bg-bg-elevated border border-border text-[12px] font-mono space-y-1">
            <div className="font-semibold text-text mb-1">Archive contents:</div>
            {preview.contents['config.json'] != null && <div>Config: {(preview.contents['config.json'] / 1024).toFixed(1)} KB</div>}
            {preview.contents['memory.db'] != null && <div>Memory DB: {(preview.contents['memory.db'] / 1024).toFixed(1)} KB</div>}
            {preview.contents['crons.json'] != null && <div>Crons: {(preview.contents['crons.json'] / 1024).toFixed(1)} KB</div>}
            {preview.contents.workspace_files != null && <div>Workspace files: {preview.contents.workspace_files}</div>}
            {preview.contents.skill_count != null && <div>Skills: {preview.contents.skill_count}</div>}
            {preview.contents.plan_memory_files != null && <div>Plan memory files: {preview.contents.plan_memory_files}</div>}
            <div className="pt-1 border-t border-border mt-1 text-muted">
              Created: {preview.created_at} | From: {preview.user}@{preview.hostname}
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
