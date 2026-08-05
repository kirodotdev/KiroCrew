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
  disabled?: boolean
  className?: string
  onAction: (action: string, pattern?: string) => void
}

export default function TrustDropdown({ fullCommand, baseCommand, isShell, disabled, className, onAction }: TrustDropdownProps) {
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
      <DropdownMenuContent side="top" align="end" className="min-w-[220px] max-w-[450px]">
        <DropdownMenuItem
          className="gap-2 text-[12px]"
          onSelect={() => onAction('trust_command', fullCommand)}
        >
          <Shield size={12} className="shrink-0 text-accent" />
          <span className="truncate">
            <Trans
              i18nKey="components.trustDropdown.trust_this_command"
              values={{ cmd: truncated }}
              components={{ mono: <span className="font-mono" /> }}
            />
          </span>
        </DropdownMenuItem>
        {isShell && (
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
          <span>{i18nT('components.trustDropdown.trust_all_tools')}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
