import { useState, type ReactNode } from 'react'
import { Handshake, Shield, ShieldPlus, ShieldCheck, BookOpen, ChevronDown } from 'lucide-react'
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
  /** Catalog key for the read-only tier ("Trust reads"), which grants standing
      approval for read-only commands. Set only by a surface whose pending call
      IS read-only; omitted, the tier does not render. It lives in this menu
      rather than beside it because the approval row is capped at the three
      controls it already carries (`max-two-buttons-per-row`), and a dropdown
      trigger counts as one however many tiers it holds. */
  trustReadsLabelKey?: string
  /** False withholds the session-wide tier, for a card that carries no
      server proof that a standing grant can be recorded. Offering it there
      would name a decision the backend refuses (#5400, #5434, #5486). */
  showTrustAll?: boolean
  onAction: (action: string, pattern?: string) => void
}

export default function TrustDropdown({ fullCommand, baseCommand, isShell, hasCommand = true, disabled, className, trustAllLabelKey, trustReadsLabelKey, showTrustAll = true, onAction }: TrustDropdownProps) {
  const [open, setOpen] = useState(false)

  // Pattern shaping lives in utils/trustPatterns so every surface that offers
  // tiered trust grants an identical scope for the same click.
  const truncated = truncateCommandLabel(fullCommand)
  const basePattern = trustBasePattern(baseCommand)
  const baseLabel = baseCommandLabel(baseCommand)

  // One list, built once, so the count below and the items rendered can never
  // disagree. The command label is interpolated INTO a whole sentence rather
  // than glued between two fragments: word order around a quoted operand
  // differs per language, and a fragment pair can only express the English one.
  const tiers: {
    action: string
    /** Fires this tier's decision. A closure rather than an (action, pattern)
     *  pair so a pattern-less tier calls ``onAction`` with ONE argument, the
     *  shape every consuming surface is written against. */
    fire: () => void
    icon: ReactNode
    /** The menu item's body. `plain` is the same text as one string, for the
     *  collapsed button, whose accessible name must be the whole label. */
    body: ReactNode
    plain: string
  }[] = []
  if (hasCommand) {
    tiers.push({
      action: 'trust_command',
      fire: () => onAction('trust_command', fullCommand),
      icon: <Shield size={12} className="shrink-0 text-accent" />,
      // The untruncated command as a tooltip: this grant is an exact-string
      // match, so the user must be able to read the whole thing before agreeing
      // to it. No `truncate` here on purpose -- CSS ellipsis would clip the tail
      // that `truncateCommandLabel` deliberately preserved, re-colliding two
      // commands that differ only in their filename. The label wraps instead;
      // the menu's own max-width still bounds it.
      body: (
        <span className="min-w-0 break-all" title={fullCommand}>
          <Trans
            i18nKey="components.trustDropdown.trust_this_command"
            values={{ cmd: truncated }}
            components={{ mono: <span className="font-mono" /> }}
          />
        </span>
      ),
      plain: i18nT('components.trustDropdown.trust_this_command', { cmd: truncated }),
    })
  }
  if (hasCommand && isShell) {
    tiers.push({
      action: 'trust_base',
      fire: () => onAction('trust_base', basePattern),
      icon: <ShieldPlus size={12} className="shrink-0 text-ok" />,
      body: (
        <span className="truncate">
          <Trans
            i18nKey="components.trustDropdown.trust_all_base"
            values={{ base: baseLabel }}
            components={{ mono: <span className="font-mono" /> }}
          />
        </span>
      ),
      plain: i18nT('components.trustDropdown.trust_all_base', { base: baseLabel }),
    })
  }
  if (trustReadsLabelKey) {
    tiers.push({
      action: 'trust_reads',
      fire: () => onAction('trust_reads'),
      icon: <BookOpen size={12} className="shrink-0 text-accent" />,
      body: <span className="min-w-0">{i18nT(trustReadsLabelKey)}</span>,
      plain: i18nT(trustReadsLabelKey),
    })
  }
  if (showTrustAll) {
    const label = trustAllLabelKey
      ? i18nT(trustAllLabelKey)
      : i18nT('components.trustDropdown.trust_all_tools')
    tiers.push({
      action: 'trust',
      fire: () => onAction('trust'),
      icon: <ShieldCheck size={12} className="shrink-0 text-warn" />,
      // min-w-0 lets a long scope-qualified label wrap inside the menu's
      // viewport-aware width cap instead of overflowing it.
      body: <span className="min-w-0">{label}</span>,
      plain: label,
    })
  }

  if (tiers.length === 0) return null

  // A ONE-tier menu is not a menu. Cold readers took the lone floating item for
  // a tooltip and the bare "Trust" trigger for something that might already be
  // the grant, so the one control they could safely reach for was the one that
  // said nothing. With a single tier the trigger IS that tier: it carries the
  // tier's own label, so the scope is on the control the user clicks. Two or
  // more tiers keep the disclosure, where the verb plus a chevron reads as
  // "there are choices behind this".
  if (tiers.length === 1) {
    const only = tiers[0]
    return (
      <button
        disabled={disabled}
        className={className}
        onClick={only.fire}
      >
        {only.icon}
        {only.plain}
      </button>
    )
  }

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
        {tiers.map(tier => (
          <DropdownMenuItem
            key={tier.action}
            className="gap-2 text-[12px]"
            onSelect={tier.fire}
          >
            {tier.icon}
            {tier.body}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
