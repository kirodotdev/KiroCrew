import { useId } from 'react'
import { Gauge, Check, ChevronRight, ChevronsUp, ChevronsDown, Minus } from 'lucide-react'
import {
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent,
  DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuItem, DropdownMenuSeparator,
} from './ui/dropdown-menu'
import {
  ContextMenuSub, ContextMenuSubTrigger, ContextMenuSubContent,
  ContextMenuRadioGroup, ContextMenuRadioItem, ContextMenuItem, ContextMenuSeparator,
} from './ui/context-menu'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'
import type { QueuePriority } from '../types'
import type { ErrorReport } from '../utils/errorReport'

import { i18nT } from '../i18n/t'

interface QueuePrioritySubmenuProps {
  /** Which menu family this submenu nests inside -- chooses Radix Dropdown vs Context primitives. */
  readonly variant: 'dropdown' | 'context'
  /** The chat's current agent-queue priority; absent reads as medium. */
  readonly current?: QueuePriority
  readonly error?: string | null
  /** The notice's heading; defaults to the change-failed title. */
  readonly errorTitle?: string
  /** The failure's structured report, so "Ask the agent" carries its endpoint,
   *  status and backend code; the localized line matches no journal entry. */
  readonly errorReport?: ErrorReport
  readonly onPick: (priority: QueuePriority) => void
}

/**
 * "Sub-agent priority" as a native Radix submenu in the shared session menu.
 *
 * When the gateway is at its sub-agent cap, spawns wait in a queue. This picks
 * which tier this chat's waiting spawns (and their nested children) sit in:
 * when a top-level start is free, a High chat's spawns start before any Medium
 * one, and Medium before Low. Starts under the child reserve ignore tiers.
 * Order inside a tier is unchanged (round-robin across chats, FIFO within one).
 * Radio items, so a screen reader announces which tier is in effect.
 */
export default function QueuePrioritySubmenu({ variant, current = 'medium', error, errorTitle, errorReport, onPick }: QueuePrioritySubmenuProps) {
  const Sub = variant === 'context' ? ContextMenuSub : DropdownMenuSub
  const SubTrigger = variant === 'context' ? ContextMenuSubTrigger : DropdownMenuSubTrigger
  const SubContent = variant === 'context' ? ContextMenuSubContent : DropdownMenuSubContent
  const RadioGroup = variant === 'context' ? ContextMenuRadioGroup : DropdownMenuRadioGroup
  const RadioItem = variant === 'context' ? ContextMenuRadioItem : DropdownMenuRadioItem
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem
  const Separator = variant === 'context' ? ContextMenuSeparator : DropdownMenuSeparator
  const errorId = useId()

  /** Keep the menu open after a pick: the moved check is the success feedback,
   *  and a refusal's error notice renders inside this same menu, so closing on
   *  select would hide both. Every explicit pick is sent once from here,
   *  including the tier that is checked: the check follows the gateway's slot
   *  patch, so the checked tier can be older than the owner's last pick (High
   *  picked, then Medium before High's patch lands), and dropping that pick
   *  would leave the gateway on High. */
  const selectHandler = (value: QueuePriority) => (event: Event) => {
    event.preventDefault()
    onPick(value)
  }

  return (
    <Sub>
      <SubTrigger data-testid="session-menu-queue-priority">
        <Gauge size={13} className="shrink-0 text-muted" />
        <span className="flex-1">{i18nT('components.queuePrioritySubmenu.agent_queue_priority')}</span>
        <ChevronRight size={12} className="text-muted" />
      </SubTrigger>
      <SubContent className="min-w-[200px] max-w-[280px]">
        {/* What the tier does, so a pick with nothing waiting still says what it will change. */}
        <p className="px-3 pt-1.5 pb-1 text-[11px] text-muted whitespace-normal" data-testid="queue-priority-caption">
          {i18nT('components.queuePrioritySubmenu.caption')}
        </p>
        {error && (
          <>
            <div className="max-w-[280px] px-2 py-1.5">
              <ErrorNotice
                id={errorId}
                message={error}
                title={errorTitle ?? i18nT('components.queuePrioritySubmenu.change_failed')}
                report={errorReport}
                variant="inline"
                className="flex-wrap"
                messagePlacement="below"
              />
            </div>
            <ErrorNoticeMenuItem Item={Item} message={error} report={errorReport} describedBy={errorId} />
            {/* Keeps the hand-off from reading as a fourth tier. */}
            <Separator />
          </>
        )}
        <RadioGroup value={current}>
          <RadioItem value="high" data-testid="queue-priority-high" onSelect={selectHandler('high')}>
            <ChevronsUp size={13} className="shrink-0 text-muted" />
            <span className="flex-1">{i18nT('components.queuePrioritySubmenu.high')}</span>
            {current === 'high' && <Check size={13} className="ml-auto text-accent shrink-0" aria-hidden />}
          </RadioItem>
          <RadioItem value="medium" data-testid="queue-priority-medium" onSelect={selectHandler('medium')}>
            <Minus size={13} className="shrink-0 text-muted" />
            <span className="flex-1">{i18nT('components.queuePrioritySubmenu.medium_default')}</span>
            {current === 'medium' && <Check size={13} className="ml-auto text-accent shrink-0" aria-hidden />}
          </RadioItem>
          <RadioItem value="low" data-testid="queue-priority-low" onSelect={selectHandler('low')}>
            <ChevronsDown size={13} className="shrink-0 text-muted" />
            <span className="flex-1">{i18nT('components.queuePrioritySubmenu.low')}</span>
            {current === 'low' && <Check size={13} className="ml-auto text-accent shrink-0" aria-hidden />}
          </RadioItem>
        </RadioGroup>
      </SubContent>
    </Sub>
  )
}
