import * as React from 'react'
import * as SelectPrimitive from '@radix-ui/react-select'
import { Check, ChevronDown, ChevronUp } from 'lucide-react'
import { cn } from '../../lib/utils'

/**
 * shadcn-style wrapper around Radix Select, themed to match ui/dropdown-menu.tsx.
 *
 * Used by SettingsSelect (components/settings.tsx) — the choke point for every
 * dropdown on the Settings pages. Selected items use the accent treatment
 * (accent-subtle wash + accent text), with the check indicator on the right to
 * match.
 */

const Select = SelectPrimitive.Root
const SelectGroup = SelectPrimitive.Group
const SelectValue = SelectPrimitive.Value

const SelectTrigger = React.forwardRef<
  React.ComponentRef<typeof SelectPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Trigger>
>(({ className, children, ...props }, ref) => (
  <SelectPrimitive.Trigger
    ref={ref}
    className={cn(
      'flex items-center justify-between w-full px-3 py-2 rounded-md text-sm border border-border bg-bg-elevated text-text',
      'hover:border-border-strong transition-all cursor-pointer outline-none',
      'focus-visible:border-accent data-[disabled]:opacity-40 data-[disabled]:pointer-events-none',
      '[&>span]:truncate [&>span]:text-left [&>span]:min-w-0',
      className
    )}
    {...props}
  >
    {children}
    <SelectPrimitive.Icon asChild>
      <ChevronDown className="ml-2 shrink-0 text-muted" size={14} aria-hidden />
    </SelectPrimitive.Icon>
  </SelectPrimitive.Trigger>
))
SelectTrigger.displayName = SelectPrimitive.Trigger.displayName

const SelectScrollUpButton = React.forwardRef<
  React.ComponentRef<typeof SelectPrimitive.ScrollUpButton>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.ScrollUpButton>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.ScrollUpButton
    ref={ref}
    className={cn('flex cursor-default items-center justify-center py-1 text-muted', className)}
    {...props}
  >
    <ChevronUp size={14} aria-hidden />
  </SelectPrimitive.ScrollUpButton>
))
SelectScrollUpButton.displayName = SelectPrimitive.ScrollUpButton.displayName

const SelectScrollDownButton = React.forwardRef<
  React.ComponentRef<typeof SelectPrimitive.ScrollDownButton>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.ScrollDownButton>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.ScrollDownButton
    ref={ref}
    className={cn('flex cursor-default items-center justify-center py-1 text-muted', className)}
    {...props}
  >
    <ChevronDown size={14} aria-hidden />
  </SelectPrimitive.ScrollDownButton>
))
SelectScrollDownButton.displayName = SelectPrimitive.ScrollDownButton.displayName

const SelectContent = React.forwardRef<
  React.ComponentRef<typeof SelectPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Content>
>(({ className, children, position = 'popper', onEscapeKeyDown, ...props }, ref) => (
  <SelectPrimitive.Portal>
    <SelectPrimitive.Content
      ref={ref}
      position={position}
      sideOffset={4}
      // Escape must dismiss ONLY the select, not the surface hosting it. Radix
      // dismisses from a document-level listener, so without this the same
      // keydown keeps bubbling to window-level Escape handlers (e.g. the
      // workspace modal in KiroCrewAgentsPage) and closes them too. We stop
      // propagation but never preventDefault, so Radix still closes the select.
      onEscapeKeyDown={e => {
        e.stopPropagation()
        onEscapeKeyDown?.(e)
      }}
      className={cn(
        'z-[9999] max-h-[240px] overflow-hidden rounded-lg border border-border bg-bg-elevated p-1 text-text shadow-lg',
        'data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95',
        'data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2',
        // Popper mode: the panel is EXACTLY the trigger's width. No `min-w`
        // here on purpose — a floor wider than the trigger makes the popup
        // visibly overhang it, which is the inconsistency this is avoiding. A
        // caller whose rows need more room widens the TRIGGER instead, so the
        // two stay in lockstep.
        position === 'popper' && 'w-[var(--radix-select-trigger-width)]',
        className
      )}
      {...props}
    >
      <SelectScrollUpButton />
      <SelectPrimitive.Viewport className="p-0">
        {children}
      </SelectPrimitive.Viewport>
      <SelectScrollDownButton />
    </SelectPrimitive.Content>
  </SelectPrimitive.Portal>
))
SelectContent.displayName = SelectPrimitive.Content.displayName

const SelectItem = React.forwardRef<
  React.ComponentRef<typeof SelectPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Item>
>(({ className, children, ...props }, ref) => (
  <SelectPrimitive.Item
    ref={ref}
    className={cn(
      'relative flex cursor-pointer select-none items-center justify-between gap-2 rounded-md px-3 py-1.5 text-[13px] outline-none transition-colors',
      'focus:bg-bg-hover data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
      // Themed selected state (user preference): accent wash + accent text
      // instead of stock shadcn's check-only look.
      'data-[state=checked]:bg-accent-subtle data-[state=checked]:text-accent data-[state=checked]:font-semibold',
      className
    )}
    {...props}
  >
    <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
    <SelectPrimitive.ItemIndicator className="shrink-0 text-accent">
      <Check size={13} aria-hidden />
    </SelectPrimitive.ItemIndicator>
  </SelectPrimitive.Item>
))
SelectItem.displayName = SelectPrimitive.Item.displayName

const SelectLabel = React.forwardRef<
  React.ComponentRef<typeof SelectPrimitive.Label>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Label>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.Label
    ref={ref}
    className={cn('px-3 py-1.5 text-[11px] font-semibold text-muted', className)}
    {...props}
  />
))
SelectLabel.displayName = SelectPrimitive.Label.displayName

const SelectSeparator = React.forwardRef<
  React.ComponentRef<typeof SelectPrimitive.Separator>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Separator>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.Separator
    ref={ref}
    className={cn('mx-1 my-1 h-px bg-border', className)}
    {...props}
  />
))
SelectSeparator.displayName = SelectPrimitive.Separator.displayName

export {
  Select,
  SelectGroup,
  SelectValue,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectScrollUpButton,
  SelectScrollDownButton,
}
