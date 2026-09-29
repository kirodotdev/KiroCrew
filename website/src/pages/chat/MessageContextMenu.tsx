import { Fragment, type ReactNode } from 'react'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem, ContextMenuSeparator } from '../../components/ui/context-menu'

export interface MessageMenuItem {
  id: string
  label: string
  icon: ReactNode
  onSelect: () => void
  /** Draw a separator ABOVE this item. */
  separatorBefore?: boolean
}

/**
 * Right-click / long-press menu on a message bubble.
 *
 * The bubble is the trigger, so the gesture works on the whole message: no
 * hover row to find, no text to select first. Radix supplies the coarse-pointer
 * form (a ~700 ms press) and the keyboard form (Shift+F10 / the Menu key on a
 * focused bubble). Capability by omission: a host that offers no items renders
 * the children bare, so surfaces without the actions keep their bubbles exactly
 * as they were.
 *
 * Deliberately does NOT own any action: the host lists the same handlers its
 * action row already has (quote, copy, copy link, pin, edit), so the two entry
 * points can never disagree about what a message can do.
 */
export default function MessageContextMenu({ items, children, onOpenChange }: { items: MessageMenuItem[]; children: ReactNode; onOpenChange?: (open: boolean) => void }) {
  if (!items.length) return <>{children}</>
  return (
    <ContextMenu onOpenChange={onOpenChange}>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent className="min-w-[220px]" data-testid="message-context-menu">
        {items.map(item => (
          <Fragment key={item.id}>
            {item.separatorBefore && <ContextMenuSeparator />}
            <ContextMenuItem onSelect={item.onSelect} data-testid={`message-context-${item.id}`}>
              {/* Layout and the touch floor live on this span: the primitive owns
                  its own classes (shadcn/no-restyle). */}
              <span className="flex items-center gap-2 [@media(hover:none)]:min-h-7">
                <span className="shrink-0 inline-flex text-muted">{item.icon}</span>
                <span>{item.label}</span>
              </span>
            </ContextMenuItem>
          </Fragment>
        ))}
      </ContextMenuContent>
    </ContextMenu>
  )
}
