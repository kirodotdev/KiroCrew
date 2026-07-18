import { useState } from 'react'
import { Handshake, Shield, ShieldPlus, ShieldCheck, ChevronDown } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem
} from './ui/dropdown-menu'

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

  const truncated = fullCommand.length > 30 ? fullCommand.slice(0, 30) + '…' : fullCommand
  const basePattern = baseCommand.split(',').map(b => b.trim() + ' *').join(',')
  const baseLabel = baseCommand.split(',').join(', ')

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button disabled={disabled} className={className}>
          <Handshake size={12} className="shrink-0" />Trust<ChevronDown size={10} className="shrink-0 opacity-70" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="end" className="min-w-[220px] max-w-[450px]">
        <DropdownMenuItem
          className="gap-2 text-[12px]"
          onSelect={() => onAction('trust_command', fullCommand)}
        >
          <Shield size={12} className="shrink-0 text-accent" />
          <span className="truncate">Trust &ldquo;<span className="font-mono">{truncated}</span>&rdquo;</span>
        </DropdownMenuItem>
        {isShell && (
          <DropdownMenuItem
            className="gap-2 text-[12px]"
            onSelect={() => onAction('trust_base', basePattern)}
          >
            <ShieldPlus size={12} className="shrink-0 text-ok" />
            <span className="truncate">Trust all &ldquo;<span className="font-mono">{baseLabel}</span>&rdquo; commands</span>
          </DropdownMenuItem>
        )}
        <DropdownMenuItem
          className="gap-2 text-[12px]"
          onSelect={() => onAction('trust')}
        >
          <ShieldCheck size={12} className="shrink-0 text-warn" />
          <span>Trust all tools</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
