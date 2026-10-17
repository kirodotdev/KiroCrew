import type { ComponentType, ReactNode } from 'react'
import { GitMerge } from 'lucide-react'
import { useAppSelector } from '../store'
import { useMergeBackDialog } from '../hooks/useMergeBackDialog'
import { i18nT } from '../i18n/t'
import { mergeBackParentMenuReason } from '../utils/mergeBackParent'

type MenuItemComponent = ComponentType<{
  className?: string
  disabled?: boolean
  onSelect?: (event: Event) => void
  children?: ReactNode
  'data-testid'?: string
}>

export { parentTakesTurnsOutsideDashboard } from '../utils/mergeBackParent'

/**
 * "Merge into parent…" on a fork's session menu. Opens the one
 * `MergeBackDialog`.
 *
 * Renders nothing for a chat that is not a fork, and nothing outside ChatPage's
 * provider. Disabled for the refusals a person can see coming: an incognito
 * or temporary fork (it derives nothing from itself), a parent whose turns can
 * run outside the dashboard (named by what it is, where the key says: a Slack
 * chat, a scheduled job), and a fork still running a turn (there is no end
 * to summarize yet). Every other refusal depends on the
 * parent or on what it already has, and the dialog asks the gateway for those.
 */
export default function MergeBackMenuItem({ Item, slotKey }: { Item: MenuItemComponent; slotKey: string }) {
  const dialog = useMergeBackDialog()
  const slot = useAppSelector(s => s.dashboard.slots.find(x => x.key === slotKey))
  if (!dialog || !slot?.forked_from) return null
  const reason = slot.memory_mode === 'incognito'
    ? i18nT('components.mergeBackMenuItem.incognito')
    : slot.memory_mode === 'temporary'
      ? i18nT('components.mergeBackMenuItem.temporary')
      : mergeBackParentMenuReason(slot.forked_from)
        || (slot.running ? i18nT('components.mergeBackMenuItem.running') : '')
  return (
    <Item disabled={!!reason} onSelect={() => dialog.open(slotKey)} data-testid="merge-back">
      <GitMerge size={13} className="shrink-0 text-muted" /> {i18nT('components.mergeBackMenuItem.merge_into_parent')}
      {/* Inline, because a disabled Radix item drops pointer events, so a hover
       *  title could never explain the grey row. */}
      {reason && <span className="ml-auto text-[10px] text-muted">{reason}</span>}
    </Item>
  )
}
