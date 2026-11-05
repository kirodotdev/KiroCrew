/**
 * SelectionActionLayer — runs app items on selected chat text (`contributes.selectionActions`).
 *
 * The host owns everything around the app: the trigger (an item, or the Apps
 * menu, in the selection toolbar of a chat reply), the popover with the app's
 * name and a Close button, and the Copy and Open buttons. Only the popover body
 * is the app's: a sandboxed, opaque-origin frame that is handed the selected text
 * and nothing else (`selectionInvocation.ts`).
 *
 * On a touch device the popover is a sheet at the bottom of the screen, so it
 * never covers the native selection handles and Copy callout; nothing here
 * listens for a long-press.
 *
 * Mounted once, beside `LinkPreviewLayer`.
 *
 * @module selectionActions/SelectionActionLayer
 */
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { Check, ExternalLink, X } from 'lucide-react'
import { api } from '../api/client'
import { currentTheme, mintAppFrame } from '../appFrame/sandboxFrame'
import ErrorNotice from '../components/ErrorNotice'
import { OverlayPortal, useCloseWhenCrewHidden } from '../components/overlayRoot'
import { ESCAPE_PRIORITY, useEscapeLayer } from '../hooks/useEscapeLayer'
import { Btn } from '../components/ui'
import { i18nT } from '../i18n/t'
import { copyToClipboard } from '../utils/clipboard'
import { isTouchDevice } from '../utils/isTouchDevice'
import { closeSelectionLayer, openSelectionItem, setSelectionActionsAvailable, useSelectionLayerState, type Anchor } from './selectionActionStore'
import { resolveSelectionActions, type ResolvedSelectionAction } from './selectionActionRegistry'
import { SelectionInvocation, type InvocationOptions, type InvocationView } from './selectionInvocation'
import { selectedTextFor, type SelectionSourceKind } from './selectionSources'
import { itemIcon } from './useAppSelectionActions'

const MARGIN = 8
const GAP = 8
/** Header and footer of the popover, outside the frame. */
const CHROME_PX = 96

/** Overridable in tests: how an item's frame is minted. */
let mintForItems: InvocationOptions['mint'] = mintAppFrame
export function setSelectionActionMintForTest(mint?: InvocationOptions['mint']): void {
  mintForItems = mint ?? mintAppFrame
}

/** A floating surface's size, never wider or taller than the viewport allows. */
export function fitViewport(width: number, height: number): { width: number; height: number } {
  return {
    width: Math.max(0, Math.min(width, window.innerWidth - 2 * MARGIN)),
    height: Math.max(0, Math.min(height, window.innerHeight - 2 * MARGIN)),
  }
}

/** Where a floating surface of `width` x `height` goes for an anchor: below it,
 *  else above, and always inside the viewport on both axes (the anchor may have
 *  scrolled or the window shrunk since it was taken). */
export function placeBelow(anchor: Anchor, width: number, height: number): { left: number; top: number } {
  const vw = window.innerWidth
  const vh = window.innerHeight
  const clamp = (n: number, hi: number) => Math.max(MARGIN, Math.min(n, hi))
  const below = anchor.bottom + GAP
  const top = below + height <= vh - MARGIN ? below : anchor.top - GAP - height
  return { left: clamp(anchor.left, vw - width - MARGIN), top: clamp(top, vh - height - MARGIN) }
}

/** Re-render on every window resize, so floating surfaces re-fit and re-place. */
function useViewportSize(): void {
  const [, setTick] = useState(0)
  useEffect(() => {
    const onResize = () => setTick((t) => t + 1)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
}

function useDismiss(ref: React.RefObject<HTMLElement | null>, close: () => void) {
  // A page-wide Escape goes through the shared layer stack, above the composer.
  useEscapeLayer(true, () => { close(); return 'take' }, ESCAPE_PRIORITY.flyout)
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      const el = ref.current
      if (el && e.target instanceof Node && !el.contains(e.target)) close()
    }
    document.addEventListener('pointerdown', onDown, true)
    return () => { document.removeEventListener('pointerdown', onDown, true) }
  }, [ref, close])
}

type MenuRead = { phase: 'loading' } | { phase: 'failed' } | { phase: 'ready'; items: ResolvedSelectionAction[] }

/** One fresh read of the app list: the items for `source` and nothing cached. */
async function readMenuItems(source: SelectionSourceKind): Promise<ResolvedSelectionAction[]> {
  const raw = typeof api?.listApps === 'function' ? await api.listApps() : []
  return selectItems(raw).filter((i) => i.sources.has(source))
}

function AppsMenu({ source, text, anchor }: { source: SelectionSourceKind; text: string; anchor: Anchor }) {
  const ref = useRef<HTMLDivElement>(null)
  // The menu reads the app list itself, every time it opens and on every Retry.
  // It never lists cached or stale items, and nothing carries over between opens.
  const [read, setRead] = useState<MenuRead>({ phase: 'loading' })
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let live = true
    setRead({ phase: 'loading' })
    readMenuItems(source).then(
      (items) => { if (live) setRead({ phase: 'ready', items }) },
      () => { if (live) setRead({ phase: 'failed' }) },
    )
    return () => { live = false }
  }, [source, attempt])
  const items = read.phase === 'ready' ? read.items : []
  useViewportSize()
  useDismiss(ref, closeSelectionLayer)
  const touch = isTouchDevice()
  const menuSize = fitViewport(240, Math.min(48 + Math.max(items.length, 1) * 36, 360))
  const pos = touch ? null : { ...placeBelow(anchor, menuSize.width, menuSize.height), width: menuSize.width, maxHeight: menuSize.height }
  useEffect(() => { ref.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus() }, [read.phase])
  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const rows = Array.from(ref.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
    const at = rows.indexOf(document.activeElement as HTMLButtonElement)
    rows[(at + (e.key === 'ArrowDown' ? 1 : rows.length - 1)) % rows.length]?.focus()
  }
  return (
    <div
      ref={ref}
      role="menu"
      tabIndex={-1}
      aria-label={i18nT('components.selectionActions.menu_label')}
      onKeyDown={onKeyDown}
      data-testid="selection-apps-menu"
      // Never taller than the screen: every row stays reachable by scrolling.
      className={`fixed z-[9999] flex flex-col p-1 rounded-lg bg-bg-elevated border border-border shadow-lg overflow-y-auto overscroll-contain ${touch ? 'left-safe-offset-2 right-safe-offset-2 bottom-safe-offset-2 max-h-[calc(100dvh-env(safe-area-inset-bottom,0px)-env(safe-area-inset-top,0px)-16px)]' : ''}`}
      style={pos ?? undefined}
    >
      {read.phase === 'loading' && (
        <div role="status" className="px-2.5 py-1.5 text-[13px] text-muted">{i18nT('components.selectionActions.loading')}</div>
      )}
      {read.phase === 'failed' && (
        <div className="m-1 flex items-center justify-between gap-2">
          {/* No hand-off: the menu floats over a chat whose composer may hold an
              unsent draft, and the hand-off navigates away from it. */}
          <ErrorNotice variant="inline" message={i18nT('components.selectionActions.load_failed')} />
          <Btn className="text-[12px] px-2 py-1 shrink-0" onClick={() => setAttempt((n) => n + 1)}>
            {i18nT('components.selectionActions.retry')}
          </Btn>
        </div>
      )}
      {read.phase === 'ready' && !items.length && (
        <div className="px-2.5 py-1.5 text-[13px] text-muted">{i18nT('components.selectionActions.none')}</div>
      )}
      {items.map((item) => (
        <button
          key={item.key}
          role="menuitem"
          className={`flex items-center gap-2 rounded-md text-left text-text hover:bg-bg-hover focus-visible:bg-bg-hover ${touch ? 'px-3 min-h-11 text-[14px]' : 'px-2.5 py-1.5 text-[13px]'}`}
          onClick={() => openSelectionItem(item, source, text, anchor)}
        >
          <span className="shrink-0">{itemIcon(item)}</span>
          <span className="min-w-0 flex-1 truncate">{item.title}</span>
          <span className="shrink-0 max-w-[45%] truncate text-[11px] text-muted">{item.appLabel}</span>
        </button>
      ))}
    </div>
  )
}

function ItemPopover({ item, source, text, anchor }: { item: ResolvedSelectionAction; source: SelectionSourceKind; text: string; anchor: Anchor }) {
  const ref = useRef<HTMLDivElement>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const titleId = useId()
  const selected = selectedTextFor(source, text, item)
  const [view, setView] = useState<InvocationView | null>(null)
  // The value the clipboard took: a later, different offer is not "Copied".
  const [copiedValue, setCopiedValue] = useState<string | null>(null)
  const [copyFailed, setCopyFailed] = useState(false)
  const invocationRef = useRef<SelectionInvocation | null>(null)
  const close = useCallback(() => closeSelectionLayer(), [])
  useDismiss(ref, close)
  useViewportSize()

  useLayoutEffect(() => {
    const box = boxRef.current
    if (!box || !selected) return
    const inv = new SelectionInvocation({
      item, selected, box, mint: mintForItems, theme: currentTheme, onView: setView, onClose: close,
    })
    invocationRef.current = inv
    setView(inv.current())
    inv.start()
    const themeWatch = new MutationObserver(() => inv.setTheme(currentTheme()))
    themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] })
    return () => {
      themeWatch.disconnect()
      inv.dispose()
      invocationRef.current = null
    }
    // One invocation per opened popover: the layer remounts this for every pick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => { closeRef.current?.focus() }, [])

  const touch = isTouchDevice()
  const frameHeight = view?.height ?? 160
  const size = fitViewport(item.width, Math.min(item.maxHeight, frameHeight) + CHROME_PX)
  const pos = touch ? null : placeBelow(anchor, size.width, size.height)
  const failed = view?.phase === 'failed'
  const copied = !!view?.copy && copiedValue === view.copy
  const onCopy = async () => {
    if (!view?.copy) return
    const value = view.copy
    const ok = await copyToClipboard(value)
    setCopiedValue(ok ? value : null)
    setCopyFailed(!ok)
  }

  return (
    <div
      ref={ref}
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
      data-testid="selection-action-popover"
      // Never taller than the screen: the header and Close stay on screen and
      // the app's body shrinks (its frame scrolls inside).
      className={`fixed z-[9999] flex flex-col rounded-lg bg-bg-elevated border border-border shadow-lg overflow-hidden max-h-[calc(100dvh-env(safe-area-inset-bottom,0px)-env(safe-area-inset-top,0px)-16px)] ${touch ? 'left-safe-offset-2 right-safe-offset-2 bottom-safe-offset-2' : ''}`}
      style={pos ? { ...pos, width: size.width } : undefined}
    >
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <span className="shrink-0 text-muted">{itemIcon(item)}</span>
        <span id={titleId} className="min-w-0 flex-1 truncate text-[13px] font-medium text-text">{item.title}</span>
        <span className="shrink-0 max-w-[45%] truncate text-[11px] text-muted">{item.appLabel}</span>
        <button
          ref={closeRef}
          className="shrink-0 p-1 rounded-md text-muted hover:text-text hover:bg-bg-hover"
          aria-label={i18nT('components.selectionActions.close')}
          title={i18nT('components.selectionActions.close')}
          onClick={close}
        >
          <X size={14} />
        </button>
      </div>
      {!selected ? (
        <div className="px-3 py-3 text-[13px] text-muted">{i18nT('components.selectionActions.too_short', { title: item.title, min: item.minLength, count: text.trim().length })}</div>
      ) : (
        <div
          ref={boxRef}
          className={failed ? 'hidden' : 'min-h-0 shrink'}
          style={{ height: Math.min(item.maxHeight, frameHeight) }}
        />
      )}
      {failed && view?.notice && (
        // No hand-off: the popover floats over a chat whose composer may hold an
        // unsent draft, and the hand-off navigates away from it.
        <ErrorNotice
          variant="inline"
          className="mx-3 my-3"
          message={view.notice.kind === 'no_response'
            ? i18nT('components.selectionActions.no_response', { app: item.appLabel })
            : view.notice.kind === 'revoked'
              ? i18nT('components.selectionActions.revoked', { app: item.appLabel })
              : i18nT('components.selectionActions.failed', { app: item.appLabel, message: view.notice.message })}
        />
      )}
      {copyFailed && (
        // No hand-off, as above: the draft stays put and Copy can be pressed again.
        <ErrorNotice variant="inline" className="mx-3 mb-2" message={i18nT('components.selectionActions.copy_failed')} onDismiss={() => setCopyFailed(false)} />
      )}
      {!failed && (view?.copy || view?.open) && (
        <div className="flex items-center justify-end gap-2 px-3 py-2 border-t border-border">
          {view?.open && (
            <Btn onClick={() => { if (view.open) window.open(view.open.href, '_blank', 'noopener,noreferrer') }}>
              <ExternalLink size={12} className="inline mr-1" />
              {i18nT('components.selectionActions.open', { host: view.open.hostname })}
            </Btn>
          )}
          {view?.copy && (
            <Btn primary onClick={() => { void onCopy() }}>
              {copied && <Check size={12} className="inline mr-1" />}
              {copied ? i18nT('components.selectionActions.copied') : i18nT('components.selectionActions.copy')}
            </Btn>
          )}
        </div>
      )}
    </div>
  )
}

function selectItems(d: unknown): ResolvedSelectionAction[] {
  const apps = Array.isArray(d) ? d : (d as { apps?: unknown[] })?.apps
  return resolveSelectionActions((apps as never[]) || [])
}

/**
 * `readApps`: whether this layer fetches the shared app list itself. The dashboard
 * shell passes `false`: its own nav read (`shell/appShell.tsx`) owns that list and
 * seeds the cache, so a second fetch here would duplicate the boot read. The
 * popped-out and embedded chats have no such read and keep the default.
 */
/** Whether the shared ['apps'] list is invalidated and no read has landed since.
 *  Read from the query cache, since a disabled observer never reports staleness. */
function useAppsInvalidated(queryClient: QueryClient): boolean {
  const cache = queryClient.getQueryCache()
  return useSyncExternalStore(
    (onChange) => cache.subscribe(onChange),
    () => queryClient.getQueryState(['apps'])?.isInvalidated === true,
    () => false,
  )
}

export default function SelectionActionLayer({ readApps = true }: { readApps?: boolean } = {}) {
  // The app list the link-preview layer reads too (one query, one cache entry).
  // The dashboard's shared app list decides one thing: whether any enabled app
  // has an approved item, so the toolbar shows the Apps button. The menu never
  // lists from it. The button hides only while a read of that list is in flight
  // or after a successful read with no approved item. When the read failed, or
  // the shell's own read has not landed, it shows (visibility only): the menu's
  // fresh read then lists the items or shows the error row with Retry, so a
  // failure is never silent. Items still come only from a successful fresh read.
  const queryClient = useQueryClient()
  const { data, isError, fetchStatus } = useQuery({
    queryKey: ['apps'],
    queryFn: () => (typeof api?.listApps === 'function' ? api.listApps() : []),
    select: selectItems,
    enabled: readApps,
  })
  // A failed refresh keeps the last list (often an empty one) as `data`, so the
  // failure is checked first. The shell's own read reports a failure only by
  // leaving the list invalidated (`mc:apps-changed`): that counts as failed until
  // a read lands.
  const invalidated = useAppsInvalidated(queryClient)
  const available = isError || invalidated || (data ? data.length > 0 : fetchStatus === 'idle')
  useEffect(() => { setSelectionActionsAvailable(available) }, [available])
  const state = useSelectionLayerState()
  // Behind a remote Crew the menu or popover closes, so its keys cannot reach it.
  useCloseWhenCrewHidden(state.kind !== 'none', closeSelectionLayer)
  if (state.kind === 'none') return null
  return (
    <OverlayPortal>
      {state.kind === 'menu'
        ? <AppsMenu key={state.seq} source={state.source} text={state.text} anchor={state.anchor} />
        : <ItemPopover key={state.seq} item={state.item} source={state.source} text={state.text} anchor={state.anchor} />}
    </OverlayPortal>
  )
}
