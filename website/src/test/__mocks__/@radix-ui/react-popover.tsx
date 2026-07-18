import React, { useState, createContext, useContext } from 'react'

const Ctx = createContext<{ open: boolean; setOpen: (v: boolean) => void }>({ open: false, setOpen: () => {} })

export const Root = ({ children, open: controlledOpen, onOpenChange }: any) => {
  const [internalOpen, setInternalOpen] = useState(false)
  const open = controlledOpen ?? internalOpen
  const setOpen = (v: boolean) => { setInternalOpen(v); onOpenChange?.(v) }
  return <Ctx.Provider value={{ open, setOpen }}>{children}</Ctx.Provider>
}

export const Trigger = React.forwardRef<any, any>(({ children, asChild, ...props }, ref) => {
  const { open, setOpen } = useContext(Ctx)
  if (asChild && React.isValidElement(children)) {
    return React.cloneElement(children as any, { ...props, ref, onClick: (e: any) => { children.props?.onClick?.(e); setOpen(!open) } })
  }
  return <button {...props} ref={ref} onClick={() => setOpen(!open)}>{children}</button>
})

export const Anchor = React.forwardRef<any, any>(({ children, ...props }, ref) => <div ref={ref} {...props}>{children}</div>)

export const Portal = ({ children }: any) => <>{children}</>

export const Content = React.forwardRef<any, any>(({ children, ...props }, ref) => {
  const { open } = useContext(Ctx)
  if (!open) return null
  return <div ref={ref} {...props}>{children}</div>
})

export const Close = React.forwardRef<any, any>(({ children, asChild, ...props }, ref) => {
  const { setOpen } = useContext(Ctx)
  if (asChild && React.isValidElement(children)) {
    return React.cloneElement(children as any, { ...props, ref, onClick: (e: any) => { children.props?.onClick?.(e); setOpen(false) } })
  }
  return <button {...props} ref={ref} onClick={() => setOpen(false)}>{children}</button>
})

export const Arrow = React.forwardRef<any, any>((props, ref) => <div ref={ref} {...props} />)
