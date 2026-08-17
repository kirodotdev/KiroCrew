// The gate between "the meeting ended" and "the meeting is closed".
//
// Every extracted action item gets a decision: file it through the configured
// task provider, or archive it as noise. The meeting cannot be closed while
// anything is still pending — that is the whole point of the step, and it is why
// upstream called this view the task review.

import { Archive, ArchiveRestore, CheckCheck, CircleCheck, ExternalLink, Send } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { Badge, Btn, Card, CardTitle, EmptyState, SendBtn, StatCard } from '../../components/ui'
import { PRIORITY_LABEL_KEY, type Task, type TranscriptSegment } from './api'
import TranscriptPanel from './components/TranscriptPanel'

/**
 * Whether a filed-task reference URL is safe to put in an `href`.
 *
 * `filed_ref` comes out of `tasks.json`, which an agent writes — so the URL is
 * model-supplied, and a `javascript:` value would execute on the dashboard
 * origin when the user clicks the link (React 18 only warns; the dashboard CSP
 * allows inline script). Only absolute http(s) is rendered as a link; anything
 * else falls back to the plain-text id below.
 */
function linkableUrl(ref: Task['filed_ref']): string | null {
  const url = ref?.url
  return url && /^https?:\/\//i.test(url) ? url : null
}

interface Props {
  tasks: Task[]
  transcript: TranscriptSegment[]
  partialTranscript: string
  transcriptFull: boolean
  provider: string
  filing: string | null
  onBack: () => void
  onClose: () => void
  onFile: (taskId: string) => void
  onArchive: (taskId: string) => void
  onUnarchive: (taskId: string) => void
}

export default function TaskReviewView({
  tasks,
  transcript,
  partialTranscript,
  transcriptFull,
  provider,
  filing,
  onBack,
  onClose,
  onFile,
  onArchive,
  onUnarchive,
}: Props) {
  const pending = tasks.filter(task => task.review_status === 'pending')
  const archived = tasks.filter(task => task.review_status === 'archived')
  const filed = tasks.filter(task => task.review_status === 'pushed')
  const canClose = pending.length === 0

  return (
    <div className="flex flex-col lg:flex-row h-full overflow-hidden">
      <div className="flex flex-col flex-1 min-w-0 min-h-0 overflow-hidden">
        <div className="flex-none px-4 md:px-6 py-4 border-b border-border flex items-center gap-3">
          <Btn onClick={onBack}>{i18nT('apps.meetings.review.backToMeeting')}</Btn>
          <h2 className="text-lg font-semibold text-text-strong">
            {i18nT('apps.meetings.review.title')}
          </h2>
          <Badge variant={canClose ? 'ok' : 'warn'}>
            {canClose
              ? i18nT('apps.meetings.review.allDone')
              : i18nT('apps.meetings.review.remaining', { count: pending.length })}
          </Badge>
          {pending.length > 0 && (
            <Btn
              className="ml-auto"
              onClick={() => pending.forEach(task => onArchive(task.id))}
            >
              <Archive className="lucide-inline" />
              {i18nT('apps.meetings.review.archiveAll')}
            </Btn>
          )}
        </div>

        <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
          <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] my-6">
            <StatCard label={i18nT('apps.meetings.review.statPending')} value={pending.length} accent />
            <StatCard label={i18nT('apps.meetings.review.statFiled')} value={filed.length} />
            <StatCard label={i18nT('apps.meetings.review.statArchived')} value={archived.length} />
          </div>

          <Card>
            <CardTitle>{i18nT('apps.meetings.review.pendingSection')}</CardTitle>
            {pending.length === 0 ? (
              <EmptyState
                icon={<CircleCheck className="lucide-inline" />}
                title={i18nT('apps.meetings.review.nothingPending')}
                subtitle={i18nT('apps.meetings.review.nothingPendingHint')}
              />
            ) : (
              <div className="flex flex-col gap-2 mt-2">
                {pending.map(task => (
                  <div
                    key={task.id}
                    className="flex items-center gap-3 px-4 py-3 border border-border rounded-md"
                  >
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-text font-medium break-words">{task.description}</p>
                      <div className="flex items-center gap-2 mt-1">
                        {task.assignee && (
                          <span className="text-[12px] text-muted">
                            {i18nT('apps.meetings.review.assignedTo', { name: task.assignee })}
                          </span>
                        )}
                        <Badge variant={task.priority === 'high' ? 'err' : 'muted'}>
                          {i18nT(PRIORITY_LABEL_KEY[task.priority])}
                        </Badge>
                      </div>
                    </div>
                    <Btn
                      onClick={() => onArchive(task.id)}
                      disabled={filing === task.id}
                      aria-label={i18nT('apps.meetings.review.archive')}
                    >
                      <Archive className="lucide-inline" />
                      {i18nT('apps.meetings.review.archive')}
                    </Btn>
                    <SendBtn
                      onClick={() => onFile(task.id)}
                      disabled={filing === task.id}
                      aria-label={i18nT('apps.meetings.review.file')}
                    >
                      <Send className="lucide-inline" />
                      {filing === task.id
                        ? i18nT('apps.meetings.review.filing')
                        : i18nT('apps.meetings.review.file')}
                    </SendBtn>
                  </div>
                ))}
              </div>
            )}
          </Card>

          {filed.length > 0 && (
            <Card className="mt-4">
              <CardTitle>
                {i18nT('apps.meetings.review.filedSection', { provider })}
              </CardTitle>
              <div className="flex flex-col gap-2 mt-2">
                {filed.map(task => {
                  const href = linkableUrl(task.filed_ref)
                  return (
                    <div
                      key={task.id}
                      className="flex items-center gap-3 px-4 py-2.5 border border-ok/30 rounded-md bg-ok-subtle"
                    >
                      <CircleCheck className="lucide-inline text-ok" />
                      <p className="flex-1 min-w-0 text-[13px] text-text truncate">
                        {task.description}
                      </p>
                      {href ? (
                        <a
                          href={href}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-accent hover:underline inline-flex items-center gap-1 text-[12px]"
                        >
                          <ExternalLink className="lucide-inline" />
                          {task.filed_ref?.id}
                        </a>
                      ) : (
                        task.filed_ref?.id && (
                          <span className="text-[12px] text-muted font-mono">
                            {task.filed_ref.id}
                          </span>
                        )
                      )}
                    </div>
                  )
                })}
              </div>
            </Card>
          )}

          {archived.length > 0 && (
            <Card className="mt-4">
              <CardTitle>{i18nT('apps.meetings.review.archivedSection')}</CardTitle>
              <div className="flex flex-col gap-2 mt-2">
                {archived.map(task => (
                  <div
                    key={task.id}
                    className="flex items-center gap-3 px-4 py-2.5 border border-border rounded-md opacity-60"
                  >
                    <p className="flex-1 min-w-0 text-[13px] text-muted line-through truncate">
                      {task.description}
                    </p>
                    <Btn
                      onClick={() => onUnarchive(task.id)}
                      aria-label={i18nT('apps.meetings.review.unarchive')}
                    >
                      <ArchiveRestore className="lucide-inline" />
                      {i18nT('apps.meetings.review.unarchive')}
                    </Btn>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>

        <div className="flex-none px-4 md:px-6 py-4 border-t border-border flex justify-center">
          <SendBtn
            onClick={onClose}
            disabled={!canClose}
            aria-label={i18nT('apps.meetings.review.closeMeeting')}
          >
            <CheckCheck className="lucide-inline" />
            {canClose
              ? i18nT('apps.meetings.review.closeMeeting')
              : i18nT('apps.meetings.review.closeBlocked', { count: pending.length })}
          </SendBtn>
        </div>
      </div>

      <div className="flex-none h-[38%] min-h-[260px] p-4 pt-0 lg:h-full lg:w-[360px] lg:pl-0 lg:pt-4">
        <TranscriptPanel
          segments={transcript}
          partial={partialTranscript}
          status="reviewing"
          full={transcriptFull}
        />
      </div>
    </div>
  )
}
