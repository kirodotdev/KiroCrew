import { useRef, useState } from 'react'
import { Ban, XCircle, ChevronDown } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem
} from './ui/dropdown-menu'

import { i18nT } from '../i18n/t'

interface RejectDropdownProps {
  disabled?: boolean
  className?: string
  /** Set when the backend hands a rejection's reason to the model: the menu
   *  then opens with a box for one, sent with either choice. */
  takesReason?: boolean
  /** `reason` is the trimmed text typed in that box, absent when it is empty. */
  onAction: (action: string, reason?: string) => void
}

/** Matches the server's cap (`REJECTION_REASON_MAX_CHARS`). */
const REASON_MAX_CHARS = 1000

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
export default function RejectDropdown({ disabled, className, takesReason = false, onAction }: RejectDropdownProps) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const reasonRef = useRef<HTMLInputElement>(null)
  const firstItemRef = useRef<HTMLDivElement>(null)
  // A reason typed and then left belongs to that opening only: closing the menu
  // drops it, so it never rides a later rejection.
  const onOpenChange = (next: boolean) => {
    setOpen(next)
    if (!next) setReason('')
  }
  const act = (action: string) => {
    const text = takesReason ? reason.trim() : ''
    setReason('')
    onAction(action, text || undefined)
  }

  return (
    <DropdownMenu open={open} onOpenChange={onOpenChange}>
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
        {takesReason && (
          <div className="px-2 pt-1.5 pb-1">
            {/* Typed keys stay in the box: the menu's type-to-select would
                otherwise take each letter as a jump to an item. Escape still
                closes the menu, and ArrowDown moves to the first choice (whose
                ArrowUp comes back here), so the box is reachable by keyboard. */}
            <input
              ref={reasonRef}
              type="text"
              value={reason}
              maxLength={REASON_MAX_CHARS}
              onChange={e => setReason(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Escape') return
                e.stopPropagation()
                if (e.key === 'ArrowDown') {
                  e.preventDefault()
                  firstItemRef.current?.focus()
                }
              }}
              placeholder={i18nT('components.rejectDropdown.reason_placeholder')}
              aria-label={i18nT('components.rejectDropdown.reason_placeholder')}
              className="w-full px-2 py-1 rounded-md bg-bg border border-border text-text text-[12px] font-body placeholder:text-muted focus:outline-none focus:border-border-strong"
            />
          </div>
        )}
        {/* Reject once is FIRST because it is the narrower of the two: the reader
            meets the option that stops one call before the one that stops the
            whole batch. */}
        <DropdownMenuItem
          ref={firstItemRef}
          className="gap-2 text-[12px]"
          onSelect={() => act('rejected_once')}
          onKeyDown={e => {
            if (takesReason && e.key === 'ArrowUp') {
              e.preventDefault()
              reasonRef.current?.focus()
            }
          }}
        >
          <XCircle size={12} className="shrink-0 text-warn" />
          <span className="min-w-0">{i18nT('components.rejectDropdown.reject_once')}</span>
        </DropdownMenuItem>
        <DropdownMenuItem
          className="gap-2 text-[12px]"
          onSelect={() => act('rejected')}
        >
          <Ban size={12} className="shrink-0 text-danger" />
          <span className="min-w-0">{i18nT('components.rejectDropdown.reject_all')}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
