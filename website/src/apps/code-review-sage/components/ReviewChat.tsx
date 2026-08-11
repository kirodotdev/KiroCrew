// Post-review chat: ask the reviewer about the findings it just produced.
//
// A report carries conclusions; "why did you decide that?" is answerable only by
// the session that decided it. The backend keeps that session alive for a bounded
// while (see `sage_lib/chat_session.py`), which is why this panel has two distinct
// states rather than one:
//
//   * LIVE   — the reviewer is still loaded and can be asked anything.
//   * CLOSED — it has been reclaimed. History is still shown, but there is no
//              composer, because an input that cannot send is worse than none.
//
// Collapsed by default: most reports are read without questions, and the findings
// are the substance of this tab.
//
// The reviewer answers in markdown (fenced code, `identifiers`, bold, numbered
// steps), so its turns render through the shared MarkdownRenderer. The user's own
// turns and the raw reasoning disclosure deliberately do NOT -- see Turn().
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, MessageCircle, Send, TriangleAlert } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import MarkdownRenderer from '../../../components/MarkdownRenderer'
import { i18nT } from '../../../i18n/t'
import { SageApiError, sageApi } from '../api'
import type { ChatTurn } from '../lib/types'

/** Codes the backend can return for a question, mapped to localized copy. An
 *  unmapped code falls back to a generic string rather than showing the server's
 *  English prose inside a translated page. */
const ASK_ERROR_KEY: Record<string, string> = {
  chat_expired: 'apps.codeReviewSage.components.reviewChat.error_expired',
  chat_busy: 'apps.codeReviewSage.components.reviewChat.error_busy',
  chat_message_too_long: 'apps.codeReviewSage.components.reviewChat.error_too_long',
  chat_message_required: 'apps.codeReviewSage.components.reviewChat.error_empty',
  chat_needs_override: 'apps.codeReviewSage.components.reviewChat.error_needs_override',
  chat_run_deleted: 'apps.codeReviewSage.components.reviewChat.error_run_deleted',
  chat_persist_failed: 'apps.codeReviewSage.components.reviewChat.error_persist_failed',
  chat_override_expiring: 'apps.codeReviewSage.components.reviewChat.error_override_expiring',
  chat_override_lapsed: 'apps.codeReviewSage.components.reviewChat.error_override_lapsed',
  chat_tool_denied: 'apps.codeReviewSage.components.reviewChat.error_tool_denied',
  chat_transcript_dir_unsafe: 'apps.codeReviewSage.components.reviewChat.error_transcript_unsafe',
}

/** While live, poll so a session closed by the idle sweep (or a question sent from
 *  another tab) stops offering a composer that would fail. */
const POLL_MS = 8000

function Reasoning({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  if (!text.trim()) return null
  return (
    <div className="mt-1">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        className="inline-flex items-center gap-1 text-[11px] text-muted hover:text-text cursor-pointer"
      >
        {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
        {i18nT('apps.codeReviewSage.components.reviewChat.reasoning')}
      </button>
      {open && (
        // Verbatim, unlike the answer above it: this is the model's raw
        // chain-of-thought, and the point of disclosing it is to show what it
        // actually emitted. Formatting it would make a working note look like a
        // considered statement.
        <div className="mt-1 whitespace-pre-wrap border-l border-border pl-2 text-[11.5px] leading-[1.6] text-muted">
          {text}
        </div>
      )}
    </div>
  )
}

function Turn({ turn }: { turn: ChatTurn }) {
  const mine = turn.role === 'user'
  return (
    <div className="flex flex-col gap-0.5">
      <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
        {mine
          ? i18nT('apps.codeReviewSage.components.reviewChat.role_you')
          : i18nT('apps.codeReviewSage.components.reviewChat.role_reviewer')}
      </div>
      <div className="text-[12.5px] leading-[1.65] text-text">
        {mine
          // The user's own words, echoed back verbatim. Running them through the
          // markdown renderer would reinterpret what they typed -- a question
          // about `**kwargs` or a *glob* pattern would come back bolded or
          // italicized, showing them something they did not write.
          ? <div className="whitespace-pre-wrap">{turn.text}</div>
          // Model-authored prose, and it answers in markdown: fenced code,
          // `identifiers`, bold, and numbered steps. Rendered flat, a real answer
          // is a wall of literal asterisks and backticks -- the same call
          // FindingCard makes for the review's own prose. MarkdownRenderer
          // sanitizes, so the model cannot inject markup through it.
          : <MarkdownRenderer content={turn.text} />}
      </div>
      {!mine && <Reasoning text={turn.thinking} />}
      {!mine && turn.tools.length > 0 && (
        <div className="mt-0.5 text-[11px] text-muted">
          {/* The list is interpolated INTO the sentence rather than concatenated
              after a "Looked at:" fragment: a key that ends mid-sentence leaves
              the rest outside the catalog, and a translator cannot reorder what
              they cannot see. */}
          {i18nT('apps.codeReviewSage.components.reviewChat.ran_tools',
            { tools: turn.tools.join(', ') })}
        </div>
      )}
      {!mine && turn.refusals.length > 0 && (
        // The answer is DEGRADED, not merely annotated: it wanted to look at
        // something and could not. Saying so is the difference between a wrong
        // answer and a bounded one.
        <div className="mt-1 inline-flex items-start gap-1.5 text-[11.5px] text-danger">
          <TriangleAlert size={12} className="mt-0.5 flex-shrink-0" />
          <span>
            {i18nT('apps.codeReviewSage.components.reviewChat.refused_notice')}
          </span>
        </div>
      )}
    </div>
  )
}

export default function ReviewChat(
  { runId, changeId }: { runId: string; changeId: string },
) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [askError, setAskError] = useState('')
  const qc = useQueryClient()
  const endRef = useRef<HTMLDivElement | null>(null)

  const stateQ = useQuery({
    queryKey: ['sage', 'chat', runId, changeId],
    queryFn: () => sageApi.chatState(runId, changeId),
    enabled: open && Boolean(runId) && Boolean(changeId),
    refetchInterval: (q) => (q.state.data?.live ? POLL_MS : false),
  })

  // Preserve the last good payload across a polling blip: react-query keeps stale
  // data when status flips to error, so gating on `isError` alone would eject the
  // transcript the user is reading.
  const state = stateQ.data
  const turns = state?.turns ?? []
  const live = Boolean(state?.live)
  const busy = Boolean(state?.busy)
  // A live session is not sufficient: without the safety override the turn is
  // refused before it is ever sent, because an agent spec's allowedTools
  // pre-approves tools that would then run with no permission event. Telling the
  // user only after they have typed is the failure mode this avoids.
  const canAsk = live && Boolean(state?.can_ask)

  const ask = useMutation({
    mutationFn: (message: string) => sageApi.chatAsk(runId, changeId, message),
    onSuccess: () => {
      setDraft('')
      setAskError('')
      void qc.invalidateQueries({ queryKey: ['sage', 'chat', runId, changeId] })
    },
    onError: (e: unknown) => {
      // Some codes arrive with a reason appended (`chat_tool_denied: <why>`), so
      // match on the code itself. The reason is deliberately NOT rendered: it can
      // embed the model-authored tool title, and the localized message already
      // says what the user can do about it. It stays in the server log.
      const raw = e instanceof SageApiError ? e.code : ''
      const code = raw.split(':', 1)[0].trim()
      const key = ASK_ERROR_KEY[code]
      setAskError(key
        ? i18nT(key)
        : i18nT('apps.codeReviewSage.components.reviewChat.error_generic'))
      // A failed question may mean the session died; re-read so the composer
      // reflects reality instead of inviting a second doomed attempt.
      void qc.invalidateQueries({ queryKey: ['sage', 'chat', runId, changeId] })
    },
  })

  const close = useMutation({
    mutationFn: () => sageApi.chatClose(runId, changeId),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['sage', 'chat', runId, changeId] })
    },
  })

  useEffect(() => {
    if (open) endRef.current?.scrollIntoView({ block: 'nearest' })
  }, [open, turns.length])

  const canSend = canAsk && !busy && !ask.isPending && draft.trim().length > 0

  function submit() {
    if (!canSend) return
    ask.mutate(draft.trim())
  }

  return (
    <div className="rounded-md border border-border">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-2.5 py-2 text-[12.5px] text-text hover:bg-bg-elevated cursor-pointer"
      >
        {open ? <ChevronDown size={13} className="flex-shrink-0 text-muted" />
          : <ChevronRight size={13} className="flex-shrink-0 text-muted" />}
        <MessageCircle size={13} className="flex-shrink-0 text-muted" />
        <span className="min-w-0 flex-1 truncate text-left">
          {i18nT('apps.codeReviewSage.components.reviewChat.title')}
        </span>
        {turns.length > 0 && (
          <span className="flex-shrink-0 tabular-nums text-[11px] text-muted">
            {turns.length}
          </span>
        )}
      </button>

      {open && (
        <div className="flex flex-col gap-3 border-t border-border px-2.5 py-2.5">
          {turns.length === 0 && !stateQ.isLoading && (
            <div className="text-[12px] leading-[1.6] text-muted">
              {i18nT('apps.codeReviewSage.components.reviewChat.empty_hint')}
            </div>
          )}
          {turns.length > 0 && (
            <div className="flex flex-col gap-3">
              {turns.map((t, i) => (
                <Turn key={`${t.ts}-${i}`} turn={t} />
              ))}
              <div ref={endRef} />
            </div>
          )}

          {canAsk ? (
            <div className="flex flex-col gap-1.5">
              <textarea
                value={draft}
                onChange={e => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    submit()
                  }
                }}
                rows={2}
                disabled={busy || ask.isPending}
                placeholder={i18nT('apps.codeReviewSage.components.reviewChat.placeholder')}
                aria-label={i18nT('apps.codeReviewSage.components.reviewChat.placeholder')}
                className="w-full resize-y rounded-md border border-border bg-bg px-2 py-1.5 text-[12.5px] text-text placeholder:text-muted disabled:opacity-60"
              />
              <div className="flex items-center justify-between gap-2">
                <button
                  type="button"
                  onClick={() => close.mutate()}
                  disabled={close.isPending}
                  className="text-[11.5px] text-muted hover:text-text disabled:opacity-60 cursor-pointer"
                >
                  {i18nT('apps.codeReviewSage.components.reviewChat.end_chat')}
                </button>
                <button
                  type="button"
                  onClick={submit}
                  disabled={!canSend}
                  className="inline-flex items-center gap-1.5 rounded-md border border-accent bg-accent-subtle px-2.5 py-1 text-[12.5px] font-medium text-accent hover:bg-accent/20 disabled:opacity-50 cursor-pointer"
                >
                  <Send size={12} />
                  {busy || ask.isPending
                    ? i18nT('apps.codeReviewSage.components.reviewChat.sending')
                    : i18nT('apps.codeReviewSage.components.reviewChat.send')}
                </button>
              </div>
            </div>
          ) : (
            // No composer once the session is gone. The explainer says what
            // happened and that the history above is intact, so a returning user
            // is not left wondering whether their conversation was lost.
            !stateQ.isLoading && (
              // Ruled off from the transcript on purpose: rendered flush under the
              // last answer in the same muted style, this notice reads as the
              // reviewer still talking rather than as the app explaining that it
              // has gone.
              <div className="flex flex-col gap-2 border-t border-border pt-2.5">
                {draft.trim() && (
                  // The session can lapse BETWEEN keystrokes — the poll flips
                  // `live` while the user is mid-sentence. Unmounting the composer
                  // then would take the half-written question with it, so the text
                  // is handed back instead of destroyed. Read-only on purpose: it
                  // is no longer an input, and offering one that cannot send is the
                  // thing this branch exists to avoid.
                  <div className="flex flex-col gap-1">
                    <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
                      {i18nT('apps.codeReviewSage.components.reviewChat.unsent_label')}
                    </div>
                    <textarea
                      value={draft}
                      readOnly
                      rows={2}
                      aria-label={i18nT('apps.codeReviewSage.components.reviewChat.unsent_label')}
                      className="w-full resize-y rounded-md border border-border bg-bg-elevated px-2 py-1.5 text-[12.5px] text-text"
                    />
                  </div>
                )}
                <div className="text-[12px] leading-[1.6] text-muted">
                  {live
                    // Still loaded, but tool use cannot be gated — a different
                    // situation from "it is gone", and a different remedy.
                    ? i18nT('apps.codeReviewSage.components.reviewChat.needs_override_notice')
                    : i18nT('apps.codeReviewSage.components.reviewChat.closed_notice')}
                </div>
              </div>
            )
          )}

          {askError && (
            <div className="text-[11.5px] text-danger">{askError}</div>
          )}
        </div>
      )}
    </div>
  )
}
