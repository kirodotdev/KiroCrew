import { memo, useEffect, useId, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, GitBranch, Lightbulb, MessageSquarePlus, Plus, X } from 'lucide-react'
import type { FollowupItem } from '../store/chatSlice'
import ErrorNotice from './ErrorNotice'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
export interface FollowUpCardProps {
  items: FollowupItem[]
  /**
   * Pre-fill THIS session's composer with the item's expanded prompt, and drop
   * that one item from the card. `index` is the item's position in `items`.
   */
  onAddToSession: (item: FollowupItem, index: number) => void
  /**
   * Create a git worktree, open a session scoped to it, and send the item's
   * prompt there as its first message. Rejects with a user-facing message on
   * failure (branch exists, not a git repo, git unavailable, send refused)
   * which the card renders inline. Once the prompt is sent only that one item
   * leaves the card; its siblings stay.
   */
  onStartInWorktree: (item: FollowupItem, index: number) => Promise<void>
  /**
   * Open a new session in this session's project directory (where a new chat
   * starts by default when it has none) and send the item's prompt there as its
   * first message, with no git involved. The primary action when there is no
   * repository to branch from.
   * Rejects with a user-facing message on failure, rendered inline like the
   * worktree route's. Once the prompt is sent only that one item leaves the card.
   */
  onStartInNewChat: (item: FollowupItem, index: number) => Promise<void>
  /** Drop this single suggestion; siblings stay. */
  onSkip: (index: number) => void
  /**
   * Absent when the active session has no project directory. The primary
   * action is then a plain new chat — there is no repo to branch from.
   */
  projectDir?: string
  /**
   * Result of the project's git probe: `false` when the project directory is
   * known NOT to be a git repo, so a worktree cannot be branched from it and
   * the primary action is a plain new chat instead. `undefined` while the
   * probe is pending or failed: the worktree action stays offered and the
   * server's own repo check remains the backstop.
   */
  projectIsRepo?: boolean
  /**
   * The root of the git repository the project directory sits INSIDE, when the
   * directory is not that root itself (a plain folder under a repo checked out
   * higher up). A worktree would branch the whole enclosing repository, which
   * the server refuses unless another session is scoped to that root, so the
   * card treats it like no repository: the primary action is a plain new chat.
   */
  enclosingRepoRoot?: string
}

/**
 * Agent-authored follow-up suggestions, rendered above the composer.
 *
 * Start (new worktree or new chat) SENDS the item's prompt as the new
 * session's first message, so one click runs agent-authored text. That is why
 * every item can show its full prompt before the click: the title and
 * description are a label the agent chose, and only the prompt is what runs.
 * "Add to this session" PRE-FILLS this composer and sends nothing. The
 * `suggest_followup` tool description (control_parts/schemas.py) states the
 * same contract to the model.
 *
 * All item strings are LLM-authored. They are rendered as text children only
 * (never dangerouslySetInnerHTML), on top of the server-side sanitization and
 * credential/URL redaction in `_redact_followup_item`.
 */
function FollowUpCard({
  items,
  onAddToSession,
  onStartInWorktree,
  onStartInNewChat,
  onSkip,
  projectDir,
  projectIsRepo,
  enclosingRepoRoot,
}: FollowUpCardProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // Index of the item whose session is being opened, so only that row shows a
  // pending state and double-clicks cannot fire two `worktree add` calls, open
  // two chats or send the prompt twice.
  const [busyIndex, setBusyIndex] = useState<number | null>(null)
  const [errors, setErrors] = useState<Record<number, string>>({})
  // Which items have their prompt expanded. Index-keyed like `errors`, and
  // reset with them when `items` changes.
  const [shownPrompts, setShownPrompts] = useState<Record<number, boolean>>({})
  const promptIdBase = useId()
  // A project that is not a git repo, or is only a folder inside one, gets no
  // worktree action: the server refuses the first outright and the second
  // unless another session is scoped to the enclosing root. Both swap the
  // worktree action for a plain new chat, as a session with no project does.
  const insideRepo = !!projectDir && !!enclosingRepoRoot
  const notRepo = !!projectDir && (projectIsRepo === false || insideRepo)
  const canWorktree = !!projectDir && !notRepo

  // Errors are keyed by array index, and Skip REMOVES an item — which shifts
  // every later index down. Without this, skipping a failed item would re-render
  // its neighbour under the failed item's message, misattributing the failure to
  // an unrelated suggestion. Any change to `items` drops the stale errors.
  //
  // `itemsGen` closes the other half of the same hazard: a request that REJECTS
  // after `items` changed would otherwise write its error against the new
  // list's index. Each request captures the generation it started in and its
  // completion is ignored if that no longer matches.
  const itemsGen = useRef(0)
  useEffect(() => { itemsGen.current += 1; setErrors({}); setShownPrompts({}) }, [items])

  const startSession = async (item: FollowupItem, index: number) => {
    if (busyIndex !== null) return
    const gen = itemsGen.current
    setBusyIndex(index)
    setErrors(prev => {
      const next = { ...prev }
      delete next[index]
      return next
    })
    try {
      await (canWorktree ? onStartInWorktree(item, index) : onStartInNewChat(item, index))
    } catch (err) {
      // Drop the error if the card's items changed under us: `index` no longer
      // refers to the item this request was for.
      if (itemsGen.current === gen) {
        const fallback = canWorktree
          ? i18nT('components.followUpCard.failed_to_create_worktree')
          : i18nT('components.followUpCard.failed_to_open_new_chat')
        setErrors(prev => ({
          ...prev,
          [index]: err instanceof Error ? err.message : fallback,
        }))
      }
    } finally {
      setBusyIndex(null)
    }
  }

  return (
    <div
      className="border border-accent/30 rounded-xl bg-card shadow-md overflow-hidden animate-scale-in"
      role="group"
      aria-label={i18nT('components.followUpCard.follow_up_suggestions')}
    >
      <div className="flex items-center gap-2 px-4 pt-3 pb-1">
        <Lightbulb size={13} className="text-accent" aria-hidden="true" />
        <span className="text-[11px] font-semibold uppercase tracking-wider text-accent">
          {i18nT('components.followUpCard.suggested_follow_up', { count: items.length })}
        </span>
      </div>
      {items.map((item, index) => {
        const busy = busyIndex === index
        const error = errors[index]
        const promptShown = !!shownPrompts[index]
        const promptId = `${promptIdBase}-prompt-${index}`
        return (
          <div key={`${item.title}-${index}`} className={`px-4 py-3 ${index > 0 ? 'border-t border-border' : ''}`}>
            <div className="text-[13px] font-medium text-text">{item.title}</div>
            {item.description && (
              <div className="text-[12px] text-muted mt-1 leading-relaxed">{item.description}</div>
            )}
            <button
              type="button"
              onClick={() => setShownPrompts(prev => ({ ...prev, [index]: !prev[index] }))}
              aria-expanded={promptShown}
              aria-controls={promptShown ? promptId : undefined}
              className="inline-flex items-center gap-1 mt-1.5 p-0 text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer"
            >
              {promptShown ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
              {promptShown ? i18nT('components.followUpCard.hide_prompt') : i18nT('components.followUpCard.show_prompt')}
            </button>
            {promptShown && (
              <div
                id={promptId}
                className="mt-1.5 max-h-48 overflow-y-auto rounded-md border border-border bg-bg px-2.5 py-2 font-mono text-[11px] leading-relaxed text-text whitespace-pre-wrap break-words"
              >
                {item.prompt}
              </div>
            )}
            <div className="flex flex-wrap items-center gap-2 mt-2.5">
              {canWorktree ? (
                <button
                  onClick={() => startSession(item, index)}
                  disabled={busy || busyIndex !== null}
                  title={i18nT('components.followUpCard.create_worktree_and_open_session', { path: projectDir })}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed bg-accent text-accent-fg hover:bg-accent-hover border-none"
                >
                  <GitBranch size={13} aria-hidden="true" />
                  {busy ? i18nT('components.followUpCard.creating_worktree') : i18nT('components.followUpCard.start_in_new_worktree')}
                </button>
              ) : (
                // No repository to branch from, so the spawn route is a plain new
                // chat: same project directory and folder, no git call.
                <button
                  onClick={() => startSession(item, index)}
                  disabled={busy || busyIndex !== null}
                  title={
                    insideRepo
                      ? i18nT('components.followUpCard.open_new_chat_inside_repo', { path: projectDir, root: enclosingRepoRoot })
                      : notRepo
                        ? i18nT('components.followUpCard.open_new_chat_in_project', { path: projectDir })
                        : i18nT('components.followUpCard.open_new_chat')
                  }
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed bg-accent text-accent-fg hover:bg-accent-hover border-none"
                >
                  <MessageSquarePlus size={13} aria-hidden="true" />
                  {busy ? i18nT('components.followUpCard.opening_new_chat') : i18nT('components.followUpCard.start_in_new_chat')}
                </button>
              )}
              <button
                onClick={() => onAddToSession(item, index)}
                disabled={busyIndex !== null}
                title={i18nT('components.followUpCard.pre_fill_this_session_s_composer_with_the_expand')}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed border border-border text-muted bg-bg hover:text-text hover:border-accent/40"
              >
                <Plus size={13} aria-hidden="true" /> {i18nT('components.followUpCard.add_to_this_session')}
              </button>
              <button
                onClick={() => onSkip(index)}
                disabled={busyIndex !== null}
                title={i18nT('components.followUpCard.dismiss_this_suggestion')}
                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed border border-transparent text-muted hover:text-text bg-transparent"
              >
                <X size={13} aria-hidden="true" /> {i18nT('components.followUpCard.skip')}
              </button>
            </div>
            {/* askAgent on: the card holds no draft of its own and the host
                composer's draft is persisted per slot (ChatPage saveDrafts), so
                the hand-off — which opens a fresh slot — destroys nothing. A
                worktree that could not be created (branch exists, not a repo,
                git missing) is exactly the failure the agent can diagnose. */}
            <ErrorNotice
              variant="inline"
              className="mt-2 whitespace-normal"
              message={error}
              askAgent
              testId={`follow-up-error-${index}`}
            />
          </div>
        )
      })}
      <div className="px-4 pb-3 text-[11px] text-muted">
        {/* The standard footer covers a session with no project directory.
            With one, the footer also says where the new chat opens; why no
            worktree is created lives in the tooltip, so the always-visible
            line stays in session terms. */}
        {canWorktree || !notRepo
          ? i18nT('components.followUpCard.footer_start_sends_add_prefills')
          : i18nT('components.followUpCard.footer_start_sends_add_prefills_project_directory')}
      </div>
    </div>
  )
}

export default memo(FollowUpCard)
