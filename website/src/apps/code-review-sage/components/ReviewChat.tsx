// Follow-up on a review: open a chat session that RESUMES the reviewer.
//
// A report carries conclusions; "why did you decide that?" is answerable only by
// the session that decided it. That session's transcript is kept, so a follow-up
// loads it from disk and continues as an ordinary chat session — which is why this
// panel has no composer of its own. Everything after the button is the Chat tab:
// history that survives restarts, real approval prompts, steering, subagents.
//
// Two states, and the difference matters:
//
//   * RESUMABLE — the reviewer's session is on disk and can be reopened.
//   * NOT RESUMABLE — nothing was kept, or the transcript is gone. No button,
//     because a session opened anyway would answer confidently with no idea what
//     was reviewed.
//
// Collapsed by default: most reports are read without questions, and the findings
// are the substance of this tab.
import { useMutation, useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, MessageCircle, TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '../../../api/client'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import { i18nT } from '../../../i18n/t'
import { SageApiError, sageApi } from '../api'
import type { ChatTurn } from '../lib/types'

/** Codes the backend can return, mapped to localized copy. An unmapped code falls
 *  back to a generic string rather than showing the server's English prose inside
 *  a translated page. */
const REASON_KEY: Record<string, string> = {
  followup_not_recorded: 'apps.codeReviewSage.components.reviewChat.not_recorded_notice',
  followup_transcript_gone: 'apps.codeReviewSage.components.reviewChat.transcript_gone_notice',
  followup_run_live: 'apps.codeReviewSage.components.reviewChat.run_live_notice',
  chat_run_deleted: 'apps.codeReviewSage.components.reviewChat.error_run_deleted',
}

function reasonText(code: string): string {
  const key = REASON_KEY[code]
  return key
    ? i18nT(key)
    : i18nT('apps.codeReviewSage.components.reviewChat.error_generic')
}

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
  const [error, setError] = useState('')
  const navigate = useNavigate()

  const stateQ = useQuery({
    queryKey: ['sage', 'chat', runId, changeId],
    queryFn: () => sageApi.chatState(runId, changeId),
    enabled: open && Boolean(runId) && Boolean(changeId),
  })

  const state = stateQ.data
  const turns = state?.turns ?? []
  const resumable = Boolean(state?.resumable)
  // An existing conversation changes the offer from "start one" to "go back to
  // it": the exchanges live in the Chat tab, not here, so a panel that says
  // "open" reads as though they were lost.
  const alreadyOpen = Boolean(state?.followup_open)

  const start = useMutation({
    mutationFn: async () => {
      // Two calls, deliberately. The app arms the resume and hands back what the
      // slot needs; the slot itself is created through the dashboard's own
      // endpoint so agent binding, workspace resolution and title redaction have
      // one implementation rather than a copy in this app.
      const prep = await sageApi.followupStart(runId, changeId)
      // Title and folder are sent only when the session is being CREATED. That
      // endpoint addresses an existing slot by name and then re-pins whatever it
      // is given, so sending them again on a continue would revert a session the
      // user has since renamed or moved into a folder of their own.
      const slot = await api.createChatSlot(
        prep.slot_key, prep.agent, undefined, undefined, undefined,
        alreadyOpen ? undefined : prep.title, undefined, undefined,
        alreadyOpen ? undefined : (prep.folder_id || undefined),
      )
      return slot.key || prep.slot_key
    },
    onSuccess: (slotKey: string) => {
      setError('')
      navigate('/chat?' + new URLSearchParams({ sid: slotKey }).toString())
    },
    onError: (e: unknown) => {
      // Some codes arrive with a reason appended, so match on the code itself.
      const raw = e instanceof SageApiError ? e.code : ''
      setError(reasonText(raw.split(':', 1)[0].trim()))
      void stateQ.refetch()
    },
  })

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
          {stateQ.isLoading && (
            // Without this the expanded panel is an empty bordered strip, which
            // reads as "there is nothing here" rather than "still reading".
            <div className="text-[12px] leading-[1.6] text-muted">
              {i18nT('apps.codeReviewSage.components.reviewChat.loading')}
            </div>
          )}
          {turns.length > 0 && (
            // Exchanges from before follow-ups became sessions. Labelled rather
            // than shown flush under the button, so they do not read as part of
            // the session the button opens.
            <div className="flex flex-col gap-3">
              <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
                {i18nT('apps.codeReviewSage.components.reviewChat.earlier_questions')}
              </div>
              {turns.map((t, i) => (
                <Turn key={`${t.ts}-${i}`} turn={t} />
              ))}
            </div>
          )}

          {!stateQ.isLoading && (
            // Ruled off from the stored history when there is any: rendered flush
            // under the last answer in the same muted style, this block reads as
            // the reviewer still talking rather than as the app offering to
            // reopen it.
            <div className={turns.length > 0
              ? 'flex flex-col gap-2 border-t border-border pt-2.5'
              : 'flex flex-col gap-2'}
            >
              {resumable ? (
                <>
                  <div className="text-[12px] leading-[1.6] text-muted">
                    {alreadyOpen
                      ? i18nT('apps.codeReviewSage.components.reviewChat.continue_hint')
                      : i18nT('apps.codeReviewSage.components.reviewChat.resume_hint')}
                  </div>
                  <button
                    type="button"
                    onClick={() => start.mutate()}
                    disabled={start.isPending}
                    className="inline-flex w-fit items-center gap-1.5 rounded-md border border-accent bg-accent-subtle px-2.5 py-1 text-[12.5px] font-medium text-accent hover:bg-accent/20 disabled:opacity-50 cursor-pointer"
                  >
                    <MessageCircle size={12} />
                    {start.isPending
                      ? i18nT('apps.codeReviewSage.components.reviewChat.opening')
                      : alreadyOpen
                        ? i18nT('apps.codeReviewSage.components.reviewChat.continue_session')
                        : i18nT('apps.codeReviewSage.components.reviewChat.open_session')}
                  </button>
                </>
              ) : (
                // No button once there is nothing to resume. The explainer says
                // what happened and what to do about it, so a returning user is
                // not left wondering whether the review lost their conversation.
                <div className="text-[12px] leading-[1.6] text-muted">
                  {reasonText(state?.reason || '')}
                </div>
              )}
            </div>
          )}

          {error && (
            <div className="text-[11.5px] text-danger">{error}</div>
          )}
        </div>
      )}
    </div>
  )
}
