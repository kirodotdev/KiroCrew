import { useState } from 'react'
import { ShieldAlert, ChevronDown, CheckCircle, Clock } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem
} from './ui/dropdown-menu'

import { i18nT } from '../i18n/t'

/** The lines under an override card's buttons: what the rule that refused the
 *  command blocks, the rule's name, and what allowing it means. `role="alert"`
 *  so a screen reader announces it with the card, since it is the reason the
 *  card asks at all. Placed under the buttons so the menu, which opens upward,
 *  never covers it while the person chooses. */
export function DenyOverrideWarning({ rule, detail }: { rule: string; detail?: string }) {
  return (
    <div role="alert" data-testid="deny-override-warning" className="px-3.5 pb-2.5 -mt-1 text-[12px] text-text select-text">
      <div className="flex items-start gap-1.5 font-medium text-danger">
        <ShieldAlert size={12} className="shrink-0 mt-[3px]" />
        <span className="min-w-0 break-words">{i18nT('components.denyOverride.blocked')}</span>
      </div>
      {detail && <p className="mt-0.5 break-words" data-testid="deny-override-detail">{detail}</p>}
      <p className="mt-0.5 text-muted break-all font-mono text-[11px]">{i18nT('components.denyOverride.rule_label', { rule })}</p>
      <p className="mt-0.5 text-muted">{i18nT('components.denyOverride.warning')}</p>
    </div>
  )
}

/** The two decisions an override card sends; only the owner's dashboard may. */
export type DenyOverrideAction = 'deny_override_once' | 'deny_override_session'

interface DenyOverrideDropdownProps {
  disabled?: boolean
  className?: string
  onAction: (action: DenyOverrideAction) => void
}

/**
 * The allow half of a security-rule override card: one trigger holding both
 * scopes, beside the usual reject control.
 *
 * WHY A DROPDOWN: `AUTOSDE.yaml`'s `max-two-buttons-per-row` caps the row at
 * two controls, and this card already has the reject control. It also keeps
 * the override one deliberate step away: the person opens the menu and picks
 * a scope, rather than meeting a primary button that runs a refused command.
 *
 * Allow once is first because it is the narrower grant.
 */
export default function DenyOverrideDropdown({ disabled, className, onAction }: DenyOverrideDropdownProps) {
  const [open, setOpen] = useState(false)

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button
          disabled={disabled}
          className={className}
          aria-label={i18nT('components.denyOverride.override')}
          data-testid="deny-override-trigger"
        >
          <ShieldAlert size={12} className="shrink-0 text-danger" />{i18nT('components.denyOverride.override')}
          <ChevronDown size={10} className="shrink-0 opacity-70" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="end" className="min-w-[240px] max-w-[min(420px,calc(100vw-2rem))]">
        <DropdownMenuItem
          className="gap-2 text-[12px]"
          onSelect={() => onAction('deny_override_once')}
        >
          <CheckCircle size={12} className="shrink-0 text-warn" />
          <span className="min-w-0">{i18nT('components.denyOverride.allow_once')}</span>
        </DropdownMenuItem>
        <DropdownMenuItem
          className="gap-2 text-[12px]"
          onSelect={() => onAction('deny_override_session')}
        >
          <Clock size={12} className="shrink-0 text-warn" />
          <span className="min-w-0">{i18nT('components.denyOverride.allow_session')}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
