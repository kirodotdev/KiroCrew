import { memo } from 'react'
import { SmilePlus, Check } from 'lucide-react'
import { REACTION_EMOJI } from '../../api/reactions'
import type { ReactionError } from '../../hooks/useChatReactions'
import ErrorNotice from '../../components/ErrorNotice'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

interface MessageReactionBarProps {
  /** Emoji the CURRENT caller has already placed on this message. */
  active: ReadonlySet<string>
  onToggle: (emoji: string) => void
  /** The last failed reaction on THIS message, when one is unacknowledged.
   *  Rendered as an inline notice so a failure is never silent
   *  (`errors-use-error-notice`). */
  error?: ReactionError | null
  onClearError?: () => void
  /** The host's own hover-reveal wrapper decides visibility, same contract as
   *  the row's other icon buttons. */
  className?: string
}

/**
 * Fixed reaction set (👍 ✅ 👀 ⭐), no picker, no custom emoji.
 *
 * The row is two regions:
 *   1. PLACED reactions — one marker per emoji the caller has placed, drawn as
 *      its own glyph and always visible (hover or not), so the reaction state
 *      reads at a glance. Bookmarking a message to find it again is the job of
 *      pins, which list it and jump back to it; these markers do not.
 *      These markers are NON-INTERACTIVE display, not buttons: a message can
 *      carry up to four, and four removal buttons in one row would break
 *      `max-two-buttons-per-row`. They show reaction STATE only.
 *   2. ADD / manage — a single `+` trigger (the ONE action control, so the row
 *      stays within the two-button cap however many emoji are placed) that
 *      opens the fixed set. An unplaced emoji reads "React with …"; a placed
 *      one reads "Remove … reaction" with a check, so the menu both adds and
 *      REMOVES. On a message with nothing placed the trigger reveals on
 *      hover/focus; once a reaction is placed it stays visible at rest, since
 *      the markers are not clickable and the menu is the only way to remove one.
 *
 * Why this shape: `max-two-buttons-per-row` (website/AUTOSDE.yaml) caps a row of
 * peer ACTION controls at two. Making the placed glyphs plain markers (not
 * clickable removal buttons) and routing every add AND remove through the one
 * `+` menu keeps exactly one action control in the row, whatever is placed —
 * the resolution both AI reviewers prescribed. An earlier revision made the
 * chips removal buttons, which put up to four actions in the row.
 */
const MessageReactionBar = memo(function MessageReactionBar({ active, onToggle, error, onClearError, className }: MessageReactionBarProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly so a language switch re-renders the labels
  // Placed emoji in the fixed set's own order, so the markers never reshuffle.
  const placed = REACTION_EMOJI.filter(e => active.has(e))
  // Keep the add control visible at rest while a notice is showing (it tells
  // the reader to open +) or while a reaction is placed: the markers are not
  // clickable, so the + is the only way to remove one and must not hide.
  const forceVisible = error != null || placed.length > 0
  const errorMessage = error
    ? error.kind === 'limit'
      ? error.limit
        ? i18nT('pages.chat.messageReactionBar.reaction_limit_reached_count', { limit: error.limit })
        : i18nT('pages.chat.messageReactionBar.reaction_limit_reached')
      : error.kind === 'unreact'
        ? i18nT('pages.chat.messageReactionBar.unreaction_failed', { emoji: error.emoji })
        : error.emoji
          ? i18nT('pages.chat.messageReactionBar.reaction_failed_emoji', { emoji: error.emoji })
          : i18nT('pages.chat.messageReactionBar.reaction_failed')
    : null

  return (
    <div
      data-testid="message-reaction-bar"
      className={`flex flex-col gap-0.5 ${className ?? ''}`}
    >
      <div className="flex items-center gap-0.5">
        {/* Placed reactions: the message's reaction STATE, always visible, as
            NON-INTERACTIVE markers (span, not button) so up to four of them
            never form a row of removal buttons over the two-action cap. Removal
            is in the `+` menu, which lists each placed emoji as "Remove …". */}
        {placed.length > 0 && (
          <div
            data-testid="reaction-placed-group"
            className="flex items-center gap-0.5"
          >
            {placed.map(emoji => (
              <span
                key={emoji}
                role="img"
                data-testid="reaction-chip"
                data-emoji={emoji}
                data-reaction-active="true"
                aria-label={i18nT('pages.chat.messageReactionBar.reaction_placed', { emoji })}
                title={i18nT('pages.chat.messageReactionBar.reaction_placed', { emoji })}
                // Flat, no ring or filled background: a ringed/filled box reads
                // as a clickable button, but these markers are non-interactive
                // (removal is in the + menu), so the flat look stops people
                // expecting a click (UX review). The glyph alone is the marker.
                className="flex items-center justify-center w-6 h-6 text-sm leading-none opacity-100 [@media(hover:none)]:min-h-10 [@media(hover:none)]:min-w-10"
              >
                <span aria-hidden="true">{emoji}</span>
              </span>
            ))}
          </div>
        )}
        {/* The single action control — the ONE button toward the two-per-row
            cap, however many emoji are placed. It both ADDS (an unplaced emoji
            reads "React with …") and REMOVES (a placed one reads "Remove …"
            with a check), so it stays present even when all four are placed. */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              data-testid="reaction-add-trigger"
              aria-label={i18nT('pages.chat.messageReactionBar.react')}
              title={i18nT('pages.chat.messageReactionBar.react')}
              // Transparent at rest, fading in on hover/focus of the message
              // row. `[@media(hover:none)]:opacity-100` is required, not
              // cosmetic: a touch device has no hover, so without it a tablet
              // user could never place a FIRST reaction. Kept inline in
              // `className`; a `const` holding these classes reads as a bare
              // literal to the i18n added-lines gate.
              className={`flex items-center justify-center w-6 h-6 rounded-md text-sm leading-none transition-colors bg-transparent border-none cursor-pointer [@media(hover:none)]:min-h-10 [@media(hover:none)]:min-w-10 ${forceVisible ? 'opacity-100 hover:bg-bg-hover' : 'opacity-0 transition-opacity duration-300 delay-100 group-hover/msg:opacity-100 group-hover/msg:delay-300 group-focus-within/msg:opacity-100 group-focus-within/msg:delay-300 [@media(hover:none)]:opacity-100 hover:bg-bg-hover'}`}
            >
              <SmilePlus size={15} aria-hidden="true" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-[180px]">
            {REACTION_EMOJI.map(emoji => {
              const isActive = active.has(emoji)
              const label = isActive
                ? i18nT('pages.chat.messageReactionBar.remove_reaction', { emoji })
                : i18nT('pages.chat.messageReactionBar.react_with', { emoji })
              return (
                <DropdownMenuItem
                  key={emoji}
                  data-testid="reaction-menu-item"
                  data-emoji={emoji}
                  aria-label={label}
                  onSelect={() => onToggle(emoji)}
                >
                  <span aria-hidden="true" className="text-sm leading-none">{emoji}</span>
                  <span>{label}</span>
                  {isActive && <Check size={13} className="ml-auto shrink-0 text-accent" />}
                </DropdownMenuItem>
              )
            })}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {/* No hand-off: the agent hand-off navigates to the chat and unmounts
          whatever rendered this notice, which would discard the unsaved
          composer draft and any open inline message editor on this row — the
          same ruling the sibling copy-failure notices in UserMessage /
          AssistantMessage make. A reaction retry needs neither the agent nor a
          navigation: the emoji is still reachable through the `+` menu (which
          both adds and removes), so the notice only reports and dismisses
          (errors-use-error-notice). */}
      <ErrorNotice
        message={errorMessage}
        variant="inline"
        onDismiss={onClearError}
        className="[@media(hover:none)]:[&_button]:min-h-10 [@media(hover:none)]:[&_button]:min-w-10"
      />
    </div>
  )
})

export { MessageReactionBar }
export type { MessageReactionBarProps }
