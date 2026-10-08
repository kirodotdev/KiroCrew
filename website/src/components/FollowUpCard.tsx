import { memo, useEffect, useRef, useState } from 'react'
import { FolderGit2, GitBranch, Lightbulb, Plus, X } from 'lucide-react'
import type { FollowupItem } from '../store/chatSlice'
import ErrorNotice from './ErrorNotice'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
export interface FollowUpCardProps {
  items: FollowupItem[]
  /** Pre-fill THIS session's composer with the item's expanded prompt. */
  onAddToSession: (item: FollowupItem) => void
  /**
   * Create a git worktree, open a session scoped to it, and pre-fill that
   * session's composer. Rejects with a user-facing message on failure (branch
   * exists, not a git repo, git unavailable) which the card renders inline.
   */
  onStartInWorktree: (item: FollowupItem) => Promise<void>
  /** Drop this single suggestion; siblings stay. */
  onSkip: (index: number) => void
  /**
   * Open the session's project picker. When the worktree action cannot run
   * (no project directory, or one that is not a git repo), the card's primary
   * slot REPLACES "Start in new worktree" with an enabled "Set repo path…"
   * button that calls this — the feature stays visible and the user always
   * has a valid way forward, rather than meeting a hidden or dead control.
   */
  onSetProject: () => void
  /**
   * The active session's project directory. Absent when the session has none.
   */
  projectDir?: string
  /**
   * Whether ``projectDir`` is actually a git work tree. The worktree action
   * runs only when the project is a repo it can branch from; otherwise the
   * primary slot shows "Set repo path…" instead.
   *
   * ``undefined`` means "not resolved yet" — treated as a repo so the worktree
   * action is offered optimistically and a slow (or momentarily failing) git
   * probe never swaps a working button for the picker. The server still
   * refuses a genuine non-repo and the card renders that inline. Pass ``false``
   * only once the probe has CONFIRMED the directory is not a repo — never for a
   * probe that merely errored, or a transient failure on a real repo would
   * wrongly show the picker.
   */
  projectIsRepo?: boolean
}

/**
 * Agent-authored follow-up suggestions, rendered above the composer.
 *
 * Both prompt-handoff actions PRE-FILL a composer rather than sending: the user
 * always sees the handoff prompt and presses send themselves, so a click can
 * never start an unattended turn. That is a deliberate product constraint, not
 * an implementation shortcut — see `suggest_followup` in mcp_core.py, whose
 * tool description promises the same thing to the model.
 *
 * The primary slot is NEVER empty and NEVER a dead control: when the session's
 * project is a git repo it offers "Start in new worktree"; otherwise it offers
 * "Set repo path…", which opens the project picker so the user can point the
 * session at a repo and then branch from it. Hiding or permanently disabling
 * the feature was rejected — a visible, valid next step is the product rule.
 *
 * All item strings are LLM-authored. They are rendered as text children only
 * (never dangerouslySetInnerHTML), on top of the server-side sanitization and
 * credential/URL redaction in `_redact_followup_item`.
 */
function FollowUpCard({
  items,
  onAddToSession,
  onStartInWorktree,
  onSkip,
  onSetProject,
  projectDir,
  projectIsRepo,
}: FollowUpCardProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // The worktree action runs only when there is a project directory AND it is
  // a git repo to branch from. `projectIsRepo === undefined` means the probe
  // has not resolved yet: treat it as a repo so a slow/transiently-failing git
  // check never swaps a working button for the picker, and let the server
  // refuse a genuine non-repo (the card renders that inline). Only an explicit
  // `false` (a CONFIRMED non-repo) shows the "Set repo path…" button instead.
  const canWorktree = !!projectDir && projectIsRepo !== false
  // Index of the item whose worktree is being created, so only that row shows
  // a pending state and double-clicks cannot fire two `worktree add` calls.
  const [busyIndex, setBusyIndex] = useState<number | null>(null)
  const [errors, setErrors] = useState<Record<number, string>>({})

  // Errors are keyed by array index, and Skip REMOVES an item — which shifts
  // every later index down. Without this, skipping a failed item would re-render
  // its neighbour under the failed item's message, misattributing the failure to
  // an unrelated suggestion. Any change to `items` drops the stale errors.
  //
  // `itemsGen` closes the other half of the same hazard: a worktree request that
  // REJECTS after `items` changed would otherwise write its error against the new
  // list's index. Each request captures the generation it started in and its
  // completion is ignored if that no longer matches.
  const itemsGen = useRef(0)
  useEffect(() => { itemsGen.current += 1; setErrors({}) }, [items])

  const startWorktree = async (item: FollowupItem, index: number) => {
    if (busyIndex !== null) return
    const gen = itemsGen.current
    setBusyIndex(index)
    setErrors(prev => {
      const next = { ...prev }
      delete next[index]
      return next
    })
    try {
      await onStartInWorktree(item)
    } catch (err) {
      // Drop the error if the card's items changed under us: `index` no longer
      // refers to the item this request was for.
      if (itemsGen.current === gen) {
        setErrors(prev => ({
          ...prev,
          [index]: err instanceof Error ? err.message : i18nT('components.followUpCard.failed_to_create_worktree'),
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
        return (
          <div key={`${item.title}-${index}`} className={`px-4 py-3 ${index > 0 ? 'border-t border-border' : ''}`}>
            <div className="text-[13px] font-medium text-text">{item.title}</div>
            {item.description && (
              <div className="text-[12px] text-muted mt-1 leading-relaxed">{item.description}</div>
            )}
            <div className="flex flex-wrap items-center gap-2 mt-2.5">
              {/* Primary slot. When the project is a git repo, offer the
                  worktree action. Otherwise REPLACE it with an enabled
                  "Set repo path…" that opens the project picker — the feature
                  stays visible and the user gets a valid way forward, instead
                  of a hidden or dead-disabled button. Both carry the accent
                  (primary-CTA) style because both are real, clickable actions. */}
              {canWorktree ? (
                <button
                  onClick={() => startWorktree(item, index)}
                  disabled={busy || busyIndex !== null}
                  title={i18nT('components.followUpCard.create_worktree_and_open_session', { path: projectDir })}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed bg-accent text-accent-fg hover:bg-accent-hover border-none"
                >
                  <GitBranch size={13} aria-hidden="true" />
                  {busy ? i18nT('components.followUpCard.creating_worktree') : i18nT('components.followUpCard.start_in_new_worktree')}
                </button>
              ) : (
                <button
                  onClick={onSetProject}
                  disabled={busyIndex !== null}
                  title={
                    projectDir
                      ? i18nT('components.followUpCard.set_repo_path_not_a_repo', { path: projectDir })
                      : i18nT('components.followUpCard.set_repo_path_no_project')
                  }
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed bg-accent text-accent-fg hover:bg-accent-hover border-none"
                >
                  <FolderGit2 size={13} aria-hidden="true" /> {i18nT('components.followUpCard.set_repo_path')}
                </button>
              )}
              <button
                onClick={() => onAddToSession(item)}
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
        {canWorktree
          ? i18nT('components.followUpCard.both_actions_pre_fill_the_composer_nothing_is_se')
          // The worktree action cannot run here, so the primary slot shows
          // "Set repo path…" instead. The footer names what that button does
          // and why the worktree route is not yet available — no project at
          // all, or a project that is not a git repository.
          : projectDir
            ? i18nT('components.followUpCard.set_repo_path_footer_not_a_repo')
            : i18nT('components.followUpCard.set_repo_path_footer_no_project')}
      </div>
    </div>
  )
}

export default memo(FollowUpCard)
