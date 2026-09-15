import { useState } from 'react'
import { Ban, XCircle, ChevronDown } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem
} from './ui/dropdown-menu'

import { i18nT } from '../i18n/t'

interface RejectDropdownProps {
  disabled?: boolean
  className?: string
  onAction: (action: string) => void
}

/**
 * The rejection half of the approval prompt: one trigger holding both tiers.
 *
 * WHY A DROPDOWN AND NOT TWO BUTTONS: `AUTOSDE.yaml`'s
 * `max-two-buttons-per-row` caps a horizontal action group at two controls, and
 * the approval row already sits at the count that rule exempts as pre-existing
 * (Allow once, Trust reads, Trust, reject) — a row at 3+ "must not grow". A
 * trigger counts as ONE however many items it holds, so collapsing the two
 * rejections here adds a choice without adding a control. It also mirrors the
 * approve side's shape: one plain button for the one-shot action, one dropdown
 * for the tiers.
 *
 * WHY "Reject" AND NOT A NEW VERB: the trigger reuses the SAME catalog key the
 * single button used, so this surface, the spawn-approval bar in the same footer
 * and `ApprovalCard` keep one verb for one operation. A new verb here would ship
 * the identical capability plus a divergence to reconcile later.
 *
 * The menu ranks the two explicitly, which is the thing peer buttons cannot do
 * — the rule's own rationale for the cap.
 */
export default function RejectDropdown({ disabled, className, onAction }: RejectDropdownProps) {
  const [open, setOpen] = useState(false)

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button
          disabled={disabled}
          className={className}
          aria-label={i18nT('components.chatInput.reject')}
        >
          <Ban size={12} className="shrink-0" />{i18nT('components.chatInput.reject')}
          <ChevronDown size={10} className="shrink-0 opacity-70" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="end" className="min-w-[240px] max-w-[min(420px,calc(100vw-2rem))]">
        {/* Reject once is FIRST because it is the narrower of the two: the reader
            meets the option that stops one call before the one that stops the
            whole batch. */}
        <DropdownMenuItem
          className="gap-2 text-[12px]"
          onSelect={() => onAction('rejected_once')}
        >
          <XCircle size={12} className="shrink-0 text-warn" />
          <span className="min-w-0">{i18nT('components.rejectDropdown.reject_once')}</span>
        </DropdownMenuItem>
        <DropdownMenuItem
          className="gap-2 text-[12px]"
          onSelect={() => onAction('rejected')}
        >
          <Ban size={12} className="shrink-0 text-danger" />
          <span className="min-w-0">{i18nT('components.rejectDropdown.reject_all')}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
