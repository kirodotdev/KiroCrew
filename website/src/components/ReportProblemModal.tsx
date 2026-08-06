import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import {
  CheckCircle2,
  AlertCircle,
  FolderOpen,
  Download,
  ExternalLink,
  Loader2,
  Lock,
} from 'lucide-react'
import { Btn, Toggle } from './ui'
import Modal from './Modal'
import { api, ApiError } from '../api/client'

import { i18nT } from '../i18n/t'

type CollectResult = Awaited<ReturnType<typeof api.collectDiagnostics>>

interface ReportProblemModalProps {
  open: boolean
  onClose: () => void
}

/**
 * The "Report a Problem" flow, shared by every surface that offers it.
 *
 * Two entry points mount this same modal — Settings › About › Support
 * (`ReportProblemCard`) and the nav rail's "Report issue" link (`App.tsx`) —
 * so a user who reaches for the rail gets the redacted bundle instead of a bare
 * link to the issue tracker. Keeping ONE component means the collect call, the
 * redaction notice, and the three deliveries can never drift between surfaces.
 *
 * Calls the shared diagnostics collector (the same engine behind
 * `kirocrew doctor --bundle`): collects gateway + kiro-cli logs and crash
 * reports, scrubs secrets, zips them, and offers three deliveries — reveal in
 * Finder, download, or open a pre-filled GitHub issue.
 */
export default function ReportProblemModal({ open, onClose }: ReportProblemModalProps) {
  const [note, setNote] = useState('')
  const [includeLogs, setIncludeLogs] = useState(true)
  const [result, setResult] = useState<CollectResult | null>(null)
  const [error, setError] = useState('')

  const mut = useMutation({
    mutationFn: () => api.collectDiagnostics({ note, include_logs: includeLogs }),
    onMutate: () => {
      setError('')
      setResult(null)
    },
    onSuccess: (r) => setResult(r),
    onError: (e) =>
      setError(
        e instanceof ApiError
          ? e.message
          : i18nT('components.reportProblemModal.collect_failed'),
      ),
  })

  const close = () => {
    if (mut.isPending) return
    onClose()
    // Reset for the next open (after the close animation).
    window.setTimeout(() => {
      setResult(null)
      setError('')
      setNote('')
    }, 200)
  }

  return (
    <Modal
      open={open}
      onClose={close}
      title={i18nT('components.reportProblemModal.report_a_problem')}
      maxWidth={560}
      footer={
        result ? (
          <Btn primary onClick={close}>
            {i18nT('components.reportProblemModal.done')}
          </Btn>
        ) : (
          <>
            <Btn onClick={close} disabled={mut.isPending}>
              {i18nT('components.reportProblemModal.cancel')}
            </Btn>
            <Btn primary disabled={mut.isPending} onClick={() => mut.mutate()}>
              {mut.isPending ? (
                <>
                  <Loader2 size={13} className="lucide-inline animate-spin" />{' '}
                  {i18nT('components.reportProblemModal.collecting')}
                </>
              ) : (
                i18nT('components.reportProblemModal.create_report')
              )}
            </Btn>
          </>
        )
      }
    >
      {!result ? (
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <span className="text-[13px] text-text font-medium">
              {i18nT('components.reportProblemModal.what_happened')}
            </span>
            <textarea
              aria-label={i18nT('components.reportProblemModal.what_happened')}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={i18nT('components.reportProblemModal.note_placeholder')}
              rows={3}
              disabled={mut.isPending}
              className="text-sm px-2.5 py-2 rounded-md bg-bg border border-border resize-none"
            />
          </div>

          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-[13px] text-text font-medium">
                {i18nT('components.reportProblemModal.include_recent_logs')}
              </div>
              <div className="text-[12px] text-muted">
                {i18nT('components.reportProblemModal.include_logs_hint')}
              </div>
            </div>
            <Toggle checked={includeLogs} onChange={setIncludeLogs} disabled={mut.isPending} />
          </div>

          <div
            className="text-[12px] text-muted rounded-md border border-border px-3 py-2 flex items-start gap-2"
            style={{ background: 'var(--ok-subtle)' }}
          >
            <Lock size={13} className="lucide-inline mt-0.5 shrink-0" />
            <span>{i18nT('components.reportProblemModal.redaction_notice')}</span>
          </div>

          {error && (
            <div className="text-[13px] text-danger flex items-center gap-1.5">
              <AlertCircle size={13} className="lucide-inline" /> {error}
            </div>
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="text-[13px] text-ok flex items-center gap-1.5">
            <CheckCircle2 size={14} className="lucide-inline" />{' '}
            {i18nT('components.reportProblemModal.diagnostics_ready', {
              redactions: result.total_redactions,
              files: result.included.length,
            })}
          </div>
          <div className="text-[12px] text-muted break-all">
            {i18nT('components.reportProblemModal.saved_to')} <code>{result.zip_path}</code>
          </div>
          <div className="flex flex-wrap gap-2">
            <Btn onClick={() => api.revealPath(result.zip_path)}>
              <FolderOpen size={13} className="lucide-inline" />{' '}
              {i18nT('components.reportProblemModal.show_in_finder')}
            </Btn>
            <a href={result.download_url} download>
              <Btn>
                <Download size={13} className="lucide-inline" />{' '}
                {i18nT('components.reportProblemModal.download_zip')}
              </Btn>
            </a>
            <a href={result.github_issue_url} target="_blank" rel="noopener noreferrer">
              <Btn primary>
                <ExternalLink size={13} className="lucide-inline" />{' '}
                {i18nT('components.reportProblemModal.open_github_issue')}
              </Btn>
            </a>
          </div>
          <div className="text-[12px] text-muted">
            {i18nT('components.reportProblemModal.prefill_hint')}
          </div>
        </div>
      )}
    </Modal>
  )
}
