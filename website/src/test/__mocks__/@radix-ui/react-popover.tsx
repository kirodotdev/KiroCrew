import React, { useState, createContext, useContext } from 'react'
import type {
  PopoverAnchorProps,
  PopoverCloseProps,
  PopoverContentProps,
  PopoverPortalProps,
  PopoverProps,
  PopoverTriggerProps,
} from '@radix-ui/react-popover'

/**
 * The one thing the `asChild` clone paths read off the slotted child: its own
 * click handler, which must still fire before the mock toggles open state. Both
 * clone sites target a button-shaped trigger, hence the ref element.
 */
type SlottableChildProps = {
  onClick?: React.MouseEventHandler<HTMLElement>
  ref?: React.Ref<HTMLButtonElement>
}

/**
 * Real Radix renders Arrow as an `<svg>`, so its own `PopoverArrowProps` is
 * SVG-shaped. This mock renders a plain `<div>`, which div props describe.
 */
type ArrowProps = React.ComponentPropsWithoutRef<'div'>

const Ctx = createContext<{ open: boolean; setOpen: (v: boolean) => void }>({ open: false, setOpen: () => {} })

export const Root = ({ children, open: controlledOpen, onOpenChange }: PopoverProps) => {
  const [internalOpen, setInternalOpen] = useState(false)
  const open = controlledOpen ?? internalOpen
  const setOpen = (v: boolean) => { setInternalOpen(v); onOpenChange?.(v) }
  return <Ctx.Provider value={{ open, setOpen }}>{children}</Ctx.Provider>
}

export const Trigger = React.forwardRef<HTMLButtonElement, PopoverTriggerProps>(({ children, asChild, ...props }, ref) => {
  const { open, setOpen } = useContext(Ctx)
  if (asChild && React.isValidElement<SlottableChildProps>(children)) {
    return React.cloneElement(children as React.ReactElement<SlottableChildProps>, { ...props, ref, onClick: (e: React.MouseEvent<HTMLElement>) => { children.props?.onClick?.(e); setOpen(!open) } })
  }
  return <button {...props} ref={ref} onClick={() => setOpen(!open)}>{children}</button>
})

export const Anchor = React.forwardRef<HTMLDivElement, PopoverAnchorProps>(({ children, ...props }, ref) => <div ref={ref} {...props}>{children}</div>)

export const Portal = ({ children }: PopoverPortalProps) => <>{children}</>

export const Content = React.forwardRef<HTMLDivElement, PopoverContentProps>(({ children, ...props }, ref) => {
  const { open } = useContext(Ctx)
  if (!open) return null
  return <div ref={ref} {...props}>{children}</div>
})

export const Close = React.forwardRef<HTMLButtonElement, PopoverCloseProps>(({ children, asChild, ...props }, ref) => {
  const { setOpen } = useContext(Ctx)
  if (asChild && React.isValidElement<SlottableChildProps>(children)) {
    return React.cloneElement(children as React.ReactElement<SlottableChildProps>, { ...props, ref, onClick: (e: React.MouseEvent<HTMLElement>) => { children.props?.onClick?.(e); setOpen(false) } })
  }
  return <button {...props} ref={ref} onClick={() => setOpen(false)}>{children}</button>
})

export const Arrow = React.forwardRef<HTMLDivElement, ArrowProps>((props, ref) => <div ref={ref} {...props} />)
