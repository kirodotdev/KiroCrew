import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { X } from 'lucide-react'
import AppIcon from '../AppIcon'
import { OverlayPortal, useCloseWhenCrewHidden } from '../overlayRoot'
import ErrorNotice from '../ErrorNotice'
import { DropdownMenuItem } from '../ui/dropdown-menu'
import { i18nT } from '../../i18n/t'
import { anchoredPopupBox } from '../../lib/pickerMenu'
import { currentTheme } from '../../appFrame/sandboxFrame'
import { ComposerActionHost, type EndReason } from '../../composerActions/composerActionHost'
import type { ComposerActionItem } from '../../composerActions/composerActionRegistry'
import { composerWouldResolve, draftIsEmpty } from '../../composerActions/literalInsert'
import type { ComposerControl } from '../composerControl'
import type { PasteBlock } from '../../utils/pasteTokens'

/* App composer actions (`contributes.composerActions`): rows in the "+" menu that
   open the app's sandboxed frame in a popover above the composer. The app is
   handed nothing from the composer; it offers text. The rule is stateless: the
   text goes in only when the composer is EMPTY, and only when the text alone
   would resolve nothing (no command, mention, skill, paste token, marker or
   path). Otherwise the host refuses it and says why in the popover. Accepted text
   is ordinary draft text: whatever the person types after it is their own and
   resolves normally. The person reads it and sends it; the app never sends. */

/** How long one invocation may stay open; each resize or offer resets it. */
export const COMPOSER_ACTION_DEADLINE_MS = 2 * 60_000
const DEFAULT_FRAME_HEIGHT = 160
const MIN_FRAME_HEIGHT = 48

/** Why the host would not insert: the draft is not empty, or the text resolves. */
export type InsertRefusal = 'not_empty' | 'not_literal'

type Phase =
  | { kind: 'running'; refused?: InsertRefusal }
  /** Clicked with a non-empty draft: no frame is made. */
  | { kind: 'blocked' }
  | { kind: 'ended'; reason: EndReason }

/** Where the popover hangs from: its left edge, the top it sits above, and
 *  the bottom it sits below with `openBelow`. */
type Anchor = { left: number; top: number; bottom: number }

interface Open {
  item: ComposerActionItem
  anchor: Anchor | null
}

export interface ComposerActionsOptions {
  items: readonly ComposerActionItem[]
  value: string
  onChange: (v: string) => void
  pasteBlocks: readonly PasteBlock[]
  composerControl: () => ComposerControl | null
  /** Changes when the composer starts holding another session's draft: an open
   *  invocation is cancelled then, never answered into the other draft. */
  scopeKey?: string | null
  /** The composer is typing-locked (another tab holds the draft): no action
   *  opens, and an open one is cancelled, so its text never lands. */
  locked?: boolean
  /** Where the popover hangs from. */
  anchor: () => Anchor | null
  /** Open below the anchor instead of above it, capped to the room below
   *  (`ChatInputProps.openBelow`). */
  openBelow?: boolean
}

export function useComposerActions({ items, value, onChange, pasteBlocks, composerControl, scopeKey, locked = false, anchor, openBelow = false }: ComposerActionsOptions) {
  const [open, setOpen] = useState<Open | null>(null)
  const [phase, setPhase] = useState<Phase>({ kind: 'running' })
  const [frameHeight, setFrameHeight] = useState(DEFAULT_FRAME_HEIGHT)
  const frameRef = useRef<ComposerActionHost | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  // Read at offer time, after the app worked: the live draft, not the render's.
  const live = useRef({ value, pasteBlocks, onChange, composerControl, locked })
  live.current = { value, pasteBlocks, onChange, composerControl, locked }
  const openRef = useRef(open)
  openRef.current = open

  const close = useCallback(() => {
    frameRef.current?.cancel('cancelled')
    frameRef.current = null
    setOpen(null)
  }, [])

  const start = useCallback((item: ComposerActionItem) => {
    frameRef.current?.cancel('cancelled')
    frameRef.current = null
    const { value: v, pasteBlocks: blocks, locked: isLocked } = live.current
    if (isLocked) { setOpen(null); return }
    setFrameHeight(DEFAULT_FRAME_HEIGHT)
    // Only an empty composer takes an app's text, so a draft is never mixed.
    setPhase(draftIsEmpty(v, blocks) ? { kind: 'running' } : { kind: 'blocked' })
    setOpen({ item, anchor: anchor() })
  }, [anchor])

  // The frame is made once its container is in the document.
  useLayoutEffect(() => {
    if (!open || phase.kind === 'blocked' || frameRef.current || !containerRef.current) return
    const { item } = open
    const frame = new ComposerActionHost({
      appName: item.appName,
      actionId: item.id,
      container: containerRef.current,
      theme: currentTheme,
      deadlineMs: COMPOSER_ACTION_DEADLINE_MS,
      onText: (text) => {
        const now = live.current
        // Another tab took the draft: the effect below closes the popover.
        if (now.locked) return 'stale'
        // The person typed while the app worked: the composer is theirs now.
        if (!draftIsEmpty(now.value, now.pasteBlocks)) { setPhase({ kind: 'running', refused: 'not_empty' }); return 'not_empty' }
        // The text alone would resolve: a command, mention, skill or path.
        if (composerWouldResolve(text)) { setPhase({ kind: 'running', refused: 'not_literal' }); return 'not_literal' }
        now.onChange(text)
        requestAnimationFrame(() => now.composerControl()?.setSelection(text.length, text.length, { focus: true }))
        frameRef.current = null
        setOpen(null)
        return undefined
      },
      onResize: (h) => setFrameHeight(Math.max(MIN_FRAME_HEIGHT, Math.min(item.maxHeight, Math.round(h)))),
      onEnd: (reason) => {
        frameRef.current = null
        // The person's own close needs no notice; anything else says what happened.
        if (reason === 'cancelled' || reason === 'closed') { setOpen(null); return }
        setPhase({ kind: 'ended', reason })
      },
      onEscape: () => close(),
    })
    frameRef.current = frame
    frame.start()
  }, [open, phase.kind, close])

  // Another session's draft in this composer: cancel, never insert into it.
  const scopeRef = useRef(scopeKey)
  useEffect(() => {
    if (scopeRef.current !== scopeKey && openRef.current) close()
    scopeRef.current = scopeKey
  }, [scopeKey, close])

  // A typing lock makes the draft read-only: cancel, never write into it.
  useEffect(() => {
    if (locked && openRef.current) close()
  }, [locked, close])

  // An action the owner revoked or an app disabled goes with its row.
  useEffect(() => {
    if (open && !items.some((i) => i.key === open.item.key)) {
      frameRef.current?.cancel('revoked')
      frameRef.current = null
      setOpen(null)
    }
  }, [items, open])

  useEffect(() => () => { frameRef.current?.cancel('cancelled') }, [])

  const popover: ReactNode = open ? (
    <ComposerActionPopover item={open.item} anchor={open.anchor} openBelow={openBelow} phase={phase} frameHeight={frameHeight} containerRef={containerRef} onClose={close} />
  ) : null

  return { start, close, popover, openKey: open?.item.key ?? null }
}

function endedMessage(reason: EndReason, app: string): string {
  switch (reason) {
    case 'deadline': return i18nT('components.chatInput.composer_action_timed_out', { app })
    case 'app_error': return i18nT('components.chatInput.composer_action_app_error', { app })
    case 'revoked': return i18nT('components.chatInput.composer_action_revoked', { app })
    default: return i18nT('components.chatInput.composer_action_failed', { app })
  }
}

function refusalMessage(refused: InsertRefusal, app: string): string {
  return refused === 'not_empty'
    ? i18nT('components.chatInput.composer_action_needs_empty', { app })
    : i18nT('components.chatInput.composer_action_would_resolve', { app })
}

function ComposerActionPopover({ item, anchor, openBelow, phase, frameHeight, containerRef, onClose }: {
  item: ComposerActionItem
  anchor: Anchor | null
  openBelow: boolean
  phase: Phase
  frameHeight: number
  containerRef: React.MutableRefObject<HTMLDivElement | null>
  onClose: () => void
}) {
  const width = Math.min(item.width, window.innerWidth - 16)
  const left = Math.max(8, Math.min(anchor?.left ?? 8, window.innerWidth - width - 8))
  // Above: `bottom` from the viewport's bottom edge. Below: `top` from its top edge.
  // With no anchor, 96px from that edge.
  const side: { top: number } | { bottom: number } = anchor ? anchoredPopupBox(anchor, openBelow, 8) : openBelow ? { top: 96 } : { bottom: 96 }
  const offset = 'top' in side ? side.top : side.bottom
  // Never taller than the room on its side of the anchor, so the header and close stay on screen.
  const maxHeight = Math.max(MIN_FRAME_HEIGHT, window.innerHeight - offset - 8)
  const title = i18nT('components.chatInput.composer_action_dialog', { title: item.title, app: item.appLabel })
  // Escape from the dashboard closes it; Escape inside the frame arrives as `escape`.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  // A switch to a remote Crew cancels the invocation, so that Crew's Escape
  // never reaches this listener.
  useCloseWhenCrewHidden(true, onClose)
  // A refusal is a hint about the draft or the text, not a failure: plain text.
  const hint = phase.kind === 'blocked'
    ? refusalMessage('not_empty', item.appLabel)
    : phase.kind === 'running' && phase.refused ? refusalMessage(phase.refused, item.appLabel) : ''
  return (
    <OverlayPortal>
      <div
        role="dialog"
        aria-label={title}
        data-composer-action={item.key}
        className="fixed rounded-xl bg-bg-elevated border border-border shadow-xl z-[60] flex flex-col overflow-y-auto animate-slide-up"
        style={{ left, ...('top' in side ? { top: offset } : { bottom: offset }), width, maxHeight }}
      >
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
          {item.icon ? <AppIcon icon={item.icon} size={14} /> : null}
          <div className="min-w-0 flex-1">
            <div className="text-[12px] font-medium text-text truncate">{item.title}</div>
            <div className="text-[11px] text-muted truncate">{i18nT('components.chatInput.composer_action_from', { app: item.appLabel })}</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={i18nT('components.chatInput.composer_action_close')}
            title={i18nT('components.chatInput.composer_action_close')}
            className="w-6 h-6 rounded-md flex items-center justify-center bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover"
          >
            <X size={14} />
          </button>
        </div>
        {/* A refused offer leaves the frame open, so the app can offer other text. */}
        {hint && (
          <p role="status" data-testid="composer-action-hint" className="m-0 px-3 py-2 text-[12px] text-text">{hint}</p>
        )}
        {phase.kind === 'ended' && (
          // No hand-off: the composer draft below is unsaved.
          <ErrorNotice message={endedMessage(phase.reason, item.appLabel)} variant="inline" className="px-3 py-2" testId="composer-action-ended" />
        )}
        {phase.kind === 'running' && (
          <div ref={containerRef} style={{ height: frameHeight }} className="w-full overflow-hidden" />
        )}
        {/* The host, not the app, checked the draft: say so when it refused for that. */}
        <div className="px-3 py-1.5 border-t border-border text-[11px] text-muted">
          {phase.kind === 'blocked' || (phase.kind === 'running' && phase.refused === 'not_empty')
            ? i18nT('components.chatInput.composer_action_footer_host', { app: item.appLabel })
            : i18nT('components.chatInput.composer_action_footer', { app: item.appLabel })}
        </div>
      </div>
    </OverlayPortal>
  )
}

/** The "+" menu rows, one per action, each naming its app. */
export function ComposerActionRows({ items, onPick }: { items: readonly ComposerActionItem[]; onPick: (item: ComposerActionItem) => void }) {
  if (!items.length) return null
  return (
    <div className="mt-2 pt-2 border-t border-border flex flex-col gap-0.5">
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          onClick={() => onPick(item)}
          data-composer-action-row={item.key}
          className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg bg-transparent hover:bg-bg-hover transition-colors cursor-pointer text-left"
        >
          <span className="w-4 shrink-0 flex items-center justify-center text-muted"><AppIcon icon={item.icon} size={14} /></span>
          <div className="min-w-0 flex flex-col">
            <span className="text-[12px] font-medium text-text truncate">{item.title}</span>
            {/* Attribution: the row is the app's, so it says whose. */}
            <span className="text-[11px] text-muted truncate">{i18nT('components.chatInput.composer_action_from', { app: item.appLabel })}</span>
          </div>
        </button>
      ))}
    </div>
  )
}

/** The same rows as items of the touch overflow menu (ChatInput's
 *  `composer-more-trigger`), which stands in for the "+" menu on a phone. */
export function ComposerActionMenuItems({ items, onPick }: { items: readonly ComposerActionItem[]; onPick: (item: ComposerActionItem) => void }) {
  if (!items.length) return null
  return (
    <div className="mt-1 pt-1 border-t border-border">
      {items.map((item) => (
        <DropdownMenuItem key={item.key} asChild onSelect={() => onPick(item)}>
          <div data-composer-action-row={item.key} className="w-full flex items-center gap-2.5 px-2 py-1.5 rounded-lg cursor-pointer text-left">
            <span className="w-4 shrink-0 flex items-center justify-center text-muted"><AppIcon icon={item.icon} size={14} /></span>
            <div className="min-w-0 flex flex-col">
              <span className="text-[12px] font-medium text-text truncate">{item.title}</span>
              <span className="text-[11px] text-muted truncate">{i18nT('components.chatInput.composer_action_from', { app: item.appLabel })}</span>
            </div>
          </div>
        </DropdownMenuItem>
      ))}
    </div>
  )
}

const NO_ACTIONS: readonly ComposerActionItem[] = []

/** ChatInput's whole wiring: the "+" menu rows and the popover. */
export function useComposerActionMenu({ items, plusBtnRef, composerBoxRef, closeMenu, ...rest }: Omit<ComposerActionsOptions, 'items' | 'anchor'> & {
  items: readonly ComposerActionItem[] | undefined
  plusBtnRef: React.RefObject<HTMLButtonElement | null>
  /** The composer box: the popover sits above it (below the box and the "+"
   *  button with `openBelow`), so the draft stays in view. */
  composerBoxRef?: React.RefObject<HTMLElement | null>
  closeMenu: () => void
}) {
  const offered = items?.length ? items : NO_ACTIONS
  const actions = useComposerActions({
    ...rest,
    items: offered,
    anchor: () => {
      const plus = plusBtnRef.current?.getBoundingClientRect()
      if (!plus) return null
      const box = composerBoxRef?.current?.getBoundingClientRect()
      return { left: plus.left, top: box ? box.top : plus.top, bottom: Math.max(plus.bottom, box ? box.bottom : plus.bottom) }
    },
  })
  const rows = offered.length
    ? <ComposerActionRows items={offered} onPick={(item) => { closeMenu(); actions.start(item) }} />
    : null
  return { rows, items: offered, start: actions.start, popover: actions.popover }
}
