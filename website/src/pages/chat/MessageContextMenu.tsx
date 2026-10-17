import { Fragment, useState, type ReactNode, type SyntheticEvent } from 'react'
import { Link, Type } from 'lucide-react'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem, ContextMenuSeparator } from '../../components/ui/context-menu'
import { isTouchDevice } from '../../utils/isTouchDevice'
import { copyToClipboard } from '../../utils/clipboard'
import { i18nT } from '../../i18n/t'

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
 * link adds Copy link address (the item the native and desktop menus offer)
 * and Copy link text (asked for by the reporter of issue #18536). The link
 * is read on pointerdown as well as contextmenu, so it is known before Radix
 * opens the menu. A touch device gets no menu, so its native long-press keeps
 * the platform's own link actions.
 *
 * `yieldToSelection`: a right-click on a bubble holding a text selection gets
 * the browser's own menu instead, so Copy, Search and the rest act on what
 * was selected rather than on the whole message. Read on pointerdown, before
 * a platform's right-click word selection can change it.
 */
export default function MessageContextMenu({ items, children, onOpenChange, onCopyFailed, yieldToSelection = false }: { items: MessageMenuItem[]; children: ReactNode; onOpenChange?: (open: boolean) => void; onCopyFailed?: () => void; yieldToSelection?: boolean }) {
  const [link, setLink] = useState<LinkTarget | null>(null)
  const [selectionHeld, setSelectionHeld] = useState(false)
  if (!items.length || isTouchDevice()) return <>{children}</>
  // Functional and identity-preserving, so a pointerdown that does not change
  // the link under it does not re-render the bubble.
  const track = (event: SyntheticEvent<HTMLElement>) => {
    const next = linkAt(event)
    setLink(prev => (prev?.href === next?.href && prev?.text === next?.text ? prev : next))
  }
  const trackPointer = (event: SyntheticEvent<HTMLElement>) => {
    track(event)
    if (yieldToSelection) setSelectionHeld(selectionInside(event.currentTarget))
  }
  const copy = (text: string) => {
    copyToClipboard(text).then(ok => { if (!ok) onCopyFailed?.() }, () => onCopyFailed?.())
  }
  const linkItems: MessageMenuItem[] = link ? [
    { id: 'copy-link-address', label: i18nT('pages.chat.messageContextMenu.copy_link_address'), icon: <Link size={14} />, onSelect: () => copy(link.href) },
    ...(link.text ? [{ id: 'copy-link-text', label: i18nT('pages.chat.messageContextMenu.copy_link_text'), icon: <Type size={14} />, onSelect: () => copy(link.text) }] : []),
  ] : []
  const all = linkItems.length
    ? [...linkItems, { ...items[0], separatorBefore: true }, ...items.slice(1)]
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
    </ContextMenu>
  )
}
