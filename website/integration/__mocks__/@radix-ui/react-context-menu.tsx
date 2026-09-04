/**
 * Test mock for @radix-ui/react-context-menu.
 *
 * Stateful: Content shows after onContextMenu fires on the Trigger child.
 * Items respond to fireEvent.click directly.
 *
 * Focus-restore fidelity: real Radix restores focus to the trigger on close
 * (FocusScope cleanup dispatches AUTOFOCUS_ON_UNMOUNT → onCloseAutoFocus, then,
 * unless defaultPrevented, focuses the previously-focused element). That
 * restore is what steals focus back from a freshly-mounted rename input and
 * cancels the edit. The mock reproduces it: on close it calls the Content's
 * onCloseAutoFocus with a preventable event, and unless prevented, focuses the
 * trigger.
 *
 * Ordering note: real browsers fire the restore AFTER a consumer's mount-time
 * requestAnimationFrame focus (that's the race the rename guard defends
 * against). jsdom fires setTimeout(0) BEFORE rAF — backwards — so a
 * setTimeout-based restore would run before the input is focused and never
 * blur it, letting a broken rename path pass. We therefore schedule the
 * restore on a double rAF so it deterministically lands one frame AFTER the
 * consumer's single-rAF focus, reproducing the real-browser worst case.
 * Without this the rename-focus regression passed its test.
 */
import React, { useState, useContext, useRef, createContext } from 'react'

interface CtxValue {
  open: boolean
  setOpen: (v: boolean) => void
  triggerRef: React.MutableRefObject<HTMLElement | null>
  closeAutoFocusRef: React.MutableRefObject<((e: Event) => void) | undefined>
}
const Ctx = createContext<CtxValue>({
  open: false, setOpen: () => {},
  triggerRef: { current: null }, closeAutoFocusRef: { current: undefined },
})

export const Root: React.FC<any> = ({ children }) => {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLElement | null>(null)
  const closeAutoFocusRef = useRef<((e: Event) => void) | undefined>(undefined)
  return <Ctx.Provider value={{ open, setOpen, triggerRef, closeAutoFocusRef }}>{children}</Ctx.Provider>
}

export const Trigger = React.forwardRef<HTMLElement, any>(({ children, asChild, ...props }, ref) => {
  const { open, setOpen, triggerRef } = useContext(Ctx)
  const setRefs = (node: HTMLElement | null) => {
    triggerRef.current = node
    if (typeof ref === 'function') ref(node)
    else if (ref) (ref as React.MutableRefObject<HTMLElement | null>).current = node
  }
  const handleContextMenu = (e: any) => { e.preventDefault?.(); setOpen(true); props.onContextMenu?.(e) }
  if (asChild && React.isValidElement(children)) {
    return React.cloneElement(children as React.ReactElement<any>, { ...props, ref: setRefs, onContextMenu: handleContextMenu, 'data-state': open ? 'open' : 'closed' })
  }
  return <span ref={setRefs} {...props} onContextMenu={handleContextMenu} data-state={open ? 'open' : 'closed'}>{children}</span>
})

export const Portal: React.FC<any> = ({ children }) => <>{children}</>
export const Content = React.forwardRef<HTMLDivElement, any>(({ children, className, onCloseAutoFocus, ...props }, ref) => {
  const { open, closeAutoFocusRef } = useContext(Ctx)
  // Publish this Content's onCloseAutoFocus so the closing Item can invoke it,
  // matching how real Radix wires the handler onto its FocusScope.
  closeAutoFocusRef.current = onCloseAutoFocus
  if (!open) return null
  return <div ref={ref} role="menu" className={className} {...props}>{children}</div>
})
// Item close-and-restore: mirror Radix's onSelect → close → focus-restore.
// The restore runs on a double rAF so it deterministically lands one frame
// after a consumer's single-rAF mount focus (see the ordering note above), and
// is skipped when the Content's onCloseAutoFocus calls preventDefault.
export const Item = React.forwardRef<HTMLDivElement, any>(({ children, className, onSelect, ...props }, ref) => {
  const { setOpen, triggerRef, closeAutoFocusRef } = useContext(Ctx)
  return (
    <div ref={ref} role="menuitem" className={className} {...props}
      onClick={e => {
        props.onClick?.(e)
        onSelect?.(e)
        setOpen(false)
        const trigger = triggerRef.current
        const handler = closeAutoFocusRef.current
        requestAnimationFrame(() => requestAnimationFrame(() => {
          const evt = new CustomEvent('closeAutoFocus', { cancelable: true })
          handler?.(evt)
          if (evt.defaultPrevented) return
          // Real Radix focuses the trigger (previously-focused element) here,
          // which blurs whatever the consumer just focused. jsdom won't focus a
          // plain <div> trigger, so model the essential browser effect directly:
          // move focus off the active element (fires its blur), then focus the
          // trigger. preventDefault above (the rename guard) skips both.
          ;(document.activeElement as HTMLElement | null)?.blur()
          trigger?.focus()
        }))
      }}>
      {children}
    </div>
  )
})
export const Separator = React.forwardRef<HTMLDivElement, any>((props, ref) => <div ref={ref} role="separator" {...props} />)
export const Group: React.FC<any> = ({ children }) => <>{children}</>

// Submenu: stateful like Root — SubTrigger opens it, SubContent renders when open.
const SubCtx = createContext<{ open: boolean; setOpen: (v: boolean) => void }>({ open: false, setOpen: () => {} })
export const Sub: React.FC<any> = ({ children, onOpenChange }) => {
  const [open, setOpen] = useState(false)
  const set = (v: boolean) => { setOpen(v); onOpenChange?.(v) }
  return <SubCtx.Provider value={{ open, setOpen: set }}>{children}</SubCtx.Provider>
}
export const SubTrigger = React.forwardRef<HTMLDivElement, any>(({ children, className, ...props }, ref) => {
  const { open, setOpen } = useContext(SubCtx)
  return (
    <div ref={ref} role="menuitem" aria-haspopup="menu" aria-expanded={open} className={className} {...props}
      onClick={e => { props.onClick?.(e); setOpen(!open) }}>
      {children}
    </div>
  )
})
export const SubContent = React.forwardRef<HTMLDivElement, any>(({ children, className, ...props }, ref) => {
  const { open } = useContext(SubCtx)
  if (!open) return null
  return <div ref={ref} role="menu" className={className} {...props}>{children}</div>
})
export const RadioGroup: React.FC<any> = ({ children }) => <>{children}</>
