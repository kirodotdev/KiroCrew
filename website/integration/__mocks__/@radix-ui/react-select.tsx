/**
 * Test mock for @radix-ui/react-select.
 *
 * Stateful, mirroring the react-dropdown-menu mock: Content is hidden until
 * Trigger is clicked, then items respond to fireEvent.click directly (no
 * pointer-event gating, which jsdom cannot simulate).
 *
 * Value fidelity: real Radix renders the selected Item's ItemText inside
 * SelectValue. The mock reproduces that with a value→label registry that
 * Items populate on render, so tests can assert the trigger shows the
 * selected option's LABEL (not its raw value) — the exact contract
 * SettingsSelect's optionLabels feature depends on.
 */
import React, { useState, useContext, useRef, createContext } from 'react'

interface CtxValue {
  open: boolean
  setOpen: (v: boolean) => void
  value: string | undefined
  select: (v: string) => void
  labels: React.MutableRefObject<Map<string, React.ReactNode>>
  disabled?: boolean
}
const Ctx = createContext<CtxValue>({
  open: false, setOpen: () => {}, value: undefined, select: () => {},
  labels: { current: new Map() },
})

export const Root: React.FC<any> = ({ children, value, defaultValue, onValueChange, open: controlledOpen, onOpenChange, disabled }) => {
  const [internalOpen, setInternalOpen] = useState(false)
  const [internalValue, setInternalValue] = useState<string | undefined>(defaultValue)
  const open = controlledOpen ?? internalOpen
  const currentValue = value !== undefined ? value : internalValue
  const labels = useRef(new Map<string, React.ReactNode>())
  const setOpen = (v: boolean) => { setInternalOpen(v); onOpenChange?.(v) }
  const select = (v: string) => { setInternalValue(v); onValueChange?.(v); setOpen(false) }
  return <Ctx.Provider value={{ open, setOpen, value: currentValue, select, labels, disabled }}>{children}</Ctx.Provider>
}

export const Trigger = React.forwardRef<HTMLButtonElement, any>(({ children, asChild, ...props }, ref) => {
  const { open, setOpen, disabled } = useContext(Ctx)
  const handleClick = (e: any) => { if (disabled) return; setOpen(!open); props.onClick?.(e) }
  if (asChild && React.isValidElement(children)) {
    return React.cloneElement(children as React.ReactElement<any>, { ...props, ref, onClick: handleClick, 'data-state': open ? 'open' : 'closed' })
  }
  return (
    <button ref={ref} type="button" role="combobox" aria-expanded={open} {...(disabled ? { 'data-disabled': '' } : {})} {...props} onClick={handleClick} data-state={open ? 'open' : 'closed'}>
      {children}
    </button>
  )
})

export const Value: React.FC<any> = ({ placeholder }) => {
  const { value, labels } = useContext(Ctx)
  const label = value !== undefined ? labels.current.get(value) : undefined
  return <span>{label ?? placeholder ?? ''}</span>
}

export const Icon: React.FC<any> = ({ children, asChild }) => {
  if (asChild && React.isValidElement(children)) return children
  return <span aria-hidden>{children}</span>
}

export const Portal: React.FC<any> = ({ children }) => <>{children}</>

export const Content = React.forwardRef<HTMLDivElement, any>(({ children, className, position: _position, sideOffset: _sideOffset, ...props }, ref) => {
  const { open } = useContext(Ctx)
  if (!open) return null
  return <div ref={ref} role="listbox" className={className} {...props}>{children}</div>
})

export const Viewport: React.FC<any> = ({ children, className }) => <div className={className}>{children}</div>

const ItemCtx = createContext<{ selected: boolean }>({ selected: false })

export const Item = React.forwardRef<HTMLDivElement, any>(({ children, className, value: itemValue, ...props }, ref) => {
  const { value, select, labels } = useContext(Ctx)
  const selected = value === itemValue
  // Register this item's label so Value can render the selected item's text
  // even before the content has ever been opened (registry fills on render;
  // Content renders only when open, so also register from a layout effect of
  // a hidden probe — simplest reliable approach: register during render).
  labels.current.set(itemValue, extractText(children))
  return (
    <ItemCtx.Provider value={{ selected }}>
      <div
        ref={ref}
        role="option"
        aria-selected={selected}
        data-state={selected ? 'checked' : 'unchecked'}
        className={className}
        {...props}
        onClick={e => { props.onClick?.(e); select(itemValue) }}
      >
        {children}
      </div>
    </ItemCtx.Provider>
  )
})

/** Real Radix renders ItemText's children; keep plain. */
export const ItemText: React.FC<any> = ({ children }) => <span>{children}</span>

/** Real Radix renders the indicator only for the selected item. */
export const ItemIndicator: React.FC<any> = ({ children, className }) => {
  const { selected } = useContext(ItemCtx)
  if (!selected) return null
  return <span className={className}>{children}</span>
}

export const ScrollUpButton: React.FC<any> = () => null
export const ScrollDownButton: React.FC<any> = () => null
export const Group: React.FC<any> = ({ children }) => <>{children}</>
export const Label = React.forwardRef<HTMLDivElement, any>(({ children, ...props }, ref) => <div ref={ref} {...props}>{children}</div>)
export const Separator = React.forwardRef<HTMLDivElement, any>((props, ref) => <div ref={ref} role="separator" {...props} />)

/** Flatten a ReactNode to its text content for the Value registry. */
function extractText(node: React.ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(extractText).join('')
  if (React.isValidElement(node)) return extractText((node.props as { children?: React.ReactNode }).children)
  return ''
}
