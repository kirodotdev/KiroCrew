/**
 * The one Apps item a selection toolbar offers for a typed source.
 *
 * Shown only while some enabled app has an approved selection action (the
 * dashboard's shared app list, published by `SelectionActionLayer`), so a user
 * with no such app never sees it. A click opens the Apps menu, which reads the
 * app list fresh and lists items only from that read. Nothing here reads the
 * document's selection: the toolbar hands over the text it snapshotted.
 *
 * @module selectionActions/useAppSelectionActions
 */
import { Blocks } from 'lucide-react'
import AppIcon from '../components/AppIcon'
import type { SelectionAction } from '../components/SelectionToolbar'
import { i18nT } from '../i18n/t'
import type { ResolvedSelectionAction } from './selectionActionRegistry'
import { openSelectionMenu, useSelectionActionsAvailable } from './selectionActionStore'
import type { SelectionSourceKind } from './selectionSources'

export function itemIcon(item: ResolvedSelectionAction) {
  return item.icon ? <AppIcon icon={item.icon} size={12} /> : <Blocks size={12} />
}

/** The toolbar's Apps item for `source`, or nothing when no app offers one. */
export function useAppSelectionActions(source: SelectionSourceKind): SelectionAction[] {
  const available = useSelectionActionsAvailable()
  if (!available) return []
  return [{
    id: 'app-items',
    icon: <Blocks size={12} />,
    label: i18nT('components.selectionActions.apps'),
    hint: i18nT('components.selectionActions.apps_hint'),
    onClick: (text, rect) => openSelectionMenu(source, text, rect),
  }]
}
