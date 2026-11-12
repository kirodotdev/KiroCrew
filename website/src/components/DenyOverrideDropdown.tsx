import { useState } from 'react'
import { ShieldAlert, ChevronDown, CheckCircle, Clock } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem
} from './ui/dropdown-menu'

import { i18nT } from '../i18n/t'

/** One plain-language line on what a rule FAMILY protects, keyed by the slug the
 *  server sends (`deny_override_family`, from `dashboard.deny_override
 *  .deny_key_family`). Literal keys, so the catalog checks see every one. A slug
 *  this map does not know, or none, gets the generic line. */
export function denyOverrideFamilyLine(family: string | undefined): string {
  switch (family) {
    case 'git_publish': return i18nT('components.denyOverride.family.git_publish')
    case 'self_protection': return i18nT('components.denyOverride.family.self_protection')
    case 'sandbox_escape': return i18nT('components.denyOverride.family.sandbox_escape')
    case 'credentials': return i18nT('components.denyOverride.family.credentials')
    case 'file_upload': return i18nT('components.denyOverride.family.file_upload')
    case 'remote_control': return i18nT('components.denyOverride.family.remote_control')
    case 'cloud': return i18nT('components.denyOverride.family.cloud')
    case 'wipe': return i18nT('components.denyOverride.family.wipe')
    case 'permissions': return i18nT('components.denyOverride.family.permissions')
    case 'discard_changes': return i18nT('components.denyOverride.family.discard_changes')
    case 'download_run': return i18nT('components.denyOverride.family.download_run')
    case 'database': return i18nT('components.denyOverride.family.database')
    default: return i18nT('components.denyOverride.family.generic')
  }
}

/** The "Allow for this session" row, naming what it lets through for the rest of the
 *  chat. It opens with the verb and says the rule stops blocking those commands:
 *  the grant waives this one rule, not its whole family, so the family (the same
 *  slug as the warning line) only describes the commands this rule blocks. A slug this map does not know, or none (a
 *  user rule, a rule no family covers), gets the generic row. Literal keys, so
 *  the catalog checks see every one. */
export function denyOverrideSessionLabel(family: string | undefined): string {
  switch (family) {
    case 'git_publish': return i18nT('components.denyOverride.session.git_publish')
    case 'self_protection': return i18nT('components.denyOverride.session.self_protection')
    case 'sandbox_escape': return i18nT('components.denyOverride.session.sandbox_escape')
    case 'credentials': return i18nT('components.denyOverride.session.credentials')
    case 'file_upload': return i18nT('components.denyOverride.session.file_upload')
    case 'remote_control': return i18nT('components.denyOverride.session.remote_control')
    case 'cloud': return i18nT('components.denyOverride.session.cloud')
    case 'wipe': return i18nT('components.denyOverride.session.wipe')
    case 'permissions': return i18nT('components.denyOverride.session.permissions')
    case 'discard_changes': return i18nT('components.denyOverride.session.discard_changes')
    case 'download_run': return i18nT('components.denyOverride.session.download_run')
    case 'database': return i18nT('components.denyOverride.session.database')
    default: return i18nT('components.denyOverride.session.generic')
  }
}

/** The lines under an override card's buttons: what the rule protects, in one
 *  plain sentence in the person's language, that the command did not run, and
 *  the refused command itself. The rule's own expert description and its id sit
 *  behind "Details": they are for someone who knows the catalog, and the
 *  description is not translated. How far an allow reaches is said once, on
 *  each menu row, so no line here restates one scope. `role="alert"` so a screen
 *  reader announces it with the card, since it is the reason the card asks at
 *  all. Placed under the buttons so the menu, which opens upward, never covers
 *  it while the person chooses. */
export function DenyOverrideWarning({ rule, family, detail, command }: { rule: string; family?: string; detail?: string; command?: string }) {
  return (
    <div role="alert" data-testid="deny-override-warning" className="px-3.5 pb-2.5 -mt-1 text-[12px] text-text select-text">
      <div className="flex items-start gap-1.5 font-medium text-warn">
        <ShieldAlert size={12} className="shrink-0 mt-[3px]" />
        <span className="min-w-0 break-words" data-testid="deny-override-protects">{denyOverrideFamilyLine(family)}</span>
      </div>
      <p className="mt-0.5 break-words">{i18nT('components.denyOverride.not_run')}</p>
      {command && (
        <div className="mt-1" data-testid="deny-override-command">
          <span className="text-muted">{i18nT('components.denyOverride.command_label')}</span>
          {/* Bounded and scrollable: a long command must not push the card's
              buttons out of the pane. The whole command stays readable, and
              the tab stop lets a keyboard user scroll it. */}
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard users scroll the exact command. */}
          <code tabIndex={0} role="region" aria-label={i18nT('components.denyOverride.command_label')} className="mt-0.5 block max-h-[30vh] overflow-y-auto whitespace-pre-wrap break-all rounded-md bg-bg-hover px-2 py-1 font-mono text-[12px]">{command}</code>
        </div>
      )}
      <details className="mt-1 text-muted" data-testid="deny-override-details">
        <summary className="cursor-pointer select-none w-fit">{i18nT('components.denyOverride.details')}</summary>
        {detail && <p className="mt-0.5 break-words" data-testid="deny-override-detail">{detail}</p>}
        <p className="mt-0.5 break-all font-mono text-[11px]">{i18nT('components.denyOverride.rule_label', { rule })}</p>
      </details>
    </div>
  )
}

/** The two decisions an override card sends; only the owner's dashboard may. */
export type DenyOverrideAction = 'deny_override_once' | 'deny_override_session'

interface DenyOverrideDropdownProps {
  disabled?: boolean
  className?: string
  /** The rule's family slug; picks what the "Allow for this session" row names. */
  family?: string
  onAction: (action: DenyOverrideAction) => void
}

/**
 * The allow half of a security-rule override card: one trigger holding both
 * scopes, beside the usual reject control. The trigger only opens the menu, so
 * its label asks the person to choose and carries a chevron; nothing runs until
 * a row in the menu is picked.
 *
 * WHY A DROPDOWN: `AUTOSDE.yaml`'s `max-two-buttons-per-row` caps the row at
 * two controls, and this card already has the reject control. It also keeps
 * the override one deliberate step away: the person opens the menu and picks
 * a scope, rather than meeting a primary button that runs a refused command.
 *
 * Allow once is first because it is the narrower grant.
 */
export default function DenyOverrideDropdown({ disabled, className, family, onAction }: DenyOverrideDropdownProps) {
  const [open, setOpen] = useState(false)

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button
          disabled={disabled}
          className={className}
          aria-label={i18nT('components.denyOverride.override')}
          aria-haspopup="menu"
          data-testid="deny-override-trigger"
        >
          {/* Warn, not danger: nothing has failed (the command has not run),
              so this trigger and the warning line under the buttons both use
              the warn tone. */}
          <ShieldAlert size={12} className="shrink-0 text-warn" />{i18nT('components.denyOverride.override')}
          <ChevronDown size={12} className="shrink-0" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="end" className="min-w-[240px] max-w-[min(420px,calc(100vw-2rem))]">
        <DropdownMenuItem onSelect={() => onAction('deny_override_once')}>
          <CheckCircle size={12} className="shrink-0 text-warn" />
          <span className="min-w-0 text-[12px]">{i18nT('components.denyOverride.allow_once')}</span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onAction('deny_override_session')}>
          <Clock size={12} className="shrink-0 text-warn" />
          <span className="min-w-0 text-[12px]">{denyOverrideSessionLabel(family)}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
