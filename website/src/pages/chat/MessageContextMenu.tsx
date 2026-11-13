import { Fragment, useEffect, useState, type ReactNode, type SyntheticEvent } from 'react'
import { OverlayPortal } from '../../components/overlayRoot'
import { ExternalLink, Image as ImageIcon, Link, Type } from 'lucide-react'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem, ContextMenuSeparator } from '../../components/ui/context-menu'
import { isTouchDevice } from '../../utils/isTouchDevice'
import { copyImageElementNatively, copyImageFromUrl, copyToClipboard, nativeImageCopyAvailable } from '../../utils/clipboard'
import { clipboardRelayAvailable, clipboardWriteBlockedByPolicy } from '../../lib/paneClipboardRelay'
import { i18nT } from '../../i18n/t'
import { safeHttpUrl } from '../../lib/safeUrl'

export interface MessageMenuItem {
  id: string
  label: string
  icon: ReactNode
  onSelect: () => void
  /** Draw a separator ABOVE this item. */
  separatorBefore?: boolean
  /** One muted line under the label, for an item whose label alone does not
   *  say what it is for. */
  hint?: string
}

interface LinkTarget { href: string; text: string }

/** The link under the gesture, bounded by the bubble so an anchor wrapping the
 *  whole transcript never counts. `href` is the resolved absolute URL, which is
 *  what the browser's own "Copy link address" copies. */
function linkAt(event: SyntheticEvent<HTMLElement>): LinkTarget | null {
  const target = event.target
  if (!(target instanceof Element)) return null
  const anchor = target.closest('a[href]')
  if (!(anchor instanceof HTMLAnchorElement) || !event.currentTarget.contains(anchor)) return null
  return { href: anchor.href, text: (anchor.textContent ?? '').trim() }
}

/** The loaded image under the gesture, bounded by the bubble. An image that
 *  failed to load has nothing to copy. */
function imageAt(event: SyntheticEvent<HTMLElement>): HTMLImageElement | null {
  const target = event.target
  if (!(target instanceof HTMLImageElement) || !event.currentTarget.contains(target)) return null
  if (!target.complete || target.naturalWidth === 0 || !(target.currentSrc || target.src)) return null
  return target
}

/** Whether a copy can fetch the image's bytes. A cross-origin image usually
 *  answers without CORS headers, so the copy would always fail. */
function fetchableImage(src: string): boolean {
  try {
    const url = new URL(src, location.href)
    return url.protocol === 'data:' || url.protocol === 'blob:' || url.origin === location.origin
  } catch {
    return false
  }
}

function canWriteImages(): boolean {
  if (typeof navigator === 'undefined' || !navigator.clipboard?.write || typeof ClipboardItem === 'undefined') return false
  return !clipboardWriteBlockedByPolicy() || clipboardRelayAvailable()
}

/** Whether the page's selection is non-empty text inside `root`. */
function selectionInside(root: HTMLElement): boolean {
  const selection = typeof window.getSelection === 'function' ? window.getSelection() : null
  if (!selection || selection.isCollapsed || selection.rangeCount === 0 || !selection.toString().trim()) return false
  return root.contains(selection.getRangeAt(0).commonAncestorContainer)
}

/**
 * Right-click menu on a message bubble.
 *
 * The bubble is the trigger, so the gesture works on the whole message: no
 * hover row to find, no text to select first. Radix supplies the keyboard form
 * (Shift+F10 / the Menu key on a focused bubble). Capability by omission: a
 * host that offers no items renders the children bare, so surfaces without the
 * actions keep their bubbles exactly as they were.
 *
 * A touch device renders the children bare too. Radix's touch form is a 700 ms
 * press, and the OS's own long-press-to-select fires first on the same bubble:
 * the menu then opens over the fresh selection, moves focus and collapses it,
 * and the trigger's `-webkit-touch-callout: none` removes iOS's own
 * Copy / Look Up callout as well. On touch the message's text belongs to the
 * platform's selection; the action row below the bubble carries the actions.
 *
 * Deliberately does NOT own any message action: the host lists the same
 * handlers its action row already has (quote, copy, copy link, pin, edit), so
 * the two entry points can never disagree about what a message can do.
 *
 * Opening this menu suppresses the browser's own, so a gesture that lands on a
 * link adds Open link in new tab (web links only, issue #22880), Copy link
 * address (the item the native and desktop menus offer) and Copy link text
 * (asked for by the reporter of issue #18536). The open takes the same
 * `window.open(..., 'noopener,noreferrer')` path as the other external links
 * here, so the desktop app hands it to the system browser. The link
 * is read on pointerdown as well as contextmenu, so it is known before Radix
 * opens the menu. A touch device gets no menu, so its native long-press keeps
 * the platform's own link actions.
 *
 * A gesture on a loaded image adds Copy image, for the same reason. Where the
 * page cannot fetch the image (no async Clipboard API on a plain-HTTP gateway,
 * or a cross-origin image whose bytes CORS hides) the desktop shell copies the
 * drawn pixels instead; in a plain browser the item is left out there and the
 * rest of the menu stays.
 *
 * `yieldToSelection`: a right-click on a bubble holding a text selection gets
 * the browser's own menu instead, so Copy, Search and the rest act on what
 * was selected rather than on the whole message. Read on pointerdown, before
 * a platform's right-click word selection can change it.
 */
export default function MessageContextMenu({ items, children, onOpenChange, onCopyFailed, yieldToSelection = false }: { items: MessageMenuItem[]; children: ReactNode; onOpenChange?: (open: boolean) => void; onCopyFailed?: () => void; yieldToSelection?: boolean }) {
  const [link, setLink] = useState<LinkTarget | null>(null)
  const [image, setImage] = useState<HTMLImageElement | null>(null)
  const [gesturePoint, setGesturePoint] = useState<{ x: number; y: number } | null>(null)
  const [copiedAt, setCopiedAt] = useState<{ x: number; y: number } | null>(null)
  const [selectionHeld, setSelectionHeld] = useState(false)
  useEffect(() => {
    if (!copiedAt) return
    const timer = setTimeout(() => setCopiedAt(null), 1500)
    return () => clearTimeout(timer)
  }, [copiedAt])
  if (!items.length || isTouchDevice()) return <>{children}</>
  // Functional and identity-preserving, so a pointerdown that does not change
  // the link under it does not re-render the bubble.
  const track = (event: SyntheticEvent<HTMLElement>) => {
    const next = linkAt(event)
    setLink(prev => (prev?.href === next?.href && prev?.text === next?.text ? prev : next))
    const img = imageAt(event)
    setImage(img)
    const native = event.nativeEvent
    setGesturePoint(img && native instanceof MouseEvent ? { x: native.clientX, y: native.clientY } : null)
  }
  const trackPointer = (event: SyntheticEvent<HTMLElement>) => {
    track(event)
    if (yieldToSelection) setSelectionHeld(selectionInside(event.currentTarget))
  }
  const copy = (text: string) => {
    copyToClipboard(text).then(ok => { if (!ok) onCopyFailed?.() }, () => onCopyFailed?.())
  }
  const imageSrc = image ? image.currentSrc || image.src : ''
  const fetchCopy = !!image && canWriteImages() && fetchableImage(imageSrc)
  const nativeCopy = !!image && !fetchCopy && nativeImageCopyAvailable()
  const copyImage = (img: HTMLImageElement) => {
    const point = gesturePoint
    const done = nativeCopy ? copyImageElementNatively(img, 1000, point) : copyImageFromUrl(imageSrc)
    done.then(ok => {
      if (!ok) { onCopyFailed?.(); return }
      const r = img.getBoundingClientRect()
      setCopiedAt(point ?? { x: (r.left + r.right) / 2, y: (r.top + r.bottom) / 2 })
    }, () => onCopyFailed?.())
  }
  const imageCopyable = fetchCopy || nativeCopy
  const targetItems: MessageMenuItem[] = [
    ...(link ? [
      // Same gate the renderer uses for these links: http(s) only, no userinfo.
      ...(safeHttpUrl(link.href) ? [{ id: 'open-link-new-tab', label: i18nT('pages.chat.messageContextMenu.open_link_new_tab'), icon: <ExternalLink size={14} />, onSelect: () => { window.open(link.href, '_blank', 'noopener,noreferrer') } }] : []),
      { id: 'copy-link-address', label: i18nT('pages.chat.messageContextMenu.copy_link_address'), icon: <Link size={14} />, onSelect: () => copy(link.href) },
      ...(link.text ? [{ id: 'copy-link-text', label: i18nT('pages.chat.messageContextMenu.copy_link_text'), icon: <Type size={14} />, onSelect: () => copy(link.text) }] : []),
    ] : []),
    ...(image && imageCopyable ? [{ id: 'copy-image', label: i18nT('components.markdownRenderer.copy_image'), icon: <ImageIcon size={14} />, onSelect: () => copyImage(image) }] : []),
  ]
  const all = targetItems.length
    ? [...targetItems, { ...items[0], separatorBefore: true }, ...items.slice(1)]
    : items
  return (
    <ContextMenu onOpenChange={onOpenChange}>
      <ContextMenuTrigger asChild disabled={yieldToSelection && selectionHeld} onContextMenu={track} onPointerDown={trackPointer}>{children}</ContextMenuTrigger>
      <ContextMenuContent className="min-w-[220px]" data-testid="message-context-menu">
        {all.map(item => (
          <Fragment key={item.id}>
            {item.separatorBefore && <ContextMenuSeparator />}
            <ContextMenuItem onSelect={item.onSelect} data-testid={`message-context-${item.id}`}>
              {/* Layout and the touch floor live on this span: the primitive owns
                  its own classes (shadcn/no-restyle). */}
              <span className="flex items-center gap-2 [@media(hover:none)]:min-h-7">
                <span className="shrink-0 inline-flex text-muted">{item.icon}</span>
                {item.hint
                  ? <span className="flex flex-col gap-0.5"><span>{item.label}</span><span className="text-[11px] leading-4 text-muted">{item.hint}</span></span>
                  : <span>{item.label}</span>}
              </span>
            </ContextMenuItem>
          </Fragment>
        ))}
      </ContextMenuContent>
      {/* The live region stays inline and mounted so it exists before its text
          arrives; only the visible pill is portalled, and only while shown. */}
      <span className="sr-only" aria-live="polite" data-testid="message-context-image-copied-status">
        {copiedAt ? i18nT('components.markdownRenderer.image_copied') : ''}
      </span>
      {copiedAt && (
        <OverlayPortal>
          <div
            data-testid="message-context-image-copied"
            aria-hidden="true"
            className="fixed z-50 pointer-events-none -translate-x-1/2 -translate-y-full rounded-full bg-black/60 backdrop-blur-md ring-1 ring-white/15 shadow-lg px-3 py-1 text-sm text-white/90"
            style={{ left: Math.min(Math.max(copiedAt.x, 72), window.innerWidth - 72), top: Math.max(copiedAt.y - 8, 40) }}
          >
            {i18nT('components.markdownRenderer.image_copied')}
          </div>
        </OverlayPortal>
      )}
    </ContextMenu>
  )
}
