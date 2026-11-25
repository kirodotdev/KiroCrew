import { ariaKeyshortcutsFor } from '../../hooks/useNavShortcutHint'
import { i18nT } from '../../i18n/t'
import { platformShortcut } from '../../utils/platform'

/** The way in to a chat's history search names the find shortcut
 *  (`useMessageSearch`: ⌘F on a Mac, Ctrl+F elsewhere): `chord` is the menu
 *  row's accelerator text, `tip` a button's tooltip, and `keyshortcuts` the
 *  chord for `aria-keyshortcuts`, spelled by the shared helper. */
export function historySearchHint(): { tip: string; chord: string; keyshortcuts: string } {
  const chord = platformShortcut('Cmd+F')
  return {
    tip: i18nT('components.chatHistorySearch.open_hint', { shortcut: chord }),
    chord,
    keyshortcuts: ariaKeyshortcutsFor({ id: 'find-in-chat', key: 'f', meta: true, group: 'actions' }),
  }
}
