import * as React from 'react'
import * as PopoverPrimitive from '@radix-ui/react-popover'

import { cn } from '../../lib/utils'
import { MaybeGuideRevealScope } from '../../guide/GuideRevealScope'
import { createTriggerContext, heldGuard, ProbeHoldContext, useProbeHold, useProbeTarget, useTriggerRef } from '../../guide/probeRegistry'
import { useGuideTrustRootAttrs } from '../../guide/trustRoot'
import type { GuideRevealScopeId } from '../../uiLocations/guidePlans.gen'

type PopoverProps = React.ComponentProps<typeof PopoverPrimitive.Root> & {
  /**
   * The compiled reveal scope this popover is (`menu:<trigger location id>`),
   * for one whose trigger or contents are registered UI locations: it reports
   * open/closed to a running guide. Reporting only; nothing here opens it.
   */
  guideScope?: GuideRevealScopeId
  /**
   * Whether a guide's `ui.find` probe may open this popover to look inside
   * (`guide/probeRegistry.ts`). Left unset, it may exactly when nothing
   * outside the popover hears it open: uncontrolled, with no `onOpenChange`.
   * `true` declares that opening it has no effect of its own; `false` keeps
   * it closed to the probe whatever its wiring.
   */
  guideProbe?: boolean
}

const PopoverTriggerSink = createTriggerContext()

/**
 * Radix `Popover.Root`; with `guideScope`, also a guide reveal scope owner.
 * It always holds the open state itself (an uncontrolled caller included), so
 * a guide's probe opens and closes it through that state, never by a press.
 */
function Popover({ guideScope, guideProbe, open: openProp, defaultOpen, onOpenChange, ...rest }: PopoverProps) {
  const [inner, setInner] = React.useState(defaultOpen ?? false)
  const isControlled = openProp !== undefined
  const handleOpenChange = React.useCallback((next: boolean) => {
    if (!isControlled) setInner(next)
    onOpenChange?.(next)
  }, [isControlled, onOpenChange])
  const open = isControlled ? openProp : inner
  const probeHold = useProbeHold(open)
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
  // Outside the Root, so "closed" is said while the portalled content is unmounted.
  return (
    <MaybeGuideRevealScope id={guideScope} open={open}>
      <PopoverTriggerSink.Provider value={setTrigger}>
        <ProbeHoldContext.Provider value={probeHold.held}>
          <PopoverPrimitive.Root open={open} onOpenChange={handleOpenChange} {...rest} />
        </ProbeHoldContext.Provider>
      </PopoverTriggerSink.Provider>
    </MaybeGuideRevealScope>
  )
}

/** Radix `Popover.Trigger`; it also tells its popover which element it is (for a guide's probe). */
const PopoverTrigger = React.forwardRef<
  React.ComponentRef<typeof PopoverPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Trigger>
>(function PopoverTrigger(props, ref) {
  const setRef = useTriggerRef(PopoverTriggerSink, ref)
  return <PopoverPrimitive.Trigger ref={setRef} {...props} />
})
PopoverTrigger.displayName = PopoverPrimitive.Trigger.displayName
const PopoverAnchor = PopoverPrimitive.Anchor

/** `default` is the padded card; `list` is a picker list (a tight inset, the
 *  larger Glass radius and a deeper shadow), so a list caller does not restyle
 *  the primitive from outside. */
const POPOVER_VARIANTS = {
  default: 'rounded-md p-4 shadow-md',
  list: 'rounded-2xl p-1.5 shadow-lg',
} as const

const PopoverContent = React.forwardRef<
  React.ComponentRef<typeof PopoverPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Content> & { variant?: keyof typeof POPOVER_VARIANTS }
>(({ className, align = 'center', sideOffset = 4, variant = 'default', onOpenAutoFocus, onCloseAutoFocus, onFocusOutside, onInteractOutside, ...props }, ref) => {
  const held = React.useContext(ProbeHoldContext)
  const trustRoot = useGuideTrustRootAttrs()
  return (
  <PopoverPrimitive.Portal>
    <PopoverPrimitive.Content
      ref={ref}
      align={align}
      sideOffset={sideOffset}
      onOpenAutoFocus={heldGuard(held, onOpenAutoFocus)}
      onCloseAutoFocus={heldGuard(held, onCloseAutoFocus)}
      onFocusOutside={heldGuard(held, onFocusOutside)}
      onInteractOutside={heldGuard(held, onInteractOutside)}
      {...trustRoot}
      className={cn(
        // Entry animation only. Radix suspends unmount until an exit animation
        // finishes, and the still-mounted dismissable layer consumes the next
        // pointer-down — so an exit animation makes a re-click on the trigger a
        // no-op for the animation's whole duration.
        'z-[9999] w-72 border border-border bg-bg-elevated text-text outline-hidden data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2',
        POPOVER_VARIANTS[variant],
        className
      )}
      {...props}
    />
  </PopoverPrimitive.Portal>
  )
})
PopoverContent.displayName = PopoverPrimitive.Content.displayName

export { Popover, PopoverTrigger, PopoverContent, PopoverAnchor }
