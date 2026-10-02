import { useId } from 'react'
import { Gauge, Check, ChevronRight, ChevronsUp, ChevronsDown, Minus } from 'lucide-react'
import {
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent,
  DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuItem,
} from './ui/dropdown-menu'
import {
  ContextMenuSub, ContextMenuSubTrigger, ContextMenuSubContent,
  ContextMenuRadioGroup, ContextMenuRadioItem, ContextMenuItem,
} from './ui/context-menu'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'
import type { QueuePriority } from '../types'

import { i18nT } from '../i18n/t'
const QUEUE_PRIORITIES: readonly QueuePriority[] = ['low', 'medium', 'high']

interface QueuePrioritySubmenuProps {
  /** Which menu family this submenu nests inside -- chooses Radix Dropdown vs Context primitives. */
  readonly variant: 'dropdown' | 'context'
  /** The chat's current agent-queue priority; absent reads as medium. */
  readonly current?: QueuePriority
  readonly error?: string | null
  readonly onPick: (priority: QueuePriority) => void
}

function isQueuePriority(value: string): value is QueuePriority {
  return (QUEUE_PRIORITIES as readonly string[]).includes(value)
}

/**
 * "Sub-agent priority" as a native Radix submenu in the shared session menu.
 *
 * When the gateway is at its sub-agent cap, spawns wait in a queue. This picks
 * which tier this chat's waiting spawns (and their nested children) sit in:
 * a High chat's spawns start before any Medium one, and Medium before Low.
 * Order inside a tier is unchanged (round-robin across chats, FIFO within one).
 * Radio items, so a screen reader announces which tier is in effect.
 */
/** Keep the menu open after a pick: the moved check is the success feedback,
 *  and a refusal's error notice renders inside this same menu, so closing on
 *  select would hide both. */
function keepOpen(event: Event) {
  event.preventDefault()
}

export default function QueuePrioritySubmenu({ variant, current = 'medium', error, onPick }: QueuePrioritySubmenuProps) {
  const Sub = variant === 'context' ? ContextMenuSub : DropdownMenuSub
  const SubTrigger = variant === 'context' ? ContextMenuSubTrigger : DropdownMenuSubTrigger
  const SubContent = variant === 'context' ? ContextMenuSubContent : DropdownMenuSubContent
  const RadioGroup = variant === 'context' ? ContextMenuRadioGroup : DropdownMenuRadioGroup
  const RadioItem = variant === 'context' ? ContextMenuRadioItem : DropdownMenuRadioItem
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem
  const errorId = useId()

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
                title={i18nT('components.queuePrioritySubmenu.change_failed')}
                variant="inline"
                className="flex-wrap"
                messagePlacement="below"
              />
            </div>
            <ErrorNoticeMenuItem Item={Item} message={error} describedBy={errorId} />
          </>
        )}
        <RadioGroup value={current} onValueChange={(value: string) => { if (isQueuePriority(value) && value !== current) onPick(value) }}>
          <RadioItem value="high" data-testid="queue-priority-high" onSelect={keepOpen}>
            <ChevronsUp size={13} className="shrink-0 text-muted" />
            <span className="flex-1">{i18nT('components.queuePrioritySubmenu.high')}</span>
            {current === 'high' && <Check size={13} className="ml-auto text-accent shrink-0" aria-hidden />}
          </RadioItem>
          <RadioItem value="medium" data-testid="queue-priority-medium" onSelect={keepOpen}>
            <Minus size={13} className="shrink-0 text-muted" />
            <span className="flex-1">{i18nT('components.queuePrioritySubmenu.medium_default')}</span>
            {current === 'medium' && <Check size={13} className="ml-auto text-accent shrink-0" aria-hidden />}
          </RadioItem>
          <RadioItem value="low" data-testid="queue-priority-low" onSelect={keepOpen}>
            <ChevronsDown size={13} className="shrink-0 text-muted" />
            <span className="flex-1">{i18nT('components.queuePrioritySubmenu.low')}</span>
            {current === 'low' && <Check size={13} className="ml-auto text-accent shrink-0" aria-hidden />}
          </RadioItem>
        </RadioGroup>
      </SubContent>
    </Sub>
  )
}
