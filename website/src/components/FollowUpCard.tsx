import { memo, useEffect, useRef, useState } from 'react'
import { GitBranch, Lightbulb, Plus, X } from 'lucide-react'
import type { FollowupItem } from '../store/chatSlice'

import { i18nT } from '../i18n/t'
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
   * Absent when the active session has no project directory. The worktree
   * button is disabled in that case — there is no repo to branch from.
   */
  projectDir?: string
}

/**
 * Agent-authored follow-up suggestions, rendered above the composer.
 *
 * Both non-skip actions PRE-FILL a composer rather than sending: the user
 * always sees the handoff prompt and presses send themselves, so a click can
 * never start an unattended turn. That is a deliberate product constraint, not
 * an implementation shortcut — see `suggest_followup` in mcp_core.py, whose
 * tool description promises the same thing to the model.
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
  projectDir,
}: FollowUpCardProps) {
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
              <button
                onClick={() => startWorktree(item, index)}
                disabled={busy || busyIndex !== null || !projectDir}
                title={
                  projectDir
                    ? i18nT('components.followUpCard.create_worktree_and_open_session', { path: projectDir })
                    : i18nT('components.followUpCard.this_session_has_no_project_directory_so_there_i')
                }
                className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed ${
                  // Accent (primary-CTA) styling ONLY when the action can work.
                  // An unscoped session disables this button permanently, and a
                  // dimmed accent button still reads as "the main action" on a
                  // dark theme — users click it, meet a not-allowed cursor, and
                  // report it as a dead button. Demote it to the secondary
                  // look so "Add to this session" is the visual default, and let
                  // the footer (below) say WHY instead of hiding the feature.
                  projectDir
                    ? 'bg-accent text-accent-fg hover:bg-accent-hover border-none'
                    : 'border border-border text-muted bg-bg'
                }`}
              >
                <GitBranch size={13} aria-hidden="true" />
                {busy ? i18nT('components.followUpCard.creating_worktree') : i18nT('components.followUpCard.start_in_new_worktree')}
              </button>
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
            {error && (
              <div role="alert" className="text-[12px] text-danger mt-2">
                {error}
              </div>
            )}
          </div>
        )
      })}
      <div className="px-4 pb-3 text-[11px] text-muted">
        {projectDir
          ? i18nT('components.followUpCard.both_actions_pre_fill_the_composer_nothing_is_se')
          // The unscoped variant must not claim "both actions": the worktree
          // button is disabled above, and this line is where the user learns
          // why — the tooltip alone hides behind a hover the not-allowed
          // cursor has already soured.
          : i18nT('components.followUpCard.worktree_disabled_no_project_directory')}
      </div>
    </div>
  )
}

export default memo(FollowUpCard)
