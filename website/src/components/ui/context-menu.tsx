import * as React from 'react'
import * as ContextMenuPrimitive from '@radix-ui/react-context-menu'
import { cn } from '../../lib/utils'
import { useIsCoarsePointer } from '../../hooks/useIsCoarsePointer'
import { PhoneSubContentDiv, PhoneSubTriggerDiv, usePhoneSubState } from './phoneSubmenu'

const ContextMenu = ContextMenuPrimitive.Root
const ContextMenuTrigger = ContextMenuPrimitive.Trigger
const ContextMenuGroup = ContextMenuPrimitive.Group
const ContextMenuPortal = ContextMenuPrimitive.Portal
const ContextMenuRadioGroup = ContextMenuPrimitive.RadioGroup

type ContextSubPhoneContextValue = { isPhone: boolean; expanded: boolean; toggle: () => void }
const ContextSubPhoneContext = React.createContext<ContextSubPhoneContextValue | null>(null)

const ContextMenuSub = React.forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<typeof ContextMenuPrimitive.Sub> & { className?: string }
>(({ children, className, open, defaultOpen, onOpenChange, ...rest }, ref) => {
  const isPhone = useIsCoarsePointer()
  const { expanded, toggle } = usePhoneSubState(open, defaultOpen, onOpenChange)
  if (isPhone) {
    return (
      <ContextSubPhoneContext.Provider value={{ isPhone: true, expanded, toggle }}>
        <div ref={ref} className={cn('w-full', className)} {...(rest as React.HTMLAttributes<HTMLDivElement>)}>
          {children}
        </div>
      </ContextSubPhoneContext.Provider>
    )
  }
  // Radix Sub is a logical container — className only for phone inline branch.
  return <ContextMenuPrimitive.Sub open={open} defaultOpen={defaultOpen} onOpenChange={onOpenChange} {...rest}>{children}</ContextMenuPrimitive.Sub>
})
ContextMenuSub.displayName = 'ContextMenuSub'

const ContextMenuContent = React.forwardRef<
  React.ComponentRef<typeof ContextMenuPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof ContextMenuPrimitive.Content>
>(({ className, ...props }, ref) => (
  <ContextMenuPrimitive.Portal>
    <ContextMenuPrimitive.Content
      ref={ref}
      className={cn(
        // Cap the height to the space Radix measured between the anchor and the
        // viewport edge (its own collision var) and scroll the overflow, so a
        // menu taller than the room below stays fully reachable on a short
        // viewport (mobile) instead of clipping its bottom items off-screen with
        // `overflow-hidden`. Radix already flips the menu above the anchor when
        // that side has more room; this handles the case where neither side is
        // tall enough.
        'z-[9999] min-w-[8rem] max-h-[var(--radix-context-menu-content-available-height)] overflow-y-auto overscroll-contain rounded-lg border border-border bg-bg-elevated p-1 text-text shadow-lg',
        // Entry animation only. Radix suspends unmount until an exit animation
        // finishes, and the still-mounted dismissable layer consumes the next
        // pointer-down — so an exit animation makes an immediate re-open (here,
        // a right-click elsewhere) a no-op for the animation's whole duration.
        'animate-in fade-in-0 zoom-in-95',
        'data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2',
        className
      )}
      {...props}
    />
  </ContextMenuPrimitive.Portal>
))
ContextMenuContent.displayName = ContextMenuPrimitive.Content.displayName

const ContextMenuItem = React.forwardRef<
  React.ComponentRef<typeof ContextMenuPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof ContextMenuPrimitive.Item> & { inset?: boolean }
>(({ className, inset, ...props }, ref) => (
  <ContextMenuPrimitive.Item
    ref={ref}
    className={cn(
      'relative flex cursor-pointer select-none items-center gap-2 rounded-md px-3 py-1.5 text-[13px] outline-none transition-colors',
      'focus:bg-bg-hover data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
      inset && 'pl-8',
      className
    )}
    {...props}
  />
))
ContextMenuItem.displayName = ContextMenuPrimitive.Item.displayName

const ContextMenuSeparator = React.forwardRef<
  React.ComponentRef<typeof ContextMenuPrimitive.Separator>,
  React.ComponentPropsWithoutRef<typeof ContextMenuPrimitive.Separator>
>(({ className, ...props }, ref) => (
  <ContextMenuPrimitive.Separator
    ref={ref}
    className={cn('mx-1 my-1 h-px bg-border', className)}
    {...props}
  />
))
ContextMenuSeparator.displayName = ContextMenuPrimitive.Separator.displayName

const ContextMenuSubTrigger = React.forwardRef<
  React.ComponentRef<typeof ContextMenuPrimitive.SubTrigger>,
  React.ComponentPropsWithoutRef<typeof ContextMenuPrimitive.SubTrigger> & { inset?: boolean }
>(({ className, inset, children, onClick, onKeyDown, ...props }, ref) => {
  const ctx = React.useContext(ContextSubPhoneContext)
  if (ctx?.isPhone) {
    return (
      <PhoneSubTriggerDiv
        ref={ref as React.Ref<HTMLDivElement>}
        inset={inset}
        expanded={ctx.expanded}
        onToggle={ctx.toggle}
        className={className}
        onClick={onClick as unknown as React.MouseEventHandler<HTMLDivElement> | undefined}
        onKeyDown={onKeyDown as unknown as React.KeyboardEventHandler<HTMLDivElement> | undefined}
        {...(props as React.HTMLAttributes<HTMLDivElement>)}
      >
        {children}
      </PhoneSubTriggerDiv>
    )
  }
  return (
    <ContextMenuPrimitive.SubTrigger
      ref={ref}
      className={cn(
        'relative flex cursor-pointer select-none items-center gap-2 rounded-md px-3 py-1.5 text-[13px] outline-none transition-colors',
        'focus:bg-bg-hover data-[state=open]:bg-bg-hover',
        inset && 'pl-8',
        className
      )}
      onClick={onClick as unknown as React.MouseEventHandler<HTMLDivElement> | undefined}
      onKeyDown={onKeyDown as unknown as React.KeyboardEventHandler<HTMLDivElement> | undefined}
      {...props}
    >
      {children}
    </ContextMenuPrimitive.SubTrigger>
  )
})
ContextMenuSubTrigger.displayName = ContextMenuPrimitive.SubTrigger.displayName

const ContextMenuSubContent = React.forwardRef<
  React.ComponentRef<typeof ContextMenuPrimitive.SubContent>,
  React.ComponentPropsWithoutRef<typeof ContextMenuPrimitive.SubContent>
>(({ className, children, ...props }, ref) => {
  const ctx = React.useContext(ContextSubPhoneContext)
  if (ctx?.isPhone) {
    if (!ctx.expanded) return null
    return (
      <PhoneSubContentDiv
        ref={ref as React.Ref<HTMLDivElement>}
        className={className}
        {...(props as React.HTMLAttributes<HTMLDivElement>)}
      >
        {children}
      </PhoneSubContentDiv>
    )
  }
  return (
    <ContextMenuPrimitive.Portal>
      <ContextMenuPrimitive.SubContent
        ref={ref}
        className={cn(
          'z-[9999] min-w-[8rem] max-h-[var(--radix-context-menu-content-available-height)] overflow-y-auto overscroll-contain rounded-lg border border-border bg-bg-elevated p-1 text-text shadow-lg',
          'data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95',
          className
        )}
        {...props}
      >
        {children}
      </ContextMenuPrimitive.SubContent>
    </ContextMenuPrimitive.Portal>
  )
})
ContextMenuSubContent.displayName = ContextMenuPrimitive.SubContent.displayName

export {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuGroup,
  ContextMenuPortal,
  ContextMenuSub,
  ContextMenuSubTrigger,
  ContextMenuSubContent,
  ContextMenuRadioGroup,
}
