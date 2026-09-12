import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, GitBranch, Loader2, Wrench, X } from 'lucide-react'

import { sageApi } from '../api'
import type { LocalFinding, LocalReviewSession } from '../lib/types'
import { i18nT } from '../../../i18n/t'
import { Badge, Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'

const inputClass = 'w-full rounded-md border border-border bg-bg-elevated px-3 py-2 text-[13px] text-text outline-none focus:border-accent'

/** A fix run outlives the review it came from: the session's own status has
 *  already settled to `completed` while the fix agent is still working, so the
 *  poller reads the LAST run too. Anything unknown is treated as still moving —
 *  a poll that stops early strands a fix run at "running" forever. */
const FIX_RUN_ACTIVE_STATUSES = new Set(['running', 'pending', 'queued'])

/** The last fix run is the one the user just asked for; earlier runs are history
 *  this panel does not replay. Returns null before the first run. */
function lastFixRun(session: LocalReviewSession | undefined) {
  const runs = session?.fix_runs ?? []
  return runs.length > 0 ? runs[runs.length - 1] : undefined
}

function fixRunStatusLabel(status: string): string {
  if (status === 'running' || status === 'pending' || status === 'queued') {
    return i18nT('apps.codeReviewSage.reviewFix.taskPanel.states.running')
  }
  if (status === 'completed') return i18nT('apps.codeReviewSage.reviewFix.taskPanel.states.done')
  if (status === 'failed') return i18nT('apps.codeReviewSage.reviewFix.taskPanel.states.failed')
  return status
}

function fixRunStatusVariant(status: string): 'ok' | 'err' | 'aim' | 'muted' {
  if (status === 'completed') return 'ok'
  if (status === 'failed') return 'err'
  if (FIX_RUN_ACTIVE_STATUSES.has(status)) return 'aim'
  return 'muted'
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
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [instruction, setInstruction] = useState('')
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
    refetchInterval: (query) => {
      const session = query.state.data?.session
      if (session?.status === 'reviewing') return 2_000
      const run = lastFixRun(session)
      return run && FIX_RUN_ACTIVE_STATUSES.has(run.status) ? 2_000 : false
    },
  })
  const review = useMutation({
    mutationFn: () => sageApi.localReview(repository, 'all-working-tree', activeId ?? undefined),
    onSuccess: (data) => {
      setSessionId(data.session.id)
      setSelected(new Set())
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
  const fix = useMutation({
    mutationFn: () => sageApi.localFix(activeId as string, [...selected], instruction),
    onSuccess: () => {
      setSelected(new Set())
      void qc.invalidateQueries({ queryKey: ['code-review-sage', 'local-session', activeId] })
    },
  })
  const session = active.data?.session
  const fixRun = lastFixRun(session)
  const openFindings = useMemo(
    () => (session?.findings ?? []).filter((finding) => finding.status === 'open' || finding.status === 'accepted'),
    [session],
  )
  const statusLabel = session?.status === 'reviewing'
    ? i18nT('apps.codeReviewSage.components.localReview.reviewing')
    : session?.status === 'failed'
      ? i18nT('apps.autoResearch.researchLabPage.state_failed')
      : i18nT('apps.autoResearch.researchLabPage.state_done')

  return (
    <main className="flex h-full min-h-0 flex-col overflow-auto bg-bg px-5 py-5 text-text">
      <div className="mx-auto w-full max-w-4xl">
        <div className="flex items-center gap-2">
          <GitBranch className="lucide-inline text-accent" aria-hidden="true" />
          <h1 className="text-[18px] font-semibold text-text-strong">{i18nT('apps.codeReviewSage.components.localReview.local')}</h1>
        </div>
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
          <section className="mt-5 space-y-3" aria-live="polite">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="text-[15px] font-semibold text-text-strong">{session.repository}</h2>
                {session.revision && <p className="mt-1 text-[11px] text-muted">{i18nT('apps.codeReviewSage.components.localReview.revision', { revision: session.revision })}</p>}
              </div>
              <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">{statusLabel}</span>
            </div>
            {session.warning && <p className="flex items-center gap-2 rounded-md bg-warn-subtle p-2 text-[12px] text-warn"><AlertTriangle className="lucide-inline" aria-hidden="true" />{session.warning}</p>}
            {fixRun && (
              <div
                className="rounded-lg border border-border bg-card p-3 flex flex-col gap-2"
                aria-live="polite"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Wrench size={13} className="lucide-inline text-accent" aria-hidden="true" />
                  <span className="text-[12px] font-semibold text-text-strong">{i18nT('apps.codeReviewSage.reviewFix.fixRun.title')}</span>
                  <span className="flex-1" />
                  <Badge variant={fixRunStatusVariant(fixRun.status)}>{fixRunStatusLabel(fixRun.status)}</Badge>
                </div>
                <ErrorNotice
                  message={fixRun.error}
                  askAgent
                  testId="local-review-fixrun-error"
                />
                {!!fixRun.changed_files?.length && (
                  <div className="flex flex-col gap-1 text-[11.5px] text-muted">
                    <span>{i18nT('apps.codeReviewSage.reviewFix.fixRun.changed_files')}</span>
                    <code className="break-words">{fixRun.changed_files.join(', ')}</code>
                  </div>
                )}
              </div>
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
                    onDisposition={(status, userInstruction) => disposition.mutate({ findingId: finding.id, status, userInstruction })}
                  />
                ))}
                {selected.size > 0 && (
                  <div className="sticky bottom-3 rounded-lg border border-accent bg-card p-3 shadow-lg">
                    <label className="block text-[12px] text-muted" htmlFor="local-review-instruction">
                      {i18nT('apps.codeReviewSage.components.localReview.human_instruction')}
                      <textarea id="local-review-instruction" aria-label={i18nT('apps.codeReviewSage.components.localReview.human_instruction')} value={instruction} onChange={(event) => setInstruction(event.target.value)} className={`${inputClass} mt-2 block w-full min-h-20`} />
                    </label>
                    <Btn type="button" primary onClick={() => fix.mutate()} disabled={fix.isPending} className="mt-2">
                      {fix.isPending && <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" aria-hidden="true" />}
                      {i18nT('apps.codeReviewSage.components.localReview.fix_selected')}
                    </Btn>
                    {/* No hand-off: the human-instruction textarea holds an unsaved
                        draft — navigating to chat unmounts the panel and discards it. */}
                    <ErrorNotice
                      variant="inline"
                      className="mt-2"
                      message={fix.error ? i18nT('apps.codeReviewSage.components.failureNotice.this_review_failed') : null}
                      testId="local-review-fix-error"
                    />
                  </div>
                )}
              </>
            )}
          </section>
        )}
      </div>
    </main>
  )
}
