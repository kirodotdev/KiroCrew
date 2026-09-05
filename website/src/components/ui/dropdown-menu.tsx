import * as React from 'react'
import * as DropdownMenuPrimitive from '@radix-ui/react-dropdown-menu'
import { cn } from '../../lib/utils'
import { useIsCoarsePointer } from '../../hooks/useIsCoarsePointer'
import { PhoneSubContentDiv, PhoneSubTriggerDiv, usePhoneSubState } from './phoneSubmenu'

const DropdownMenu = DropdownMenuPrimitive.Root
const DropdownMenuTrigger = DropdownMenuPrimitive.Trigger
const DropdownMenuGroup = DropdownMenuPrimitive.Group
const DropdownMenuPortal = DropdownMenuPrimitive.Portal
const DropdownMenuRadioGroup = DropdownMenuPrimitive.RadioGroup

type SubPhoneContextValue = { isPhone: boolean; expanded: boolean; toggle: () => void }
const DropdownSubPhoneContext = React.createContext<SubPhoneContextValue | null>(null)

const DropdownMenuSub = React.forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Sub> & { className?: string }
>(({ children, className, open, defaultOpen, onOpenChange, ...rest }, ref) => {
  const isPhone = useIsCoarsePointer()
  const { expanded, toggle } = usePhoneSubState(open, defaultOpen, onOpenChange)
  if (isPhone) {
    return (
      <DropdownSubPhoneContext.Provider value={{ isPhone: true, expanded, toggle }}>
        <div ref={ref} className={cn('w-full', className)} {...(rest as React.HTMLAttributes<HTMLDivElement>)}>
          {children}
        </div>
      </DropdownSubPhoneContext.Provider>
    )
  }
  // Radix Sub is a logical container with no DOM — className only applies to the phone branch.
  return (
    <DropdownMenuPrimitive.Sub open={open} defaultOpen={defaultOpen} onOpenChange={onOpenChange} {...rest}>{children}</DropdownMenuPrimitive.Sub>
  )
})
DropdownMenuSub.displayName = 'DropdownMenuSub'

const DropdownMenuContent = React.forwardRef<
  React.ComponentRef<typeof DropdownMenuPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Content>
>(({ className, sideOffset = 4, ...props }, ref) => (
  <DropdownMenuPrimitive.Portal>
    <DropdownMenuPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      className={cn(
        // Cap the height to the space Radix measured between the trigger and the
        // viewport edge (its own collision var) and scroll the overflow, so a
        // menu taller than the room below the trigger stays fully reachable on a
        // short viewport (mobile) instead of clipping its bottom items off-screen
        // with `overflow-hidden`. Radix already flips the menu above the trigger
        // when that side has more room; this handles the case where neither side
        // is tall enough.
        'z-[9999] min-w-[8rem] max-h-[var(--radix-dropdown-menu-content-available-height)] overflow-y-auto overscroll-contain rounded-lg border border-border bg-bg-elevated p-1 text-text shadow-lg',
        // Entry animation only. Radix suspends unmount until an exit animation
        // finishes, and the still-mounted dismissable layer consumes the next
        // pointer-down — so an exit animation makes a re-click on the trigger a
        // no-op for the animation's whole duration.
        'data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95',
        'data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2',
        className
      )}
      {...props}
    />
  </DropdownMenuPrimitive.Portal>
))
DropdownMenuContent.displayName = DropdownMenuPrimitive.Content.displayName

const DropdownMenuItem = React.forwardRef<
  React.ComponentRef<typeof DropdownMenuPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Item> & { inset?: boolean }
>(({ className, inset, ...props }, ref) => (
  <DropdownMenuPrimitive.Item
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
DropdownMenuItem.displayName = DropdownMenuPrimitive.Item.displayName

/**
 * A menu row that reports WHICH option is currently in effect. Screen readers
 * announce a `menuitemradio` as checked/unchecked, which a plain item cannot
 * convey — so a menu that stands in for a set of mutually exclusive
 * destinations (a switcher) must use this rather than styling alone.
 */
const DropdownMenuRadioItem = React.forwardRef<
  React.ComponentRef<typeof DropdownMenuPrimitive.RadioItem>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.RadioItem>
>(({ className, ...props }, ref) => (
  <DropdownMenuPrimitive.RadioItem
    ref={ref}
    className={cn(
      'relative flex cursor-pointer select-none items-center gap-2 rounded-md px-3 py-1.5 text-[13px] outline-none transition-colors',
      'focus:bg-bg-hover data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
      className
    )}
    {...props}
  />
))
DropdownMenuRadioItem.displayName = DropdownMenuPrimitive.RadioItem.displayName

const DropdownMenuSeparator = React.forwardRef<
  React.ComponentRef<typeof DropdownMenuPrimitive.Separator>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Separator>
>(({ className, ...props }, ref) => (
  <DropdownMenuPrimitive.Separator
    ref={ref}
    className={cn('mx-1 my-1 h-px bg-border', className)}
    {...props}
  />
))
DropdownMenuSeparator.displayName = DropdownMenuPrimitive.Separator.displayName

const DropdownMenuLabel = React.forwardRef<
  React.ComponentRef<typeof DropdownMenuPrimitive.Label>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Label> & { inset?: boolean }
>(({ className, inset, ...props }, ref) => (
  <DropdownMenuPrimitive.Label
    ref={ref}
    className={cn('px-3 py-1.5 text-[12px] font-semibold text-muted', inset && 'pl-8', className)}
    {...props}
  />
))
DropdownMenuLabel.displayName = DropdownMenuPrimitive.Label.displayName

const DropdownMenuSubTrigger = React.forwardRef<
  React.ComponentRef<typeof DropdownMenuPrimitive.SubTrigger>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.SubTrigger> & { inset?: boolean }
>(({ className, inset, children, onClick, onKeyDown, ...props }, ref) => {
  const ctx = React.useContext(DropdownSubPhoneContext)
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
    <DropdownMenuPrimitive.SubTrigger
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
    </DropdownMenuPrimitive.SubTrigger>
  )
})
DropdownMenuSubTrigger.displayName = DropdownMenuPrimitive.SubTrigger.displayName

const DropdownMenuSubContent = React.forwardRef<
  React.ComponentRef<typeof DropdownMenuPrimitive.SubContent>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.SubContent>
>(({ className, children, ...props }, ref) => {
  const ctx = React.useContext(DropdownSubPhoneContext)
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
    <DropdownMenuPrimitive.Portal>
      <DropdownMenuPrimitive.SubContent
        ref={ref}
        className={cn(
          'z-[9999] min-w-[8rem] max-h-[var(--radix-dropdown-menu-content-available-height)] overflow-y-auto overscroll-contain rounded-lg border border-border bg-bg-elevated p-1 text-text shadow-lg',
          'data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95',
          className
        )}
        {...props}
      >
        {children}
      </DropdownMenuPrimitive.SubContent>
    </DropdownMenuPrimitive.Portal>
  )
})
DropdownMenuSubContent.displayName = DropdownMenuPrimitive.SubContent.displayName

export {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuLabel,
  DropdownMenuGroup,
  DropdownMenuPortal,
  DropdownMenuSub,
  DropdownMenuSubTrigger,
  DropdownMenuSubContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
}
