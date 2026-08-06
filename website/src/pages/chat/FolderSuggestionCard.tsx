import { FolderInput } from 'lucide-react'
import { motion } from 'framer-motion'

import { i18nT } from '../../i18n/t'

export interface FolderSuggestionCardProps {
  /** Folder name, and its root→leaf breadcrumb when the folder is nested. */
  folderName: string
  breadcrumb: string
  /** File the session into the suggested folder. */
  onAccept: () => void
  /** Leave the session where it is. The card is not re-offered either way. */
  onDecline: () => void
}

/**
 * "File this session in <folder>?" — offered once, after the session is titled,
 * for a session that is not in a folder yet.
 *
 * Rendered in the composer's own width box (via ChatInput's `aboveComposer`), so
 * it shares the tip's exact geometry. It takes precedence over the ambient tip
 * rather than stacking with it: the tip yields through `tipSuppressed` in
 * ChatPage, because two cards in that band is the crowding the band's priority
 * contract exists to prevent.
 *
 * Both buttons are terminal — there is nothing server-side to resolve, and the
 * backend offers at most one card per slot for that slot's lifetime, so
 * declining cannot be re-asked and accepting is a plain folder move the user can
 * undo from the sidebar.
 */
export default function FolderSuggestionCard({ folderName, breadcrumb, onAccept, onDecline }: FolderSuggestionCardProps) {
  // Show the breadcrumb only when it adds ancestry: for a root folder it is just
  // the name again, and rendering both reads as a duplicate.
  const parentPath = breadcrumb && breadcrumb !== folderName ? breadcrumb : ''

  return (
    <motion.div
      className="w-full flex items-center gap-2.5 px-4 py-2 rounded-md text-xs shadow-lg"
      style={{
        background: 'color-mix(in srgb, var(--accent) 6%, var(--bg-elevated))',
        border: '1px solid color-mix(in srgb, var(--accent) 12%, transparent)',
      }}
      initial={{ y: 6, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      exit={{ y: 4, opacity: 0 }}
      transition={{ duration: 0.25, ease: [0.2, 0.8, 0.2, 1] }}
      role="complementary"
      aria-label={i18nT('components.folderSuggestionCard.folder_suggestion')}
      data-testid="folder-suggestion-card"
    >
      {/* Always the lucide glyph, never the folder's own emoji: an emoji is a
          font-dependent bitmap that renders as a tofu box wherever the platform
          has no emoji font, and it would not inherit --accent, so the card's one
          icon would stop tracking the theme. */}
      <FolderInput size={14} className="shrink-0" aria-hidden="true" style={{ color: 'var(--accent)' }} />

      <div className="min-w-0 flex-1">
        {/* One interpolated string, not a concatenation of "Move this session to"
            + name + "?": a split sentence cannot be reordered by a translator,
            and several locales need the folder name somewhere other than last. */}
        <span className="block text-[12px] leading-tight truncate" style={{ color: 'var(--text)' }}>
          {i18nT('components.folderSuggestionCard.move_to_folder_question', { folder: folderName })}
        </span>
        {parentPath && (
          <span className="block text-[11px] leading-tight mt-0.5 truncate" style={{ color: 'var(--muted)' }} title={parentPath}>
            {parentPath}
          </span>
        )}
      </div>

      <div className="flex items-center gap-1.5 shrink-0">
        <button
          onClick={onAccept}
          data-testid="folder-suggestion-accept"
          className="px-2.5 py-1 rounded text-[11px] font-medium hover:brightness-110 transition"
          style={{
            color: 'var(--accent)',
            background: 'color-mix(in srgb, var(--accent) 12%, transparent)',
            border: '1px solid color-mix(in srgb, var(--accent) 30%, transparent)',
          }}
        >
          {i18nT('components.folderSuggestionCard.yes_move_it')}
        </button>
        <button
          onClick={onDecline}
          data-testid="folder-suggestion-decline"
          className="px-2.5 py-1 rounded text-[11px] transition-colors hover:bg-[var(--bg-hover)]"
          style={{ color: 'var(--muted)', border: '1px solid var(--border)' }}
        >
          {i18nT('components.folderSuggestionCard.no_thanks')}
        </button>
      </div>
    </motion.div>
  )
}
