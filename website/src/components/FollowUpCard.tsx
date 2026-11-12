import { memo, useEffect, useId, useRef, useState } from 'react'
import { Trans } from 'react-i18next'
import { ChevronDown, ChevronRight, GitBranch, Lightbulb, MessageSquarePlus, Plus, Send, X } from 'lucide-react'
import type { FollowupItem } from '../store/chatSlice'
import ErrorNotice from './ErrorNotice'
import { SettingsLink } from './SettingsLink'
import { reportUiControlClick } from '../lib/uiControlTelemetry'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

/** What a Start press does with the prompt: `prefill` puts it in the new
 *  session's message box (the default), `send` sends it as that session's
 *  first message (Settings, "Send follow-ups with one click"). */
export type FollowupStartMode = 'send' | 'prefill'

export interface FollowUpCardProps {
  items: FollowupItem[]
  /**
   * Pre-fill THIS session's composer with the item's expanded prompt, and drop
   * that one item from the card. `index` is the item's position in `items`.
   */
  onAddToSession: (item: FollowupItem, index: number) => void
  /**
   * Create a git worktree, open a session scoped to it, and hand it the item's
   * prompt the way `mode` says: in its message box, or sent as its first
   * message. Rejects with a user-facing message on failure (branch exists, not
   * a git repo, git unavailable, send refused) which the card renders inline.
   * Once the prompt is handed over only that one item leaves the card; its
   * siblings stay.
   */
  onStartInWorktree: (item: FollowupItem, index: number, mode: FollowupStartMode) => Promise<void>
  /**
   * Open a new session in this session's project directory (where a new chat
   * starts by default when it has none) and hand it the item's prompt the way
   * `mode` says, with no git involved. The primary action when there is no
   * repository to branch from.
   * Rejects with a user-facing message on failure, rendered inline like the
   * worktree route's. Once the prompt is handed over only that one item leaves
   * the card.
   */
  onStartInNewChat: (item: FollowupItem, index: number, mode: FollowupStartMode) => Promise<void>
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
  /**
   * The person's "Send follow-ups with one click" setting. Off (the default),
   * Start opens the new session with the prompt in its message box; on, Start
   * sends it. The footer, the send glyph and the mode passed to Start all come
   * from this one value, so a press does what the card said it would.
   */
  startSends?: boolean
}

// The collapsed card shows this many lines of each prompt. A prompt with more
// lines, or one long enough to wrap past them, gets a Show prompt toggle.
const PROMPT_PREVIEW_LINES = 3
const PROMPT_PREVIEW_CHARS = 240

/** The prompt's lines from the first one with text. The send trims the prompt,
 *  so leading blank lines never reach the new session, and counting them would
 *  fill the preview with nothing but the ellipsis. */
function followupPromptLines(prompt: string): string[] {
  const lines = prompt.split('\n')
  const first = lines.findIndex(line => line.trim() !== '')
  return first < 0 ? [] : lines.slice(first)
}

/** The collapsed card's text: the first PROMPT_PREVIEW_LINES lines with text,
 *  cut at PROMPT_PREVIEW_CHARS, with a trailing ellipsis that says more is hidden. */
function followupPromptPreview(prompt: string): string {
  const head = followupPromptLines(prompt).slice(0, PROMPT_PREVIEW_LINES).join('\n')
  return `${head.slice(0, PROMPT_PREVIEW_CHARS).trimEnd()}…`
}

/** Whether the collapsed card hides any of the prompt, so it needs Show prompt. */
function followupPromptHasMore(prompt: string): boolean {
  const lines = followupPromptLines(prompt)
  return lines.length > PROMPT_PREVIEW_LINES || lines.join('\n').length > PROMPT_PREVIEW_CHARS
}

/**
 * Agent-authored follow-up suggestions, rendered above the composer.
 *
 * Start (a separate copy, which is a git worktree, or a new chat) opens a new
 * session with the item's prompt in its message box, and nothing is sent until
 * the person presses send. With "Send follow-ups with one click" on
 * (`startSends`), Start SENDS the prompt as the new session's first message,
 * so one click runs agent-authored text, and the person stays on this session
 * while it runs. Every item shows the first three
 * lines of its prompt with no toggle, and Show prompt expands the rest
 * (rfc-followup-start-sends.md, Decision 3): the title and description are a
 * label the agent chose, and only the prompt is what runs.
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
  startSends = false,
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
  // The footer says what Start does; both Start buttons point at it so a screen
  // reader hears what a press does, as the send glyph shows when Start sends.
  const footerId = `${promptIdBase}-footer`
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
    reportUiControlClick(canWorktree ? 'followup_start_worktree' : 'followup_start_chat')
    const mode: FollowupStartMode = startSends ? 'send' : 'prefill'
    try {
      await (canWorktree ? onStartInWorktree(item, index, mode) : onStartInNewChat(item, index, mode))
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
        const promptHasMore = followupPromptHasMore(item.prompt)
        return (
          <div key={`${item.title}-${index}`} className={`px-4 py-3 ${index > 0 ? 'border-t border-border' : ''}`}>
            <div className="text-[13px] font-medium text-text">{item.title}</div>
            {item.description && (
              <div className="text-[12px] text-muted mt-1 leading-relaxed">{item.description}</div>
            )}
            <div
              id={promptId}
              data-testid="followup-prompt"
              className={`mt-1.5 rounded-md border border-border bg-bg px-2.5 py-2 font-mono text-[11px] leading-relaxed text-text whitespace-pre-wrap break-words ${promptShown ? 'max-h-48 overflow-y-auto' : ''}`}
            >
              {promptShown || !promptHasMore ? item.prompt : followupPromptPreview(item.prompt)}
            </div>
            {promptHasMore && (
              <button
                type="button"
                onClick={() => setShownPrompts(prev => ({ ...prev, [index]: !prev[index] }))}
                aria-expanded={promptShown}
                aria-controls={promptId}
                className="inline-flex items-center gap-1 mt-1 p-0 text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer"
              >
                {promptShown ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
                {promptShown ? i18nT('components.followUpCard.hide_prompt') : i18nT('components.followUpCard.show_prompt')}
              </button>
            )}
            <div className="flex flex-wrap items-center gap-2 mt-2.5">
              {canWorktree ? (
                <button
                  onClick={() => startSession(item, index)}
                  disabled={busy || busyIndex !== null}
                  aria-describedby={footerId}
                  title={i18nT('components.followUpCard.create_worktree_and_open_session', { path: projectDir })}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed bg-accent text-accent-fg hover:bg-accent-hover border-none"
                >
                  <GitBranch size={13} aria-hidden="true" />
                  {/* One-click send gets its own verb, so a press that runs the
                      prompt never looks like the press that only pre-fills. */}
                  {busy
                    ? i18nT('components.followUpCard.creating_worktree')
                    : startSends ? i18nT('components.followUpCard.run_in_separate_copy') : i18nT('components.followUpCard.start_in_new_worktree')}
                  {startSends && <Send size={12} aria-hidden="true" className="opacity-80" />}
                </button>
              ) : (
                // No repository to branch from, so the spawn route is a plain new
                // chat: same project directory and folder, no git call.
                <button
                  onClick={() => startSession(item, index)}
                  disabled={busy || busyIndex !== null}
                  aria-describedby={footerId}
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
                  {busy
                    ? i18nT('components.followUpCard.opening_new_chat')
                    : startSends ? i18nT('components.followUpCard.run_in_new_chat') : i18nT('components.followUpCard.start_in_new_chat')}
                  {startSends && <Send size={12} aria-hidden="true" className="opacity-80" />}
                </button>
              )}
              <button
                onClick={() => { reportUiControlClick('followup_add_session'); onAddToSession(item, index) }}
                disabled={busyIndex !== null}
                title={i18nT('components.followUpCard.pre_fill_this_session_s_composer_with_the_expand')}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[12px] font-medium cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed border border-border text-muted bg-bg hover:text-text hover:border-accent/40"
              >
                <Plus size={13} aria-hidden="true" /> {i18nT('components.followUpCard.add_to_this_session')}
              </button>
              <button
                onClick={() => { reportUiControlClick('followup_skip'); onSkip(index) }}
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
      <div id={footerId} className="px-4 pb-3 text-[11px] text-muted">
        {/* The footer describes whichever Start behaviour is active. With no
            project directory it covers a plain new chat; with one that is not
            a repository it also says where the new chat opens. Why no
            worktree is created lives in the tooltip, so the always-visible
            line stays in plain terms. Both worktree footers say what the
            copy is (a new session on its own copy of the project) and that
            this session is left alone, so the label needs no git term. */}
        {/* Each one-click footer opens with why Start sends at once and where
            to turn that off, inside the same sentence run, with the setting's
            own name as the link to it. The person is never surprised by a card
            that behaves differently from the one they learned, and the reason
            cannot be read apart from the behaviour it explains. The Settings
            path is built from the tabs' own labels so it matches the nav. */}
        <span>
        {startSends
          ? (
            <Trans
              i18nKey={canWorktree
                ? 'components.followUpCard.footer_worktree_start_sends'
                : !notRepo
                  ? 'components.followUpCard.footer_start_sends_add_prefills'
                  : 'components.followUpCard.footer_start_sends_add_prefills_project_directory'}
              values={{
                setting: i18nT('pages.settings.chatPanel.followup_start_sends'),
                path: [i18nT('nav.settings'), i18nT('settings.tabs.chat.label'), i18nT('pages.settings.chatPanel.composer')].join(' → '),
              }}
              components={[<SettingsLink key="setting" tab="chat" sub="composer" highlight="chat.send-follow-ups-with-one-click" />]}
            />
          )
          : canWorktree
            ? i18nT('components.followUpCard.footer_worktree_prefills')
            : notRepo
              ? i18nT('components.followUpCard.footer_both_prefill_project_directory')
              : i18nT('components.followUpCard.footer_both_prefill')}
        </span>
      </div>
    </div>
  )
}

export default memo(FollowUpCard)
