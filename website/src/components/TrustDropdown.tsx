import { Fragment, useId, useState, type ComponentPropsWithoutRef, type ReactNode } from 'react'
import { Handshake, Shield, ShieldPlus, ShieldCheck, BookOpen, ChevronDown } from 'lucide-react'
import { Trans } from 'react-i18next'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuRadioItem, DropdownMenuRadioSegments, DropdownMenuSeparator,
} from './ui/dropdown-menu'
import { baseCommandLabel, trustBasePattern } from '../utils/trustPatterns'
import { grantEndsAt, grantTtlSeconds, type GrantDuration } from '../utils/grantDuration'
import { fmtTime } from '../i18n/format'

import { i18nT } from '../i18n/t'
import { guideTrustRoot, GuideTrustRootProvider } from '../guide/trustRoot'
import { CuedMenuContent, CuedScrollBox, PendingCallItem, pageScrollBox, type PendingCall } from './chat-input/CuedScrollBox'
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
  /** False withholds the base tier on the server's own word that no base is
      derivable from the pending command (compound, quoted, env-prefixed): the
      endpoint would refuse the grant, and a first-token label would describe
      a scope it never records. Default true keeps every other surface's
      shell-title derivation. */
  showTrustBase?: boolean
  /** Catalog keys for the two per-command tiers, for a surface whose grants
      are scoped differently from chat's (a channel grant is agent-scoped and
      lasts until restart). Same contract as `trustAllLabelKey`: the label must
      name the actual grant. Both keys place the command or base with a
      `<mono/>` slot, which this component fills, and take `labelValues`. */
  trustCommandLabelKey?: string
  trustBaseLabelKey?: string
  /** The command tier's scope is a host name: its chip moves to the next line
      whole and breaks only when it cannot fit one, instead of mid-word. */
  scopeIsHost?: boolean
  /** Extra interpolation values for the tier labels (e.g. `role`). */
  labelValues?: Record<string, string>
  /** Gives the command-scoped tiers a lifetime: the menu opens with one
      duration choice (an hour, the rest of today, this session) above one row
      per scope, and each command row names the lifetime it would grant, so no
      row reads as permanent. Only a surface whose endpoint records
      `ttl_seconds` on the grant sets it; elsewhere the menu keeps its plain
      rows, because a label naming a lifetime the backend never records would
      be consent to a grant that is not the one made.

      The exact-command row prints `fullCommand` whole itself, so the string
      being consented to is read where it is chosen (see `TimedTrustMenu`). */
  timedGrants?: boolean
  /** The server-derived `@server/tool` an approval card may grant for good
      ("Always allow this tool"). Fired as `always_allow_tool` with this ref as
      its pattern, which the server only checks against the card's own. */
  alwaysAllowTool?: string
  /** Why that grant is refused for this card; renders the row disabled with it. */
  alwaysAllowRefusal?: string
  /** With `timedGrants`: what the exact-match row grants. A shell command is
      an exact "command", shown on the card; any other tool's grant is a
      "call" scope, the tool (and file) the server keys it on, which the row
      prints itself. */
  commandNoun?: 'command' | 'call'
  /** With `timedGrants`: the pending call as the card shows it, its command (if
      any) and the rest of its input. Every row here also approves that call, and
      the open menu covers the card, so the menu prints it at its top unless the
      exact-command row already prints all of it. */
  pendingCall?: PendingCall
  /** Passed to the menu. The approval card sets it, so on touch an outside tap
      only closes the menu instead of also reaching the control under it (see
      `DropdownMenu`). */
  modal?: boolean
  /** `ttlSeconds` is present only for a timed grant, and is the grant's
      lifetime in whole seconds. */
  onAction: (action: string, pattern?: string, ttlSeconds?: number) => void
}

/** How long a command-scoped grant lasts: one of the two timed choices, or the
 *  rest of the chat session (no `ttl_seconds`, the grant the row always made). */
type Lifetime = GrantDuration | 'session'

/** Menu order: shortest first, so the widest lifetime is the last one read. */
const LIFETIMES: readonly Lifetime[] = ['hour', 'today', 'session']

/** Whole-sentence keys per scope and lifetime. A scope and a lifetime are never
 *  glued from two fragments: where the lifetime sits around the scope differs
 *  per language, and a fragment pair can only express the English order. Flat
 *  maps of literal keys, indexed directly, so the key-reference gate can check
 *  each one exists. */
const COMMAND_SCOPE_KEYS: Record<Lifetime, string> = {
  hour: 'components.trustDropdown.scope_command_hour',
  today: 'components.trustDropdown.scope_command_today',
  session: 'components.trustDropdown.scope_command_session',
}

const CALL_SCOPE_KEYS: Record<Lifetime, string> = {
  hour: 'components.trustDropdown.scope_call_hour',
  today: 'components.trustDropdown.scope_call_today',
  session: 'components.trustDropdown.scope_call_session',
}

const BASE_SCOPE_KEYS: Record<Lifetime, string> = {
  hour: 'components.trustDropdown.scope_base_hour',
  today: 'components.trustDropdown.scope_base_today',
  session: 'components.trustDropdown.scope_base_session',
}

/** The rows the duration choice applies to: only these grants record a lifetime. */
const TIMED_ROWS = new Set(['trust_command', 'trust_base'])
/** The rows whose grant always lasts the session, whatever the duration says. */
const SESSION_ROWS = new Set(['trust_reads', 'trust'])

const DURATION_KEYS: Record<Exclude<Lifetime, 'hour'>, string> = {
  today: 'components.trustDropdown.duration_today',
  session: 'components.trustDropdown.duration_session',
}

/** One grant row, at the size both shapes of the menu share. */
function TrustRow(props: ComponentPropsWithoutRef<typeof DropdownMenuItem>) {
  // PageUp / PageDown page through a row's own bounded box (the exact command)
  // before they move between rows.
  return <DropdownMenuItem className="gap-2 text-[12px]" onKeyDown={pageScrollBox} {...props} />
}

/** The always-allow row's body, in every shape: the `@server/tool` ref it grants
 *  for good, whole, or the reason it is refused. The ref is printed here because
 *  the card chips that name the tool sit under the open menu. */
function AlwaysAllowBody({ tool, refusal }: { tool?: string; refusal?: string }) {
  return (
    <span className="min-w-0">
      {i18nT('components.trustDropdown.always_allow_this_tool')}
      {tool
        ? <> <span data-testid="trust-always-ref" className="bg-bg-elevated px-1.5 py-0.5 rounded font-mono break-all border border-border">{tool}</span></>
        : refusal ? <span className="block text-muted text-[11px]">{refusal}</span> : null}
    </span>
  )
}

export default function TrustDropdown(props: TrustDropdownProps) {
  // Two shapes of one menu. Which one is the surface's choice, made once and
  // never changed for a mounted card, so each shape owns its own state.
  return props.timedGrants ? <TimedTrustMenu {...props} /> : <PlainTrustMenu {...props} />
}

function PlainTrustMenu({ fullCommand, baseCommand, isShell, hasCommand = true, disabled, className, trustAllLabelKey, trustReadsLabelKey, showTrustAll = true, showTrustBase = true, trustCommandLabelKey, trustBaseLabelKey, scopeIsHost = false, labelValues, alwaysAllowTool, alwaysAllowRefusal, modal, onAction }: TrustDropdownProps) {
  const [open, setOpen] = useState(false)

  // Pattern shaping lives in utils/trustPatterns so every surface that offers
  // tiered trust grants an identical scope for the same click.
  const basePattern = trustBasePattern(baseCommand)
  const baseLabel = baseCommandLabel(baseCommand)
  const commandKey = trustCommandLabelKey ?? 'components.trustDropdown.trust_this_command'
  const baseKey = trustBaseLabelKey ?? 'components.trustDropdown.trust_all_base'

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
    /** The menu item's body, and the whole label of the collapsed button. */
    body: ReactNode
    /** A row shown with its reason but not choosable. */
    disabled?: boolean
  }[] = []
  if (hasCommand) {
    tiers.push({
      action: 'trust_command',
      fire: () => onAction('trust_command', fullCommand),
      icon: <Shield size={12} className="shrink-0 text-accent" />,
      // The WHOLE command, never a shortened one. This grant is an exact-STRING
      // match, so the label is not a description of the scope -- it IS the
      // scope. Any elision leaves two commands that differ only inside the cut
      // rendering identically, and the `title` below was the only way back to
      // the rest: `title` needs a pointer, so on touch the elided label was the
      // entire basis for the grant (#4700). No `truncate` either -- a CSS
      // ellipsis would clip what the string kept. The label wraps instead, over
      // as many lines as it needs: `break-all` breaks an unspaced run (a sha, a
      // base64 argument), the menu's viewport-aware max-width bounds the width,
      // and DropdownMenuContent's own max-height + `overflow-y-auto` bound the
      // height, so even a pathological command scrolls inside the menu rather
      // than running off screen. `title` stays for the pointer user, who gets
      // the command as one unwrapped line.
      //
      // `whitespace-pre-wrap` because HTML's default `white-space: normal`
      // COLLAPSES runs of whitespace, which is the same defect as an elision:
      // `grep "a  b" f` would render as `grep "a b" f` and ask for consent to a
      // string the user never saw. `break-all` governs where a word may break,
      // not whether spaces survive, so it does not cover this.
      //
      // `break-all` sits on the COMMAND span alone. The words around it are
      // prose in the reader's language, and prose that may break between any
      // two letters ("until Kiro Crew rest / arts") is prose the reader has to
      // reassemble on the consent surface; those words wrap at spaces, and
      // only the command -- which may have none -- breaks anywhere.
      // No `aria-label` beside the visible text: the row's accessible name is
      // its own content, which is now the whole command, so an added label
      // could only duplicate it -- and would silently stop matching the visible
      // one the day either changes (WCAG 2.5.3).
      //
      // The command is the <mono/> chip's child, never a value: <Trans> parses
      // its string as markup, so a value would be parsed with it.
      body: (
        <span className="min-w-0 whitespace-pre-wrap" title={fullCommand}>
          <Trans
            i18nKey={commandKey}
            values={labelValues}
            components={{ mono: <span className={`bg-bg-elevated px-1.5 py-0.5 rounded font-mono border border-border ${scopeIsHost ? 'break-words' : 'break-all'}`}>{fullCommand}</span> }}
          />
        </span>
      ),
    })
  }
  if (hasCommand && isShell && showTrustBase) {
    tiers.push({
      action: 'trust_base',
      fire: () => onAction('trust_base', basePattern),
      icon: <ShieldPlus size={12} className="shrink-0 text-ok" />,
      body: (
        <span className="truncate">
          <Trans
            i18nKey={baseKey}
            values={labelValues}
            components={{ mono: <span className="font-mono">{baseLabel}</span> }}
          />
        </span>
      ),
    })
  }
  if (trustReadsLabelKey) {
    tiers.push({
      action: 'trust_reads',
      fire: () => onAction('trust_reads'),
      icon: <BookOpen size={12} className="shrink-0 text-accent" />,
      body: <span className="min-w-0">{i18nT(trustReadsLabelKey)}</span>,
    })
  }
  if (alwaysAllowTool || alwaysAllowRefusal) {
    tiers.push({
      action: 'always_allow_tool',
      fire: () => { if (alwaysAllowTool) onAction('always_allow_tool', alwaysAllowTool) },
      icon: <ShieldCheck size={12} className="shrink-0 text-ok" />,
      disabled: !alwaysAllowTool,
      body: <AlwaysAllowBody tool={alwaysAllowTool} refusal={alwaysAllowRefusal} />,
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
        disabled={disabled || only.disabled}
        className={className}
        onClick={only.fire}
      >
        {only.icon}
        {only.body}
      </button>
    )
  }

  return (
    // A trust grant is the agent's own ceiling, wherever the menu is used:
    // the provider marks what it portals, the content says so itself as well.
    <GuideTrustRootProvider>
      <DropdownMenu open={open} onOpenChange={setOpen} modal={modal}>
        <DropdownMenuTrigger asChild>
          <button disabled={disabled} className={className}>
            <Handshake size={12} className="shrink-0" />{i18nT('components.trustDropdown.trust')}<ChevronDown size={10} className="shrink-0 opacity-70" />
          </button>
        </DropdownMenuTrigger>
        {/* The width cap is viewport-aware: a flat max-w overflows a narrow screen
            (measured at 320px, the menu reached 440px and ran off the right edge),
            which hides the very label this menu exists to make readable. */}
        <DropdownMenuContent side="top" align="end" className="min-w-[220px] max-w-[min(450px,calc(100vw-2rem))]" {...guideTrustRoot}>
          {tiers.map(tier => (
            <TrustRow key={tier.action} disabled={tier.disabled} onSelect={tier.fire}>
              {tier.icon}
              {tier.body}
            </TrustRow>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </GuideTrustRootProvider>
  )
}

/**
 * The chat approval card's menu: one duration choice on top, one row per scope.
 *
 * WHERE THE COMMAND IS READ. The exact-command grant is an exact-STRING match,
 * so the string is the scope (see the command row in `PlainTrustMenu`, which
 * prints it for that reason). This row prints it too, whole and verbatim, in a
 * bounded box that fades at whichever edge hides text. The card's own box may
 * be covered by this menu, scrolled, or out of the pane on a short screen; the
 * row never depends on it, so the menu needs no placement of its own. Every
 * row also approves the pending call, so the menu prints that call at its top
 * (`pendingCall`) when the exact-command row does not already hold all of it.
 *
 * WHAT THE DURATION COVERS. Only the command and family rows take a lifetime,
 * because only those grants record one. The read-only and all-tools rows last
 * the session whatever the choice above says, and each says so itself; the
 * always-allow row lasts until it is removed in settings.
 *
 * The duration choice is three `menuitemradio` rows laid out as one segmented
 * control, not buttons: a Radix menu takes Tab for itself, so a plain button
 * inside it is out of the keyboard's reach, while radio items sit in the same
 * arrow-key order as the rows below and announce which lifetime is in effect.
 */
function TimedTrustMenu({ fullCommand, baseCommand, isShell, hasCommand = true, disabled, className, trustAllLabelKey, trustReadsLabelKey, showTrustAll = true, showTrustBase = true, alwaysAllowTool, alwaysAllowRefusal, commandNoun = 'command', pendingCall, modal, onAction }: TrustDropdownProps) {
  const [open, setOpen] = useState(false)
  const [lifetime, setLifetime] = useState<Lifetime>('session')
  // The all-tools grant is the widest one here and names no command, so it
  // takes two clicks: the first arms the row, which then says what a second
  // click does. In the menu, choosing a duration or closing the menu disarms
  // it; the lone all-tools control disarms when it loses focus or on Escape.
  const [armed, setArmed] = useState(false)
  const durationLabelId = useId()
  const basePattern = trustBasePattern(baseCommand)
  const baseLabel = baseCommandLabel(baseCommand)
  const showCommand = hasCommand
  const showBase = hasCommand && isShell && showTrustBase
  const showReads = !!trustReadsLabelKey
  const showAlways = !!(alwaysAllowTool || alwaysAllowRefusal)
  const allConfirmLabel = i18nT('components.trustDropdown.scope_all_confirm')
  const allLabel = armed ? allConfirmLabel : i18nT(trustAllLabelKey ?? 'components.trustDropdown.scope_all')
  const idleAllLabel = i18nT(trustAllLabelKey ?? 'components.trustDropdown.trust_all_tools')
  // The same row as `PlainTrustMenu`'s: a refused grant stays visible, with its reason.
  const alwaysBody = <AlwaysAllowBody tool={alwaysAllowTool} refusal={alwaysAllowRefusal} />
  // Every row also runs the pending call, which the open menu covers on the
  // card. Printed once at the top, unless the exact-command row's box already
  // holds all of it (a shell command with nothing else in its input).
  const hasCall = !!pendingCall && !!(pendingCall.command || pendingCall.input || pendingCall.tool?.name)
  const rowPrintsCall = showCommand && commandNoun === 'command' && !!pendingCall && pendingCall.command === fullCommand && !pendingCall.input
  const showCall = hasCall && !rowPrintsCall
  const fireAlways = () => { if (alwaysAllowTool) onAction('always_allow_tool', alwaysAllowTool) }

  if (!showCommand && !showBase && !showReads && !showTrustAll && !showAlways) return null

  // The lifetime is measured at the click, so "until midnight" ends at the
  // midnight the user is in when they choose it.
  const ttl = (): [] | [number] => (lifetime === 'session' ? [] : [grantTtlSeconds(lifetime, new Date())])

  // A ONE-tier menu is not a menu (see `PlainTrustMenu`): the control carries
  // the tier's own label. Only the tiers that take no lifetime can stand
  // alone this way; a lone command tier still has a duration to choose.
  if (!showCommand && !showBase && [showReads, showTrustAll, showAlways].filter(Boolean).length === 1) {
    if (showAlways) {
      return (
        <button disabled={disabled || !alwaysAllowTool} className={className} onClick={fireAlways}>
          <ShieldCheck size={12} className="shrink-0 text-ok" />
          {alwaysBody}
        </button>
      )
    }
    if (showReads) {
      return (
        <button disabled={disabled} className={className} onClick={() => onAction('trust_reads')}>
          <BookOpen size={12} className="shrink-0 text-accent" />
          <span className="min-w-0">{i18nT(trustReadsLabelKey!)}</span>
        </button>
      )
    }
    return (
      <button
        disabled={disabled}
        className={className}
        onClick={() => { if (armed) onAction('trust'); else setArmed(true) }}
        onBlur={() => setArmed(false)}
        onKeyDown={e => { if (e.key === 'Escape') setArmed(false) }}
      >
        <ShieldCheck size={12} className="shrink-0 text-warn" />
        {/* Both labels share one grid cell, the other one drawn invisibly from
            an attribute, so the button is as wide armed as idle and the second
            click lands where the first did. The reserve is not in the text. */}
        <span className="grid min-w-0">
          <span className="col-start-1 row-start-1" aria-live="polite">{armed ? allLabel : idleAllLabel}</span>
          <span aria-hidden className="col-start-1 row-start-1 invisible after:content-[attr(data-reserve)]" data-reserve={armed ? idleAllLabel : allConfirmLabel} />
        </span>
      </button>
    )
  }

  // One row per scope, built once so the menu renders exactly what was decided
  // above. Each row closes the menu and fires, except the all-tools row's
  // first click.
  const rows: { id: string; icon: ReactNode; body: ReactNode; disabled?: boolean; select: (e: Event) => void }[] = []
  if (showCommand) {
    rows.push({
      id: 'trust_command',
      icon: <Shield size={12} className="shrink-0 text-accent" />,
      // A shell grant is an exact-string match, and the card shows that string.
      // A non-shell grant is not: the server keys it on the tool (and, for a
      // file write, its one file), whatever the call's other arguments. So that
      // row prints the server's own label for the scope, whole, as
      // `PlainTrustMenu` does, rather than claim this exact call.
      body: commandNoun === 'call' ? (
        <span className="min-w-0 whitespace-pre-wrap">
          <Trans
            i18nKey={CALL_SCOPE_KEYS[lifetime]}
            components={{ mono: <span data-testid="trust-call-scope" className="bg-bg-elevated px-1.5 py-0.5 rounded font-mono break-all border border-border">{fullCommand}</span> }}
          />
        </span>
      ) : (
        <span className="min-w-0">
          {i18nT(COMMAND_SCOPE_KEYS[lifetime])}
          {/* The whole command, verbatim: the grant is an exact-string match,
              so the string must be readable where it is chosen. In its own
              bounded box with edge cues, so the rows below stay in view. */}
          <CuedScrollBox testId="trust-command-full" className="mt-1 max-h-[30vh] bg-bg-elevated px-1.5 py-0.5 rounded font-mono whitespace-break-spaces break-all border border-border">{fullCommand}</CuedScrollBox>
        </span>
      ),
      select: () => onAction('trust_command', fullCommand, ...ttl()),
    })
  }
  if (showBase) {
    rows.push({
      id: 'trust_base',
      icon: <ShieldPlus size={12} className="shrink-0 text-accent" />,
      body: (
        <span className="min-w-0">
          <Trans i18nKey={BASE_SCOPE_KEYS[lifetime]} components={{ mono: <span className="font-mono">{baseLabel}</span> }} />
        </span>
      ),
      select: () => onAction('trust_base', basePattern, ...ttl()),
    })
  }
  if (showAlways) {
    rows.push({
      id: 'always_allow_tool',
      icon: <ShieldCheck size={12} className="shrink-0 text-ok" />,
      body: alwaysBody,
      disabled: !alwaysAllowTool,
      select: fireAlways,
    })
  }
  if (showReads) {
    rows.push({
      id: 'trust_reads',
      icon: <BookOpen size={12} className="shrink-0 text-accent" />,
      body: <span className="min-w-0">{i18nT('components.trustDropdown.scope_reads')}</span>,
      select: () => onAction('trust_reads'),
    })
  }
  if (showTrustAll) {
    rows.push({
      id: 'trust',
      icon: <ShieldCheck size={12} className="shrink-0 text-warn" />,
      // The widest grant reads in the warning tone, and heavier once armed.
      body: <span className={`min-w-0 text-warn-text ${armed ? 'font-semibold' : ''}`} aria-live="polite">{allLabel}</span>,
      select: e => {
        if (armed) { onAction('trust'); return }
        // The first click only arms the row: the menu stays open and the row
        // now says what the next click does.
        e.preventDefault()
        setArmed(true)
      },
    })
  }

  const onOpenChange = (next: boolean) => {
    if (!next) {
      // A choice made and then left belongs to that opening only.
      setArmed(false)
      setLifetime('session')
    }
    setOpen(next)
  }

  return (
    // A trust grant is the agent's own ceiling, wherever the menu is used:
    // the provider marks what it portals, the content says so itself as well.
    <GuideTrustRootProvider>
      <DropdownMenu open={open} onOpenChange={onOpenChange} modal={modal}>
        <DropdownMenuTrigger asChild>
          <button disabled={disabled} className={className}>
            <Handshake size={12} className="shrink-0" />{i18nT('components.trustDropdown.trust')}<ChevronDown size={10} className="shrink-0 opacity-70" />
          </button>
        </DropdownMenuTrigger>
        {/* On a short screen the menu itself scrolls; it fades the edge that hides rows. */}
        <CuedMenuContent side="top" align="end" data-testid="trust-menu" {...guideTrustRoot}>
          {showCall && pendingCall && <PendingCallItem testId="trust-pending-call" label={i18nT('components.trustDropdown.pending_call_label')} call={pendingCall} />}
          {(showCommand || showBase) && (
            <div className="flex items-center gap-2 px-2 pt-1 pb-1.5">
              <span id={durationLabelId} className="shrink-0 text-[12px] text-muted">{i18nT('components.trustDropdown.duration_label')}</span>
              <DropdownMenuRadioSegments value={lifetime} aria-labelledby={durationLabelId}>
                {LIFETIMES.map(choice => (
                  <DropdownMenuRadioItem
                    key={choice}
                    value={choice}
                    segment
                    // Choosing a lifetime changes what the rows below grant; it
                    // grants nothing itself, so the menu stays open.
                    onSelect={e => { e.preventDefault(); setLifetime(choice); setArmed(false) }}
                  >
                    {choice === 'hour' ? (
                      <Trans
                        i18nKey="components.trustDropdown.duration_hour"
                        // The clock time the grant would end, as of this render.
                        values={{ time: fmtTime(grantEndsAt('hour', new Date())) }}
                        components={{ small: <span className="text-[11px] font-normal opacity-75" /> }}
                      />
                    ) : i18nT(DURATION_KEYS[choice])}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioSegments>
            </div>
          )}
          {rows.map((row, i) => (
            <Fragment key={row.id}>
              {/* A rule under the rows the duration covers, so it reads as theirs
                  alone, and a heading over the rows that always last the session. */}
              {i > 0 && !TIMED_ROWS.has(row.id) && TIMED_ROWS.has(rows[i - 1].id) && <DropdownMenuSeparator />}
              {SESSION_ROWS.has(row.id) && (i === 0 || !SESSION_ROWS.has(rows[i - 1].id)) && (
                // A plain line, as the Duration label above is: it names the rows, it is not one.
                <div data-testid="trust-session-rows" className="px-2 pt-1 pb-0.5 text-[11px] text-muted">{i18nT('components.trustDropdown.session_rows_label')}</div>
              )}
              <TrustRow disabled={row.disabled} onSelect={row.select}>
                {row.icon}
                {row.body}
              </TrustRow>
            </Fragment>
          ))}
        </CuedMenuContent>
      </DropdownMenu>
    </GuideTrustRootProvider>
  )
}

/** Trust's place on an approval bar whose request takes no standing grant: the
    menu's own trigger, disabled and just as wide, so Allow once and Reject sit
    where they do beside the Trust menu and a reflex click there never lands on a
    grant. Native `disabled` keeps it out of the tab order and the approve chord's
    probe. The reason is a `title` on the wrapper, since a disabled button gets no
    pointer events, rather than an InfoTip, which would add a control to the row. */
export function TrustUnavailable({ className }: { className?: string }) {
  const reasonId = useId()
  const reason = i18nT('components.trustDropdown.trust_unavailable')
  return (
    <span title={reason} className="inline-flex cursor-not-allowed">
      <button disabled aria-describedby={reasonId} className={`${className ?? ''} pointer-events-none`}>
        <Handshake size={12} className="shrink-0" />{i18nT('components.trustDropdown.trust')}<ChevronDown size={10} className="shrink-0 opacity-70" />
      </button>
      <span id={reasonId} hidden>{reason}</span>
    </span>
  )
}
