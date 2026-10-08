import * as React from 'react'
import * as DropdownMenuPrimitive from '@radix-ui/react-dropdown-menu'
import { cn } from '../../lib/utils'
import { autoSiteRef } from '../../uiLocations/targetRegistry'
import { useCloseOnFileDrag } from '../../hooks/useCloseOnFileDrag'
import { useIsTouchDevice } from '../../hooks/useIsTouchDevice'
import { PhoneSubContentDiv, PhoneSubTriggerDiv, usePhoneSubState } from './phoneSubmenu'
import { MaybeGuideRevealScope } from '../../guide/GuideRevealScope'
import type { GuideRevealScopeId } from '../../uiLocations/guidePlans.gen'
import { createTriggerContext, heldGuard, ProbeHoldContext, useProbeHold, useProbeTarget, useTriggerRef } from '../../guide/probeRegistry'
import { useGuideTrustRootAttrs } from '../../guide/trustRoot'

type DropdownMenuProps = React.ComponentProps<typeof DropdownMenuPrimitive.Root> & {
  /**
   * The compiled reveal scope this menu is (`menu:<trigger location id>`), for
   * a menu whose trigger or items are registered UI locations. The menu then
   * reports open/closed to a running guide, which points at the trigger until
   * it reads open. Reporting only: nothing here ever opens the menu.
   */
  guideScope?: GuideRevealScopeId
  /**
   * Whether a guide's `ui.find` probe may open this menu to look inside
   * (`guide/probeRegistry.ts`). Left unset, it may exactly when nothing
   * outside the menu hears it open: uncontrolled, with no `onOpenChange`.
   * `true` declares that opening it has no effect of its own; `false` keeps
   * it closed to the probe whatever its wiring.
   */
  guideProbe?: boolean
}

const DropdownTriggerSink = createTriggerContext()

/**
 * Radix `DropdownMenu.Root`, plus two rules.
 *
 * Non-modal by default on touch devices. A modal Radix menu sets
 * `pointer-events: none` on `document.body` while open, so on a phone the
 * first tap outside the menu only closes it and the control under the finger
 * never gets the tap; the user has to tap again. Non-modal, Radix dismisses a
 * touch outside on the tap's own `click`, after the tapped control has handled
 * it, so one tap closes the menu and activates the control. Without the modal
 * scroll lock the page behind can scroll; the menu stays anchored to its
 * trigger while it does, the same as every Popover here. Mouse devices keep
 * the modal default, and an explicit `modal` prop wins on every device.
 *
 * An open modal menu closes the moment a file drag from outside the page
 * enters the window, so the chat composer's drop zone can receive the drop.
 * The mechanism is documented on `useCloseOnFileDrag`; `ContextMenu` applies
 * the same rule.
 *
 * `guideScope` makes the menu a guide reveal scope (see the prop).
 *
 * Controlled (`open`) and uncontrolled (`defaultOpen`) usage both work: the
 * close goes through the same path as a click-outside, so `onOpenChange(false)`
 * fires for callers that track the state themselves.
 */
function DropdownMenu({ open: openProp, defaultOpen, onOpenChange, modal: modalProp, guideScope, guideProbe, ...rest }: DropdownMenuProps) {
  const isTouch = useIsTouchDevice()
  const isControlled = openProp !== undefined
  const [uncontrolledOpen, setUncontrolledOpen] = React.useState(defaultOpen ?? false)
  const open = isControlled ? openProp : uncontrolledOpen
  const probeHold = useProbeHold(open)
  // A menu a probe opened is non-modal until it closes: a modal one would
  // hide the rest of the page from assistive tech and take its pointer.
  const modal = probeHold.heldOpen ? false : (modalProp ?? !isTouch)

  const handleOpenChange = React.useCallback((next: boolean) => {
    if (!isControlled) setUncontrolledOpen(next)
    onOpenChange?.(next)
  }, [isControlled, onOpenChange])
  const close = React.useCallback(() => handleOpenChange(false), [handleOpenChange])

  useCloseOnFileDrag(open && modal, close)

  // A probe opens the menu through this state and closes it the same way.
  const trigger = React.useRef<HTMLElement | null>(null)
  const openRef = React.useRef(open)
  openRef.current = open
  const setTrigger = React.useCallback((el: HTMLElement | null) => { trigger.current = el }, [])
  useProbeTarget(guideProbe ?? (!isControlled && !onOpenChange), {
    kind: 'popup',
    trigger: () => trigger.current,
    isOpen: () => openRef.current,
    open: () => {
      probeHold.hold()
      handleOpenChange(true)
      return () => { if (openRef.current) handleOpenChange(false) }
    },
  })

  // The scope owner sits outside the Root, so it reports "closed" while the
  // portalled content is unmounted; the content still reads it through context.
  return (
    <MaybeGuideRevealScope id={guideScope} open={open}>
      <DropdownTriggerSink.Provider value={setTrigger}>
        <ProbeHoldContext.Provider value={probeHold.held}>
          <DropdownMenuPrimitive.Root open={open} onOpenChange={handleOpenChange} modal={modal} {...rest} />
        </ProbeHoldContext.Provider>
      </DropdownTriggerSink.Provider>
    </MaybeGuideRevealScope>
  )
}

/** Radix `DropdownMenu.Trigger`; it also tells its menu which element it is (for a guide's probe). */
const DropdownMenuTrigger = React.forwardRef<
  React.ComponentRef<typeof DropdownMenuPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Trigger>
>(function DropdownMenuTrigger(props, ref) {
  const setRef = useTriggerRef(DropdownTriggerSink, ref)
  return <DropdownMenuPrimitive.Trigger ref={setRef} {...props} />
})
DropdownMenuTrigger.displayName = DropdownMenuPrimitive.Trigger.displayName
const DropdownMenuGroup = DropdownMenuPrimitive.Group
const DropdownMenuPortal = DropdownMenuPrimitive.Portal
const DropdownMenuRadioGroup = DropdownMenuPrimitive.RadioGroup

type SubPhoneContextValue = { isPhone: boolean; expanded: boolean; toggle: () => void }
const DropdownSubPhoneContext = React.createContext<SubPhoneContextValue | null>(null)

const DropdownMenuSub = React.forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Sub>
>(function DropdownMenuSub({ children, open, defaultOpen, onOpenChange, ...rest }, ref) {
  const isPhone = useIsTouchDevice()
  const { expanded, toggle } = usePhoneSubState(open, defaultOpen, onOpenChange)
  if (isPhone) {
    return (
      <DropdownSubPhoneContext.Provider value={{ isPhone: true, expanded, toggle }}>
        <div ref={ref} className="w-full" {...(rest as React.HTMLAttributes<HTMLDivElement>)}>
          {children}
        </div>
      </DropdownSubPhoneContext.Provider>
    )
  }
  return (
    <DropdownMenuPrimitive.Sub open={open} defaultOpen={defaultOpen} onOpenChange={onOpenChange} {...rest}>{children}</DropdownMenuPrimitive.Sub>
  )
})

const DropdownMenuContent = React.forwardRef<
  React.ComponentRef<typeof DropdownMenuPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Content>
>(({ className, sideOffset = 4, onCloseAutoFocus, onFocusOutside, onInteractOutside, ...props }, ref) => {
  const held = React.useContext(ProbeHoldContext)
  const trustRoot = useGuideTrustRootAttrs()
  // `onOpenAutoFocus` is not in Content's public props, but Radix reads it from them.
  const probeFocus = { onOpenAutoFocus: heldGuard<Event>(held, undefined) }
  return (
  <DropdownMenuPrimitive.Portal>
    <DropdownMenuPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      {...probeFocus}
      onCloseAutoFocus={heldGuard(held, onCloseAutoFocus)}
      onFocusOutside={heldGuard(held, onFocusOutside)}
      onInteractOutside={heldGuard(held, onInteractOutside)}
      {...trustRoot}
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
  )
})
DropdownMenuContent.displayName = DropdownMenuPrimitive.Content.displayName

const DropdownMenuItem = React.forwardRef<
  React.ComponentRef<typeof DropdownMenuPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Item> & { inset?: boolean }
>(({ className, inset, ...props }, ref) => (
  <DropdownMenuPrimitive.Item
    ref={autoSiteRef((props as Record<string, unknown>)['data-ui-auto'], ref)}
    className={cn(
      'relative flex cursor-pointer select-none items-center gap-2 rounded-md px-3 py-1.5 text-[13px] outline-hidden transition-colors',
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
      'relative flex cursor-pointer select-none items-center gap-2 rounded-md px-3 py-1.5 text-[13px] outline-hidden transition-colors',
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
        'relative flex cursor-pointer select-none items-center gap-2 rounded-md px-3 py-1.5 text-[13px] outline-hidden transition-colors',
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
  const trustRoot = useGuideTrustRootAttrs()
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
        {...trustRoot}
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
