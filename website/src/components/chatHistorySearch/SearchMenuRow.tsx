import { Search } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { DropdownMenuItem } from '../ui/dropdown-menu'
import { historySearchHint } from './searchShortcut'

/** A header menu's row that opens the chat's history search, with the find
 *  shortcut on its right. Header rows hold at most two peer controls
 *  (`max-two-buttons-per-row`), so Search is a row in a menu the header has. */
export default function SearchMenuRow({ onSelect, testId }: { onSelect: () => void; testId: string }) {
  const hint = historySearchHint()
  return (
    <DropdownMenuItem onSelect={onSelect} aria-keyshortcuts={hint.keyshortcuts} data-testid={testId}>
      <Search size={13} className="shrink-0 text-muted" />
      <span className="flex-1">{i18nT('components.chatHistorySearch.open')}</span>
      <span className="ps-4 text-[11px] text-muted" aria-hidden>{hint.chord}</span>
    </DropdownMenuItem>
  )
}
