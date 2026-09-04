import { useState } from 'react'
import { Handshake, Shield, ShieldPlus, ShieldCheck, ChevronDown } from 'lucide-react'
import { Trans } from 'react-i18next'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem
} from './ui/dropdown-menu'
import { baseCommandLabel, trustBasePattern, truncateCommandLabel } from '../utils/trustPatterns'

import { i18nT } from '../i18n/t'
interface TrustDropdownProps {
  fullCommand: string
  baseCommand: string
  isShell: boolean
  /** Whether the approval carries an actual tool command. Agent-role channel
      approvals pass false because command-scoped tiers would describe the
      wrong thing and emit decisions (`trust_command` / `trust_base`) the
      channel backend refuses; shell-command channel approvals pass true.
      Explicit rather than inferred from `fullCommand`, so command-bearing
      surfaces keep every tier no matter what the command text looks like. */
  hasCommand?: boolean
  disabled?: boolean
  className?: string
  // Overrides the catalog key for the "trust all tools" option. The default
  // label reads as session-scoped; a surface whose `trust` decision grants
  // something wider (e.g. channel-wide and persisted to disk) must pass a key
  // that names the actual grant, so consent matches what is being consented to.
  trustAllLabelKey?: string
  onAction: (action: string, pattern?: string) => void
}

export default function TrustDropdown({ fullCommand, baseCommand, isShell, hasCommand = true, disabled, className, trustAllLabelKey, onAction }: TrustDropdownProps) {
  const [open, setOpen] = useState(false)

  // Pattern shaping lives in utils/trustPatterns so every surface that offers
  // tiered trust grants an identical scope for the same click.
  const truncated = truncateCommandLabel(fullCommand)
  const basePattern = trustBasePattern(baseCommand)
  const baseLabel = baseCommandLabel(baseCommand)

  // The command label is interpolated INTO a whole sentence rather than glued
  // between two fragments: word order around a quoted operand differs per
  // language, and a fragment pair can only express the English one.
  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button disabled={disabled} className={className}>
          <Handshake size={12} className="shrink-0" />{i18nT('components.trustDropdown.trust')}<ChevronDown size={10} className="shrink-0 opacity-70" />
        </button>
      </DropdownMenuTrigger>
      {/* The width cap is viewport-aware: a flat max-w overflows a narrow screen
          (measured at 320px, the menu reached 440px and ran off the right edge),
          which hides the very label this menu exists to make readable. */}
      <DropdownMenuContent side="top" align="end" className="min-w-[220px] max-w-[min(450px,calc(100vw-2rem))]">
        {hasCommand && (
          <DropdownMenuItem
            className="gap-2 text-[12px]"
            onSelect={() => onAction('trust_command', fullCommand)}
          >
            <Shield size={12} className="shrink-0 text-accent" />
            {/* The untruncated command as a tooltip: this grant is an exact-string
                match, so the user must be able to read the whole thing before
                agreeing to it. No `truncate` here on purpose -- CSS ellipsis would
                clip the tail that `truncateCommandLabel` deliberately preserved,
                re-colliding two commands that differ only in their filename. The
                label wraps instead; the menu's own max-width still bounds it. */}
            <span className="min-w-0 break-all" title={fullCommand}>
              <Trans
                i18nKey="components.trustDropdown.trust_this_command"
                values={{ cmd: truncated }}
                components={{ mono: <span className="font-mono" /> }}
              />
            </span>
          </DropdownMenuItem>
        )}
        {hasCommand && isShell && (
          <DropdownMenuItem
            className="gap-2 text-[12px]"
            onSelect={() => onAction('trust_base', basePattern)}
          >
            <ShieldPlus size={12} className="shrink-0 text-ok" />
            <span className="truncate">
              <Trans
                i18nKey="components.trustDropdown.trust_all_base"
                values={{ base: baseLabel }}
                components={{ mono: <span className="font-mono" /> }}
              />
            </span>
          </DropdownMenuItem>
        )}
        <DropdownMenuItem
          className="gap-2 text-[12px]"
          onSelect={() => onAction('trust')}
        >
          <ShieldCheck size={12} className="shrink-0 text-warn" />
          {/* min-w-0 lets a long scope-qualified label wrap inside the menu's
              viewport-aware width cap instead of overflowing it. */}
          <span className="min-w-0">{trustAllLabelKey ? i18nT(trustAllLabelKey) : i18nT('components.trustDropdown.trust_all_tools')}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
