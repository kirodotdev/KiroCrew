import { useId, useState } from 'react'
import { FolderInput } from 'lucide-react'
import { motion } from 'framer-motion'

import { i18nT } from '../../i18n/t'
import type { ChatFolder } from '../../types'
import { orderFoldersWithPaths, type FolderSortMode } from '../../utils/folderTree'
import { NativeSelect, NativeSelectOption } from '../../components/ui/native-select'

export interface FolderSuggestionCardProps {
  /** The folder the backend suggested — preselected in the dropdown. */
  suggestedFolderId: string
  /** The suggestion's display name, carried separately from `folders` so the
   *  card can still offer the suggestion when the folder list has not loaded
   *  (or no longer contains it). */
  suggestedFolderName: string
  /** The suggestion's full ancestry path as the backend spelled it. Only read
   *  on the fallback path: when `folders` cannot label the suggestion, the
   *  synthetic option shows this instead of the bare name, so a nested
   *  destination keeps its ancestry exactly as the pre-dropdown card's
   *  breadcrumb line did. */
  suggestedFolderBreadcrumb?: string
  /** Every folder the user can file into, shared from the sidebar's
   *  `['chat-folders']` cache. Order/nesting is normalized here. */
  folders: readonly ChatFolder[]
  /** File the session into the folder the dropdown currently shows. */
  onAccept: (folderId: string) => void
  /** Leave the session where it is. The card is not re-offered either way. */
  onDecline: () => void
  /** The sidebar's folder sort mode, so the option list reads in the order the
   *  sidebar draws the same tree. A prop, passed by ChatPage from
   *  `useFolderSortMode`, so this card stays a pure function of its props. */
  folderSortMode?: FolderSortMode
}

/**
 * "Move this session into: <folder ▾>?" — offered once, after the session is
 * titled, for a session that is not in a folder yet. The dropdown arrives
 * prefilled with the suggestion, so a correct guess is still one click on
 * Move — and a wrong one is a pick from the same control instead of a decline
 * followed by a hunt through the sidebar.
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
 * undo from the sidebar. There is deliberately no "No folder (root)" entry in
 * the dropdown: staying at root IS the decline button.
 *
 * Answering is not the only way out: an untouched card ages out after
 * FOLDER_SUGGESTION_MAX_TURNS of the user's own confirmed sends made while this
 * card was on screen (chatSlice's `ageFolderSuggestion`, dispatched by the
 * ChatPage render site), so a wrong guess costs the composer band a few turns
 * rather than the whole session.
 *
 * The render site keys this card by the suggestion's `ts`, so a replacement
 * suggestion remounts it and the dropdown re-prefills — a selection made
 * against the previous card can never leak onto the new one.
 */
export default function FolderSuggestionCard({
  suggestedFolderId, suggestedFolderName, suggestedFolderBreadcrumb, folders, onAccept, onDecline,
  folderSortMode = 'custom',
}: FolderSuggestionCardProps) {
  const selectId = useId()
  const [selectedId, setSelectedId] = useState(suggestedFolderId)

  // Pre-order tree sequence, the same normalization every folder picker uses.
  // Options are labeled with the full ancestry path for nested folders (root
  // folders keep the bare name): unlike the pickers' indented popup rows, a
  // closed <select> shows ONLY the chosen option's text, so the path is what
  // keeps same-named subfolders under different parents unambiguous — and it
  // replaces the old card's separate breadcrumb line.
  const options = orderFoldersWithPaths(folders, folderSortMode).map(o => ({
    id: o.folder.id,
    label: o.depth > 0 ? o.path : o.folder.name,
  }))
  // The suggestion must always be offerable, even when the folder list is
  // still loading, failed (ChatPage normalizes that to []), or no longer
  // carries the folder. A synthetic first option keeps the card functional —
  // exactly the pre-dropdown behavior — rather than rendering an empty select.
  // Labeled by the suggestion's own breadcrumb so a nested destination keeps
  // its full ancestry here too, matching the real options' path labels.
  if (!options.some(o => o.id === suggestedFolderId)) {
    options.unshift({ id: suggestedFolderId, label: suggestedFolderBreadcrumb || suggestedFolderName })
  }
  // Act on what the select SHOWS. If a refetch removed the selected folder,
  // the browser falls back to displaying the first option — accepting must
  // follow the visible choice, never a stale id the user can no longer see.
  const effectiveId = options.some(o => o.id === selectedId) ? selectedId : options[0].id
  const selectedLabel = options.find(o => o.id === effectiveId)?.label ?? suggestedFolderName

  return (
    <motion.div
      // flex-wrap is the narrow-viewport provision (AUTOSDE narrow-viewport-
      // required): when label + select + actions exceed the row — a 320px
      // viewport leaves ~288px inside the padding — the overflowing pieces
      // wrap to their own line instead of clipping. Content-driven on purpose:
      // the card tracks the composer's width, not the viewport's, so a media
      // query would misfire in narrow side-by-side panes.
      className="w-full flex flex-wrap items-center gap-x-2.5 gap-y-1.5 px-4 py-2 rounded-md text-xs shadow-lg"
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

      {/* A real <label htmlFor>, not aria-label: the visible sentence IS the
          select's accessible name, and clicking it focuses the control. */}
      <label
        htmlFor={selectId}
        className="text-[12px] leading-tight shrink-0"
        style={{ color: 'var(--text)' }}
      >
        {i18nT('components.folderSuggestionCard.move_to_folder_prompt')}
      </label>

      {/* NativeSelect over the Radix popup on purpose: the card sits flush
          against the composer at the viewport's bottom edge, where the platform
          positions its own option list correctly for free (and stays scrollable
          on touch — the same reason SimpleSelect renders native there). Compact
          overrides shrink it to the card's 11px scale; the coarse-pointer bump
          back to 16px is NativeSelect's own iOS focus-zoom rule, kept. `title`
          mirrors the old question line's tooltip: this control truncates, and
          the path label is the only place a nested destination is spelled out. */}
      <NativeSelect
        id={selectId}
        value={effectiveId}
        onChange={e => setSelectedId(e.target.value)}
        title={selectedLabel}
        className="text-[11px] [@media(pointer:coarse)]:text-base py-1 pl-2"
        wrapperStyle={{ flex: '1 1 auto', minWidth: 60, maxWidth: 240 }}
        data-testid="folder-suggestion-select"
      >
        {options.map(o => (
          <NativeSelectOption key={o.id} value={o.id}>{o.label}</NativeSelectOption>
        ))}
      </NativeSelect>

      <div className="flex items-center gap-1.5 shrink-0 ml-auto">
        <button
          onClick={() => onAccept(effectiveId)}
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
