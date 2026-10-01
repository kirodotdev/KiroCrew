import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, GitBranch, Loader2, X } from 'lucide-react'

import { sageApi } from '../api'
import { api } from '../../../api/client'
import {
  REVIEW_MODEL_AUTO,
  type LocalFinding,
  type LocalReviewSession,
  type ReviewFixFindingSnapshot,
  type ReviewFixTaskResponse,
} from '../lib/types'
import { i18nT } from '../../../i18n/t'
import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import ReviewFixSetup from '../components/ReviewFixSetup'
import ReviewFixTaskPanel, { type ReviewFixTaskTransport } from '../../../components/ReviewFixTaskPanel'

const inputClass = 'w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-[13px] text-text outline-none focus:border-accent'

const reviewFixTransport: ReviewFixTaskTransport = {
  status: (taskId) => api.reviewFixStatus(taskId),
  action: (taskId, input) => api.reviewFixAction(taskId, input),
}

function Finding({
  finding, selected, onSelect, onDisposition,
}: {
  finding: LocalFinding
  selected: boolean
  onSelect: () => void
  onDisposition: (status: 'accepted' | 'dismissed', instruction?: string) => void
}) {
  const [userInstruction, setUserInstruction] = useState(finding.user_instruction ?? '')
  const severityLabel = finding.severity === 'error'
    ? i18nT('apps.codeReviewSage.components.findingCard.severity_must_fix')
    : finding.severity === 'warning'
      ? i18nT('apps.codeReviewSage.components.findingCard.severity_should_fix')
      : i18nT('apps.codeReviewSage.components.localReview.severity_info')
  const tone = finding.severity === 'error'
    ? 'border-danger text-danger'
    : finding.severity === 'warning' ? 'border-warn text-warn' : 'border-accent text-accent'
  return (
    <article className="rounded-lg border border-border bg-card p-3.5">
      <div className="flex items-start gap-2">
        <input
          type="checkbox"
          checked={selected}
          onChange={onSelect}
          aria-label={i18nT('apps.codeReviewSage.components.localReview.select_finding')}
          className="mt-1 accent-accent"
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${tone}`}>
              {severityLabel}
            </span>
            <code className="text-[12px] text-muted">{finding.file}:{finding.line}</code>
          </div>
          <h3 className="mt-2 text-[14px] font-semibold text-text-strong">{finding.title}</h3>
          <p className="mt-1.5 text-[13px] leading-relaxed text-text">{finding.message}</p>
          {finding.suggestion && (
            <p className="mt-2 border-l-2 border-accent pl-2 text-[12px] leading-relaxed text-muted">
              {finding.suggestion}
            </p>
          )}
        </div>
      </div>
      {finding.status === 'open' && (
        <div className="mt-3 flex items-center justify-end gap-2 border-t border-border pt-2">
          <input
            aria-label={i18nT('apps.codeReviewSage.components.localReview.human_instruction')}
            value={userInstruction}
            onChange={(event) => setUserInstruction(event.target.value)}
            placeholder={i18nT('apps.codeReviewSage.components.localReview.human_instruction')}
            className="min-w-0 flex-1 rounded-md border border-border bg-bg-elevated px-2 py-1 text-[12px] text-text outline-none focus:border-accent"
          />
          <Btn type="button" onClick={() => onDisposition('dismissed', userInstruction)}>
            <X className="lucide-inline" aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.localReview.dismiss')}
          </Btn>
          <Btn type="button" primary onClick={() => onDisposition('accepted', userInstruction)}>
            <Check className="lucide-inline" aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.localReview.accept')}
          </Btn>
        </div>
      )}
    </article>
  )
}

// Owns every piece of state scoped to one local-review session (selection,
// fix-setup, the in-progress fix task). Keyed by `session.id` from the
// parent so switching to a different session -- whether by the user
// starting a fresh review or the sessions-list poll changing which session
// is active -- remounts this subtree and resets all of it, instead of an
// open fix-setup or a stale finding selection silently carrying over onto
// the new session's findings.
function SessionWorkspace({
  session,
  onDispositionSubmit,
  dispositionErrorMessage,
}: {
  session: LocalReviewSession
  onDispositionSubmit: (findingId: string, status: 'accepted' | 'dismissed', userInstruction?: string) => void
  dispositionErrorMessage: string | null
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [fixModel, setFixModel] = useState(REVIEW_MODEL_AUTO)
  const [fixSetupOpen, setFixSetupOpen] = useState(false)
  const [fixFindings, setFixFindings] = useState<ReviewFixFindingSnapshot[]>([])
  const [fixTaskId, setFixTaskId] = useState<string | null>(null)

  const startFix = (findingsToFix: LocalFinding[]) => {
    if (findingsToFix.length === 0) return
    setFixFindings(findingsToFix.map((finding) => ({
      key: finding.id,
      title: finding.title,
      severity: finding.severity,
      body: finding.message,
      file_path: finding.file,
      line: finding.line,
      end_line: finding.end_line,
      fingerprint: finding.fingerprint,
      suggested_fix: finding.suggestion ?? undefined,
    })))
    setFixSetupOpen(true)
  }
  const onFixCreated = (response: ReviewFixTaskResponse) => {
    setFixTaskId(response.task_id)
    setFixSetupOpen(false)
    setSelected(new Set())
  }
  const openFindings = useMemo(
    () => (session.findings ?? []).filter((finding) => finding.status === 'open' || finding.status === 'accepted'),
    [session],
  )
  const statusLabel = session.status === 'reviewing'
    ? i18nT('apps.codeReviewSage.components.localReview.reviewing')
    : session.status === 'failed'
      ? i18nT('apps.autoResearch.researchLabPage.state_failed')
      : i18nT('apps.autoResearch.researchLabPage.state_done')

  return (
    <section className="mt-5 space-y-3" aria-live="polite">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-[15px] font-semibold text-text-strong">{session.repository}</h2>
          {session.revision && <p className="mt-1 text-[11px] text-muted">{i18nT('apps.codeReviewSage.components.localReview.revision', { revision: session.revision })}</p>}
        </div>
        <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">{statusLabel}</span>
      </div>
      {session.warning && <p className="flex items-center gap-2 rounded-md bg-warn-subtle p-2 text-[12px] text-warn"><AlertTriangle className="lucide-inline" aria-hidden="true" />{session.warning}</p>}
      {(session.findings_truncated ?? 0) > 0 && (
        <p className="flex items-center gap-2 rounded-md bg-warn-subtle p-2 text-[12px] text-warn">
          <AlertTriangle className="lucide-inline" aria-hidden="true" />
          {i18nT('apps.codeReviewSage.components.localReview.findings_truncated_notice', { count: session.findings_truncated })}
        </p>
      )}
      {fixSetupOpen && (
        <ReviewFixSetup
          findings={fixFindings}
          localSessionId={session.id}
          targetPath={session.repository}
          model={fixModel}
          onModelChange={setFixModel}
          onCreated={onFixCreated}
          onClose={() => setFixSetupOpen(false)}
        />
      )}
      {fixTaskId && (
        <ReviewFixTaskPanel
          taskId={fixTaskId}
          transport={reviewFixTransport}
          onOpenTaskRunner={(taskId) => {
            window.location.assign(`/projects?applied=${encodeURIComponent(taskId)}`)
          }}
        />
      )}
      {session.status === 'reviewing' ? (
        <p className="text-[13px] text-muted">{i18nT('apps.codeReviewSage.components.localReview.reviewing')}</p>
      ) : openFindings.length === 0 ? (
        <>
          <InlineDiff session={session} findings={openFindings} />
          <p className="rounded-lg border border-border bg-card p-4 text-[13px] text-muted">{i18nT('apps.codeReviewSage.components.localReview.no_findings')}</p>
        </>
      ) : (
        <>
          <InlineDiff session={session} findings={openFindings} />
          <p className="text-[12px] text-muted">{i18nT('apps.codeReviewSage.components.localReview.files_reviewed', { count: session.files?.length ?? 0 })}</p>
          <ErrorNotice
            variant="inline"
            message={dispositionErrorMessage}
            askAgent
            testId="local-review-disposition-error"
          />
          {openFindings.map((finding) => (
            <Finding
              key={finding.id}
              finding={finding}
              selected={selected.has(finding.id)}
              onSelect={() => setSelected((current) => {
                const next = new Set(current)
                if (next.has(finding.id)) next.delete(finding.id); else next.add(finding.id)
                return next
              })}
              onDisposition={(status, userInstruction) => onDispositionSubmit(finding.id, status, userInstruction)}
            />
          ))}
          {selected.size > 0 && (
            <div className="sticky bottom-3 rounded-lg border border-accent bg-card p-3 shadow-lg">
              <Btn
                type="button"
                primary
                onClick={() => startFix(openFindings.filter((finding) => selected.has(finding.id)))}
              >
                {i18nT('apps.codeReviewSage.components.localReview.fix_selected', { count: selected.size })}
              </Btn>
            </div>
          )}
        </>
      )}
    </section>
  )
}

function InlineDiff({ session, findings }: { session: LocalReviewSession; findings: LocalFinding[] }) {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card">
      {(session.files ?? []).map((file) => (
        <div key={file.path} className="border-b border-border last:border-b-0">
          <div className="border-b border-border bg-bg-elevated px-3 py-2 font-mono text-[12px] text-text">
            {file.path}
          </div>
          {(file.hunks ?? []).map((hunk, hunkIndex) => (
            <div key={`${file.path}-${hunkIndex}`} className="overflow-x-auto font-mono text-[12px] leading-relaxed">
              {hunk.lines.map((line, lineIndex) => {
                const anchored = line.kind === 'add'
                  ? findings.filter((finding) => finding.file === file.path && finding.line === line.new_line)
                  : []
                return (
                  <div key={`${file.path}-${hunkIndex}-${lineIndex}`}>
                    <div className={`flex min-w-max ${line.kind === 'add' ? 'bg-[var(--diff-add)]' : line.kind === 'delete' ? 'bg-[var(--diff-del)]' : ''}`}>
                      <span className="w-12 flex-shrink-0 select-none px-2 text-right text-muted">
                        {line.new_line ?? line.old_line ?? ''}
                      </span>
                      <span className="w-5 flex-shrink-0 select-none text-muted">
                        {line.kind === 'add' ? '+' : line.kind === 'delete' ? '-' : ' '}
                      </span>
                      <code className="whitespace-pre px-1 text-text">{line.content}</code>
                    </div>
                    {anchored.map((finding) => (
                      <div key={finding.id} className="ml-12 border-l-2 border-accent bg-accent-subtle px-3 py-2 font-sans text-[12px] text-text">
                        <span className="font-semibold">{finding.title}</span> — {finding.message}
                      </div>
                    ))}
                  </div>
                )
              })}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

export default function LocalReviewView() {
  const qc = useQueryClient()
  const [repository, setRepository] = useState('')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const sessions = useQuery({
    queryKey: ['code-review-sage', 'local-sessions'],
    queryFn: () => sageApi.localSessions(),
    refetchInterval: 5_000,
  })
  const activeId = sessionId ?? sessions.data?.sessions[0]?.id ?? null
  const active = useQuery({
    queryKey: ['code-review-sage', 'local-session', activeId],
    queryFn: () => sageApi.localSession(activeId as string),
    enabled: !!activeId,
    refetchInterval: (query) => (query.state.data?.session?.status === 'reviewing' ? 2_000 : false),
  })
  const review = useMutation({
    mutationFn: () => sageApi.localReview(repository, 'all-working-tree', activeId ?? undefined),
    onSuccess: (data) => {
      setSessionId(data.session.id)
      void qc.invalidateQueries({ queryKey: ['code-review-sage', 'local-sessions'] })
    },
  })
  const disposition = useMutation({
    mutationFn: ({ findingId, status, userInstruction }: {
      findingId: string
      status: 'accepted' | 'dismissed'
      userInstruction?: string
    }) => sageApi.localDisposition(activeId as string, findingId, status, userInstruction),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['code-review-sage', 'local-session', activeId] }),
  })
  const session = active.data?.session

  return (
    <main className="flex h-full min-h-0 flex-col overflow-auto bg-bg px-5 py-5 text-text">
      <div className="mx-auto w-full max-w-4xl">
        <div className="flex items-center gap-2">
          <GitBranch className="lucide-inline text-accent" aria-hidden="true" />
          <h1 className="text-[18px] font-semibold text-text-strong">{i18nT('apps.codeReviewSage.components.localReview.local')}</h1>
        </div>
        <ErrorNotice
          className="mt-3"
          message={sessions.error ? (sessions.error as Error).message : null}
          askAgent
          testId="local-review-sessions-error"
        />
        <ErrorNotice
          className="mt-3"
          message={active.error ? (active.error as Error).message : null}
          askAgent
          testId="local-review-active-error"
        />
        <form
          className="mt-5 rounded-lg border border-border bg-card p-4"
          onSubmit={(event) => { event.preventDefault(); if (repository.trim()) review.mutate() }}
        >
          <label className="block text-[13px] font-medium text-text" htmlFor="local-review-repository">
            {i18nT('apps.codeReviewSage.components.localReview.repository_path')}
            <input
              id="local-review-repository"
              aria-label={i18nT('apps.codeReviewSage.components.localReview.repository_path')}
              value={repository}
              onChange={(event) => setRepository(event.target.value)}
              placeholder={i18nT('apps.codeReviewSage.components.localReview.repository_path_hint')}
              className={`${inputClass} mt-2 block w-full`}
            />
          </label>
          <Btn type="submit" primary disabled={!repository.trim() || review.isPending} className="mt-3">
            {review.isPending && <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" aria-hidden="true" />}
            {review.isPending
              ? i18nT('apps.codeReviewSage.components.localReview.reviewing')
              : i18nT('apps.codeReviewSage.components.localReview.start_review')}
          </Btn>
          {/* No hand-off: the typed repository path is an unsaved draft — navigating
              to chat unmounts this form and discards it. The user re-submits here. */}
          <ErrorNotice
            variant="inline"
            className="mt-2"
            message={review.error ? i18nT('apps.codeReviewSage.components.failureNotice.this_review_failed') : null}
            testId="local-review-start-error"
          />
        </form>

        {session && (
          <SessionWorkspace
            key={session.id}
            session={session}
            onDispositionSubmit={(findingId, status, userInstruction) => disposition.mutate({ findingId, status, userInstruction })}
            dispositionErrorMessage={disposition.error ? (disposition.error as Error).message : null}
          />
        )}
      </div>
    </main>
  )
}
