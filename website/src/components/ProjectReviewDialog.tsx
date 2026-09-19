import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, ShieldCheck, X } from 'lucide-react'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import type { ProjectBundle, ProjectReviewFile, ProjectReviewPreview } from '../types'
import { copyToClipboard } from '../utils/clipboard'
import { parseErrorCode, type ErrorReport } from '../utils/errorReport'
import ErrorNotice from './ErrorNotice'
import Modal from './Modal'
import { blockedReviewDetail } from './ProjectReviewDialog.prompt'
import { Badge, Btn, ContentSkeleton, SendBtn } from './ui'

const PROJECTS_QUERY_KEY = ['project-bundles'] as const

/**
 * True for the one rejection the dialog recovers from itself: the files
 * changed between the preview and the accept, so the digest the owner saw no
 * longer matches. Duck-typed on `status` + the body's `code` (like
 * `isNotFoundError`) so it holds under a mocked `api/client`, and so a request
 * that FAILED for any other reason still reaches the error notice.
 */
export function isReviewMovedError(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false
  const { status, body } = error as { status?: unknown; body?: unknown }
  return status === 409 && typeof body === 'string' && parseErrorCode(body) === 'project_review_moved'
}

const STATUS_VARIANT: Record<ProjectReviewFile['status'], 'ok' | 'warn' | 'err' | 'muted'> = {
  added: 'ok',
  changed: 'warn',
  removed: 'muted',
  unreadable: 'err',
}

function statusLabel(status: ProjectReviewFile['status']): string {
  if (status === 'added') return i18nT('components.projectReviewDialog.status_added')
  if (status === 'changed') return i18nT('components.projectReviewDialog.status_changed')
  if (status === 'removed') return i18nT('components.projectReviewDialog.status_removed')
  return i18nT('components.projectReviewDialog.status_unreadable')
}

/** Why an unreadable entry shows no content and can never be accepted. An
 *  unknown reason is still named, verbatim: the server saw something this
 *  client does not know how to describe, which is not a reason to hide it. */
function unreadableHelp(file: ProjectReviewFile): string {
  switch (file.reason) {
    case 'link-outside-root': return i18nT('components.projectReviewDialog.unreadable_link_outside_root')
    case 'too-large': return i18nT('components.projectReviewDialog.unreadable_too_large')
    case 'binary': return i18nT('components.projectReviewDialog.unreadable_binary')
    case 'overflow': return i18nT('components.projectReviewDialog.unreadable_overflow')
    // The redactor changed what the dialog would display, so the bytes on
    // screen would not be the bytes accepted: the file is reviewed elsewhere.
    case 'redacted': return i18nT('components.projectReviewDialog.unreadable_redacted')
    case 'error': return i18nT('components.projectReviewDialog.unreadable_error')
    default: return file.reason
      ? i18nT('components.projectReviewDialog.unreadable_other', { reason: file.reason })
      : i18nT('components.projectReviewDialog.unreadable_error')
  }
}

type CopyState = 'idle' | 'copied' | 'failed'

/** Copies a checkout-relative path so the fix instruction beside it can be
 *  carried to a terminal or editor. The icon is always visible — the button
 *  is the affordance, not a hover reveal — and the label says which state it
 *  is in, so the outcome is announced as well as drawn. A tick is shown only
 *  when the text actually reached the clipboard (`copyToClipboard` reports
 *  that), never over an unchanged clipboard. The button's own state is the
 *  1.5s confirmation; the durable record of a FAILED copy is the caller's,
 *  through `onResult`, so the failure can stand as an error notice beside
 *  the path instead of vanishing with the icon. */
function CopyPathButton({ path, onResult }: { path: string; onResult: (ok: boolean) => void }) {
  const [state, setState] = useState<CopyState>('idle')
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (resetTimer.current) clearTimeout(resetTimer.current) }, [])
  const label = state === 'copied'
    ? i18nT('components.projectReviewDialog.path_copied')
    : state === 'failed'
      ? i18nT('components.projectReviewDialog.copy_path_failed')
      : i18nT('components.projectReviewDialog.copy_path')
  return (
    <button
      type="button"
      className="inline-flex shrink-0 cursor-pointer items-center rounded border border-border bg-bg-elevated p-1 text-muted hover:bg-bg-hover hover:text-text"
      aria-label={label}
      aria-live="polite"
      title={label}
      onClick={async () => {
        const ok = await copyToClipboard(path)
        setState(ok ? 'copied' : 'failed')
        onResult(ok)
        if (resetTimer.current) clearTimeout(resetTimer.current)
        resetTimer.current = setTimeout(() => setState('idle'), 1500)
      }}
    >
      {state === 'copied'
        ? <Check size={13} className="text-ok" aria-hidden="true" />
        : state === 'failed'
          ? <X size={13} className="text-danger" aria-hidden="true" />
          : <Copy size={13} aria-hidden="true" />}
    </button>
  )
}

function ReviewFile({ file }: { file: ProjectReviewFile }) {
  const blocked = file.status === 'unreadable'
  // A copy that did not reach the clipboard, kept until the owner clears it
  // or a later copy succeeds. The button's own failed state resets after a
  // moment; this is what stays.
  const [copyFailed, setCopyFailed] = useState(false)
  return (
    <li className="rounded-md border border-border bg-bg-elevated" data-testid="project-review-file">
      <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
        <span className="min-w-0 break-all font-mono text-[13px] text-text">{file.path}</span>
        <Badge variant={STATUS_VARIANT[file.status]}>{statusLabel(file.status)}</Badge>
      </div>
      {typeof file.content === 'string' ? (
        /* Read-only by construction: a <pre> holds the WHOLE bytes being
           accepted, exactly as the server read them, with nothing to edit. It
           scrolls past max-h, so it is a labelled region with a tab stop — a
           scrollable region must be keyboard focusable (axe
           scrollable-region-focusable), the same shape as CodeBlock. */
        /* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */
        <pre aria-label={i18nT('components.projectReviewDialog.content_of', { path: file.path })} className="m-0 max-h-72 overflow-auto border-t border-border px-3 py-2 font-mono text-[12px] leading-5 text-text whitespace-pre-wrap break-all" role="region" tabIndex={0}>
          {file.content}
        </pre>
      ) : blocked ? (
        <div className="border-t border-border px-3 py-2 text-[12px]">
          <p className="m-0 text-danger">{unreadableHelp(file)}</p>
          {/* The path to fix, on its own line with a copy affordance: every
              remedy above names a file to replace, so the row hands over the
              exact path rather than leaving the owner to retype it. */}
          <div className="mt-1.5 flex items-center gap-1.5" data-testid="project-review-fix-path">
            <code className="min-w-0 break-all font-mono text-[12px] text-text">{file.path}</code>
            <CopyPathButton path={file.path} onResult={ok => setCopyFailed(!ok)} />
          </div>
          {/* A failed copy is an error, rendered as one: the shared notice,
              directly under the path it concerns, so the entry above stays
              readable. Hand-off decision: NO agent hand-off (`askAgent` off).
              The remedy is already in the notice's own frame — the path is
              printed one line up, selectable, so "select it and copy it by
              hand" completes the task the button failed at. Nothing here is
              the agent's to fix: the clipboard is refused by the browser or
              the page's origin, not by the Project, and the hand-off would
              navigate away from the review the owner is in the middle of
              reading. Dismissable, and cleared by the next copy that lands. */}
          <ErrorNotice
            className="mt-1.5"
            message={copyFailed ? i18nT('components.projectReviewDialog.copy_path_failed_notice') : null}
            onDismiss={() => setCopyFailed(false)}
            testId="project-review-copy-failed"
          />
        </div>
      ) : (
        <div className="border-t border-border px-3 py-2 text-[12px] text-muted">
          {i18nT('components.projectReviewDialog.removed_help')}
        </div>
      )}
    </li>
  )
}

/**
 * The digest-bound review: what the owner accepts is exactly what they were
 * shown. The preview (`GET …/review`) carries every file awaiting review with
 * its content and a digest of that set; "Accept these changes" posts that
 * digest back, and the server refuses with 409 `project_review_moved` when the
 * files changed since — the dialog then re-fetches and says so, instead of
 * recording a review of bytes nobody read.
 *
 * Entries the server could not read (a symlink out of the tree, an oversized
 * file, a file that is not text, a `.kiro/` tree over the file cap) can never
 * be accepted: they stay stale after any review, so the accept button is
 * withheld while one is listed and the entry says what to fix, with the path
 * to fix ready to copy. Every readable entry shows its whole content — the
 * digest covers no byte the owner was not shown.
 */
export default function ProjectReviewDialog({ project, open, onClose }: {
  project: ProjectBundle
  open: boolean
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [moved, setMoved] = useState(false)
  const previewQuery = useQuery<ProjectReviewPreview>({
    queryKey: [...PROJECTS_QUERY_KEY, project.id, 'review'],
    queryFn: () => api.projectBundleReviewPreview(project.id),
    enabled: open,
    // A cached preview is a stale digest waiting to be refused: re-read on
    // every open so the owner reads the current files.
    staleTime: 0,
    gcTime: 0,
  })
  const acceptMutation = useMutation({
    mutationFn: (digest: string) => api.reviewProjectBundle(project.id, digest),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY })
      close()
    },
    onError: async error => {
      if (!isReviewMovedError(error)) return
      setMoved(true)
      await previewQuery.refetch()
    },
  })
  // Every way out clears the dialog's own state: the moved notice belongs to
  // the preview it was raised against and a rejection should not greet the
  // next open, which re-reads the files anyway.
  function close() {
    setMoved(false)
    acceptMutation.reset()
    onClose()
  }

  const preview = previewQuery.data
  const files = preview?.files ?? []
  const firstReview = files.length > 0 && files.every(file => file.status === 'added')
  const blocked = files.filter(file => file.status === 'unreadable')
  const canAccept = Boolean(preview) && files.length > 0 && blocked.length === 0
    && !previewQuery.isFetching && !acceptMutation.isPending
  const blockedMessage = blocked.length > 0
    ? i18nT('components.projectReviewDialog.blocked', { count: blocked.length })
    : null
  // The withheld accept is a dead end the owner cannot clear from here, so it
  // carries the same hand-off the page's notices do. A structured report, not
  // a journal lookup: no request failed, the server named the unreadable
  // entries inside a successful preview, and the agent needs exactly those
  // paths and reasons — the fenced data block carries them, the translated
  // lead carries the ask.
  const blockedReport: ErrorReport | undefined = blockedMessage ? {
    id: `project-review-blocked-${project.id}`,
    at: Date.now(),
    source: 'system',
    route: '/project-bundles',
    code: 'project_review_unreviewable',
    message: blockedMessage,
    detail: blockedReviewDetail(project.name, blocked),
  } : undefined

  return (
    <Modal
      open={open}
      onClose={close}
      title={firstReview
        ? i18nT('components.projectReviewDialog.title_first', { name: project.name })
        : i18nT('components.projectReviewDialog.title_changed', { name: project.name })}
      maxWidth={760}
      dismissDisabled={acceptMutation.isPending}
      footer={
        <>
          <Btn disabled={acceptMutation.isPending} onClick={close}>
            {i18nT('components.projectReviewDialog.cancel')}
          </Btn>
          <SendBtn
            className="inline-flex items-center gap-1.5"
            disabled={!canAccept}
            onClick={() => { if (preview) acceptMutation.mutate(preview.digest) }}
          >
            <ShieldCheck className="lucide-inline" />
            {i18nT('components.projectReviewDialog.accept')}
          </SendBtn>
        </>
      }
    >
      <p className="m-0 mb-3 text-sm text-text">
        {firstReview
          ? i18nT('components.projectReviewDialog.intro_first')
          : i18nT('components.projectReviewDialog.intro_changed')}
      </p>
      {/* The files changed again while the owner was reading: a rejected
          request, rendered as one, above the re-read preview it applies to. */}
      {moved && (
        <ErrorNotice className="mb-3" message={i18nT('components.projectReviewDialog.moved')} askAgent />
      )}
      {previewQuery.isLoading ? (
        <ContentSkeleton rows={4} />
      ) : previewQuery.error ? (
        <ErrorNotice
          message={previewQuery.error instanceof Error && previewQuery.error.message
            ? previewQuery.error.message
            : i18nT('components.projectReviewDialog.preview_failed')}
          askAgent
        />
      ) : files.length === 0 ? (
        <p className="m-0 text-[13px] text-muted">{i18nT('components.projectReviewDialog.nothing_to_review')}</p>
      ) : (
        <ul className="m-0 list-none space-y-3 p-0">
          {files.map(file => <ReviewFile file={file} key={`${file.status}:${file.path}`} />)}
        </ul>
      )}
      {/* The withheld accept, stated as the blocker it is, with the hand-off
          beside it: fixing a link, a binary or an oversized file is work the
          agent can do in the Project, so a disabled button is never the end. */}
      <ErrorNotice
        className="mt-3"
        message={blockedMessage}
        report={blockedReport}
        askAgent
        onHandoff={close}
        testId="project-review-blocked"
      />
      <ErrorNotice
        className="mt-3"
        message={acceptMutation.error && !isReviewMovedError(acceptMutation.error)
          ? (acceptMutation.error instanceof Error && acceptMutation.error.message
            ? acceptMutation.error.message
            : i18nT('components.projectReviewDialog.accept_failed'))
          : null}
        askAgent
      />
    </Modal>
  )
}
