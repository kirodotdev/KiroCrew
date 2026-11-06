import { useId, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { SquareTerminal } from 'lucide-react'

import { api } from '../api/client'
import ErrorNotice, {
  ErrorNoticeMenuItem,
  type ErrorNoticeMenuItemComponent,
} from './ErrorNotice'
import { addKiroCliTab, MAX_TERMINALS, useBottomTerminal } from '../hooks/useBottomTerminal'
import { bringBack as bringBackTerminalPopout, isPopoutOpen as isTerminalPopoutOpen } from '../utils/terminalPopout'
import { useTerminalEnabled } from '../utils/terminalRegistry'
import { useOpenInKiroCliPreview } from '../hooks/usePreviewFlag'
import { KIRO_CLI_HIDDEN_CODES, kiroCliBlockedLabel } from '../lib/kiroCli'
import { i18nT } from '../i18n/t'

/** Open the tab in THIS window. The one-shot open intent is armed in this
 *  window's terminal registry, so a popped-out panel comes back first (as its
 *  Return button does); in the popout the tab's first dial would not carry it. */
function openKiroCliHere(slotKey: string, title?: string): void {
  // `addKiroCliTab` opens this window's panel (`open` is per window).
  if (isTerminalPopoutOpen()) bringBackTerminalPopout()
  addKiroCliTab(slotKey, title)
}

/** *text* with each "kiro-cli" kept on one line: a wrap at its hyphen reads as
 *  two words. */
function keepKiroCliWhole(text: string): ReactNode {
  const parts = text.split('kiro-cli')
  return parts.flatMap((part, i) => (i === 0
    ? [part]
    : [<span key={i} className="whitespace-nowrap">kiro-cli</span>, part]))
}

interface OpenInKiroCliItemProps {
  /** The chat whose own kiro-cli conversation the terminal opens. */
  readonly slotKey: string
  /** The chat's title, which labels the terminal tab. */
  readonly title?: string
  /** True while the chat runs a turn: the gateway refuses then, so the row says so at once. */
  readonly running: boolean
  /** The Radix menu-item primitive of the hosting menu family. */
  readonly Item: ErrorNoticeMenuItemComponent
}

/**
 * "Open in kiro-cli": continue this chat's conversation in kiro-cli, in a new
 * tab of the docked terminal panel. The chat pauses while kiro-cli holds the
 * conversation and picks it up again once kiro-cli exits.
 *
 * Renders nothing while its Feature Preview is off (the default; the gateway
 * refuses the handover then too), when the terminal panel is off, or when the
 * chat can never open in kiro-cli (another backend, a channel or app chat, a remote environment), and
 * a disabled row naming the reason while it cannot open yet (no conversation,
 * a turn running, the log or folder missing). A failed availability read is
 * shown through ErrorNotice rather than read as "unavailable".
 *
 * The availability answer is fetched only while the menu is open (Radix mounts
 * a menu's Content on open), and fresh each time, since a chat's state moves.
 */
export default function OpenInKiroCliItem({ slotKey, title, running, Item }: OpenInKiroCliItemProps) {
  const terminalEnabled = useTerminalEnabled()
  const previewOn = useOpenInKiroCliPreview()
  const { tabs } = useBottomTerminal()
  const errorId = useId()
  const { data, error } = useQuery({
    queryKey: ['kiro-cli-availability', slotKey, running],
    queryFn: () => api.chatSlotKiroCli(slotKey),
    enabled: terminalEnabled && previewOn,
    staleTime: 0,
    retry: false,
  })

  if (!previewOn || !terminalEnabled) return null
  const label = i18nT('components.sessionActionsMenu.open_in_kiro_cli')

  if (error) {
    const message = error instanceof Error ? error.message : String(error)
    return (
      <>
        <Item disabled>
          <SquareTerminal size={13} className="shrink-0 text-muted" /> {label}
          <span
            className="ml-auto"
            role="presentation"
            onClick={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <ErrorNotice
              id={errorId}
              message={message}
              title={i18nT('components.sessionActionsMenu.kiro_cli_check_failed')}
              variant="inline"
            />
          </span>
        </Item>
        <ErrorNoticeMenuItem Item={Item} message={message} describedBy={errorId} />
      </>
    )
  }
  if (!data) return null
  if (!data.available && data.code && KIRO_CLI_HIDDEN_CODES.has(data.code)) return null

  const hasOwnTab = tabs.some(t => t.kiroCliSlot === slotKey)
  const atCap = !hasOwnTab && tabs.length >= MAX_TERMINALS
  const blocked = running
    ? kiroCliBlockedLabel('kiro_cli_busy')
    : !data.available
      ? kiroCliBlockedLabel(data.code ?? '')
      : atCap
        ? i18nT('components.sessionActionsMenu.kiro_cli_blocked_terminals_full')
        : ''
  const fallbackAgent = data.available ? data.agent_without_spec : ''
  // The row says what happens to the chat; which agent kiro-cli runs is detail
  // for the hover text.
  const tooltip = [
    i18nT('components.sessionActionsMenu.open_in_kiro_cli_tooltip'),
    fallbackAgent ? i18nT('components.sessionActionsMenu.open_in_kiro_cli_default_agent', { agent: fallbackAgent }) : '',
  ].filter(Boolean).join(' ')

  return (
    <Item
      disabled={Boolean(blocked) && !hasOwnTab}
      title={tooltip}
      onSelect={() => { openKiroCliHere(slotKey, title) }}
      data-testid="open-in-kiro-cli"
    >
      <SquareTerminal size={13} className="self-start mt-0.5 shrink-0 text-muted" />
      <span className="flex min-w-0 flex-col">
        <span>{label}</span>
        {/* Under the label at the label's own contrast: what happens, or why it
            cannot open yet. */}
        <span
          className="max-w-[15rem] whitespace-normal text-[12px] leading-snug"
          data-testid={blocked && !hasOwnTab ? 'open-in-kiro-cli-reason' : 'open-in-kiro-cli-note'}
        >
          {keepKiroCliWhole(blocked && !hasOwnTab ? blocked : i18nT('components.sessionActionsMenu.open_in_kiro_cli_note'))}
        </span>
      </span>
    </Item>
  )
}
