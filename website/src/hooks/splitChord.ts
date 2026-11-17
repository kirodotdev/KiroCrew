import { isMacPlatform } from './useKeyboardShortcuts'

/**
 * The split-view shortcut: ⌘D on a Mac, Ctrl+D or ⌘D elsewhere, with no Shift
 * or Alt. On a Mac, Ctrl+D is a text field's delete-forward key, so it never
 * splits there. Both the single-chat listener and the grid read this one test.
 */
export function isSplitChord(e: KeyboardEvent): boolean {
  const primary = isMacPlatform() ? e.metaKey : e.metaKey || e.ctrlKey
  return primary && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'd'
}
