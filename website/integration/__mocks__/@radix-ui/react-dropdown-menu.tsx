/**
 * Test mock for @radix-ui/react-dropdown-menu.
 *
 * Stateful: Content is hidden until Trigger is clicked, then items
 * respond to fireEvent.click directly (no pointer-event gating).
 *
 * Focus-restore fidelity (mirrors the context-menu mock): real Radix restores
 * focus to the trigger on close via onCloseAutoFocus, then, unless
 * defaultPrevented, focuses the previously-focused element. That restore is
 * what steals focus from a freshly-mounted rename input and cancels the edit.
 * The mock reproduces it: on close it calls the Content's onCloseAutoFocus with
 * a preventable event and, unless prevented, blurs the active element + focuses
 * the trigger — on a double rAF so it lands one frame AFTER a consumer's
 * single-rAF mount focus (real-browser order; jsdom fires setTimeout(0) before
 * rAF, so setTimeout would be wrong). Without this, a broken folder-rename path
 * passes its test.
 *
 * onSelect preventDefault fidelity: real Radix keeps the menu open when an
 * Item's onSelect calls event.preventDefault(); the mock honors that too.
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

export const Root: React.FC<any> = ({ children, open: controlledOpen, onOpenChange }) => {
  const [internal, setInternal] = useState(false)
  const open = controlledOpen ?? internal
  const triggerRef = useRef<HTMLElement | null>(null)
  const closeAutoFocusRef = useRef<((e: Event) => void) | undefined>(undefined)
  const setOpen = (v: boolean) => { setInternal(v); onOpenChange?.(v) }
  return <Ctx.Provider value={{ open, setOpen, triggerRef, closeAutoFocusRef }}>{children}</Ctx.Provider>
}

export const Trigger = React.forwardRef<HTMLButtonElement, any>(({ children, asChild, ...props }, ref) => {
  const { open, setOpen, triggerRef } = useContext(Ctx)
  const setRefs = (node: HTMLElement | null) => {
    triggerRef.current = node
    if (typeof ref === 'function') ref(node)
    else if (ref) (ref as React.MutableRefObject<HTMLElement | null>).current = node
  }
  const handleClick = (e: any) => { setOpen(!open); props.onClick?.(e) }
  if (asChild && React.isValidElement(children)) {
    return React.cloneElement(children as React.ReactElement<any>, { ...props, ref: setRefs, onClick: handleClick, 'data-state': open ? 'open' : 'closed' })
  }
  return <button ref={setRefs} {...props} onClick={handleClick} data-state={open ? 'open' : 'closed'}>{children}</button>
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
// Item close-and-restore: mirror Radix's onSelect → close → focus-restore. The
// restore runs on a double rAF so it deterministically lands one frame after a
// consumer's single-rAF mount focus, and is skipped when the Content's
// onCloseAutoFocus calls preventDefault.
/**
 * The shared close-and-restore click handler for a menu row. Item and RadioItem
 * differ only in role and checked state, so the focus-restore fidelity that the
 * rename-guard tests depend on lives in one place.
 */
function useRowClick(props: any, onSelect?: (e: any) => void) {
  const { setOpen, triggerRef, closeAutoFocusRef } = useContext(Ctx)
  return (e: any) => {
    props.onClick?.(e)
    onSelect?.(e)
    // Real Radix keeps the menu open when onSelect prevents default
    // (used by items that open an inline sub-panel, e.g. cut mode or a
    // confirm card). Mirror that: only close when not defaultPrevented.
    if (e.defaultPrevented) return
    setOpen(false)
    const trigger = triggerRef.current
    const handler = closeAutoFocusRef.current
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const evt = new CustomEvent('closeAutoFocus', { cancelable: true })
      handler?.(evt)
      if (evt.defaultPrevented) return
      // Real Radix focuses the trigger (previously-focused element) here,
      // which blurs whatever the consumer just focused. jsdom won't focus a
      // plain trigger reliably, so model the essential browser effect: move
      // focus off the active element (fires its blur), then focus the
      // trigger. preventDefault above (the rename guard) skips both.
      ;(document.activeElement as HTMLElement | null)?.blur()
      trigger?.focus()
    }))
  }
}

export const Item = React.forwardRef<HTMLDivElement, any>(({ children, className, onSelect, ...props }, ref) => (
  <div ref={ref} role="menuitem" className={className} {...props} onClick={useRowClick(props, onSelect)}>
    {children}
  </div>
))

export const Separator = React.forwardRef<HTMLDivElement, any>((props, ref) => <div ref={ref} role="separator" {...props} />)
export const Label = React.forwardRef<HTMLDivElement, any>(({ children, ...props }, ref) => <div ref={ref} {...props}>{children}</div>)
export const Group: React.FC<any> = ({ children }) => <>{children}</>
export const Sub: React.FC<any> = ({ children }) => {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLElement | null>(null)
  const closeAutoFocusRef = useRef<((e: Event) => void) | undefined>(undefined)
  return <Ctx.Provider value={{ open, setOpen, triggerRef, closeAutoFocusRef }}>{children}</Ctx.Provider>
}
export const SubTrigger = React.forwardRef<HTMLDivElement, any>(({ children, className, ...props }, ref) => {
  const { setOpen } = useContext(Ctx)
  return <div ref={ref} role="menuitem" className={className} {...props} onMouseEnter={() => setOpen(true)}>{children}</div>
})
export const SubContent = React.forwardRef<HTMLDivElement, any>(({ children, className, onCloseAutoFocus: _onCloseAutoFocus, ...props }, ref) => {
  const { open } = useContext(Ctx)
  if (!open) return null
  return <div ref={ref} role="menu" className={className} {...props}>{children}</div>
})
const RadioCtx = createContext<string | undefined>(undefined)
export const RadioGroup: React.FC<any> = ({ children, value }) => (
  <RadioCtx.Provider value={value}>{children}</RadioCtx.Provider>
)
export const RadioItem = React.forwardRef<HTMLDivElement, any>(({ children, className, onSelect, value, ...props }, ref) => {
  const selected = useContext(RadioCtx)
  return (
    <div
      ref={ref}
      role="menuitemradio"
      aria-checked={selected === value}
      className={className}
      {...props}
      onClick={useRowClick(props, onSelect)}
    >
      {children}
    </div>
  )
})
