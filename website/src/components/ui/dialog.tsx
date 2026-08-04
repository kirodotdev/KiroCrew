import * as React from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { cn } from '../../lib/utils'
import { i18nT } from '../../i18n/t'

/**
 * shadcn/ui `dialog` over Radix Dialog, themed to this repo's tokens the way
 * ui/select.tsx and ui/dropdown-menu.tsx are rather than shadcn's stock zinc.
 *
 * Adopting the primitive means DELETING hand-rolled behaviour, not wrapping it:
 * Radix owns the focus trap, focus restore, Tab cycling, Escape, scroll lock,
 * `aria-modal`, and the nested-dialog focus stack. `components/Modal.tsx` still
 * hand-rolls its own and is left untouched for its 12 other callers.
 *
 * Two things a caller must know:
 *
 *   - Radix REQUIRES a `DialogTitle` inside `DialogContent` for the accessible
 *     name, and warns when `aria-describedby` points at nothing. `DialogContent`
 *     therefore defaults `aria-describedby` to `undefined`; pass a
 *     `DialogDescription` and wire it explicitly if a description is wanted.
 *   - Animations use this repo's own `scale-in`/`scale-out` + `fade-in`/
 *     `fade-out` keyframes, NOT shadcn's stock `animate-in`/`zoom-in-95`. Those
 *     stock class names come from `tailwindcss-animate`, which is not installed
 *     here — they are inert in ui/select.tsx today, and making them real would
 *     silently start animating every dropdown, popover and context menu in the
 *     app.
 */

const Dialog = DialogPrimitive.Root
const DialogTrigger = DialogPrimitive.Trigger
const DialogPortal = DialogPrimitive.Portal
const DialogClose = DialogPrimitive.Close

const DialogOverlay = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      'fixed inset-0 z-[100] bg-bg/60 backdrop-blur-md',
      'data-[state=open]:animate-fade-in data-[state=closed]:animate-fade-out',
      className,
    )}
    {...props}
  />
))
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName

interface DialogContentProps
  extends React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> {
  /** Max width in px (default 640, matching the old centered Modal). */
  maxWidth?: number
  /** Hide the built-in close button when the caller renders its own. */
  hideClose?: boolean
}

const DialogContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  DialogContentProps
>(({ className, children, maxWidth = 640, hideClose = false, style, onKeyDown, ...props }, ref) => (
  <DialogPortal>
    <DialogOverlay />
    <DialogPrimitive.Content
      ref={ref}
      // Radix warns when this points at an id that does not exist. Callers that
      // want a description render a DialogDescription and set it themselves.
      aria-describedby={undefined}
      // The page's own global shortcuts must not fire while the user is typing
      // in here. `useKeyboardShortcuts` binds `document.addEventListener
      // ('keydown', ...)` in the BUBBLE phase, so without this a Cmd+, inside a
      // half-filled form reaches it, navigates to Settings, and unmounts the
      // dialog along with the unsaved input. `SideSheet` has carried the same
      // guard; a centered dialog needs it just as much.
      //
      // It does NOT cost us Escape: Radix's DismissableLayer listens with
      // `{ capture: true }`, and a capture-phase listener runs BEFORE the event
      // reaches this element, so stopping bubble propagation here cannot hide it.
      // (Asserted end-to-end in scripts/verify-crews-dialog-select.mjs.)
      onKeyDown={e => {
        onKeyDown?.(e)
        e.stopPropagation()
      }}
      style={{ maxWidth, maxHeight: '90vh', ...style }}
      className={cn(
        'fixed left-1/2 top-1/2 z-[101] flex w-[calc(100%-4rem)] -translate-x-1/2 -translate-y-1/2',
        'flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl outline-none',
        'data-[state=open]:animate-scale-in data-[state=closed]:animate-scale-out',
        className,
      )}
      {...props}
      // AFTER the spread, and conditional: `aria-labelledby` OUTRANKS
      // `aria-label` in the accname spec, and Radix always points it at the
      // DialogTitle. A caller passing `aria-label` for a fuller name ("Edit crew
      // oncall", over a title that only shows "oncall") would otherwise be
      // silently ignored. When no `aria-label` is given, leave Radix's wiring
      // alone so the visible Title still names the dialog.
      {...(props['aria-label'] ? { 'aria-labelledby': undefined } : {})}
    >
      {children}
      {!hideClose && (
        <DialogPrimitive.Close
          aria-label={i18nT('components.dialog.close')}
          className="absolute right-4 top-3 rounded-md p-1.5 text-muted transition-colors hover:bg-bg-hover hover:text-text focus-ring"
        >
          <X size={16} />
        </DialogPrimitive.Close>
      )}
    </DialogPrimitive.Content>
  </DialogPortal>
))
DialogContent.displayName = DialogPrimitive.Content.displayName

/** Fixed-height header strip. `pr-12` keeps content clear of the close button. */
const DialogHeader = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(function DialogHeader({ className, ...props }, ref) {
  return (
    <div
      ref={ref}
      className={cn(
        'flex shrink-0 items-center gap-3 border-b border-border px-5 py-3 pr-12',
        className,
      )}
      {...props}
    />
  )
})

/** Scrollable body. The dialog is capped at 90vh, so this is what gives. */
const DialogBody = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(function DialogBody({ className, ...props }, ref) {
  return (
    <div
      ref={ref}
      className={cn('min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-5 py-4', className)}
      {...props}
    />
  )
})

const DialogFooter = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(function DialogFooter({ className, ...props }, ref) {
  return (
    <div
      ref={ref}
      className={cn(
        'flex shrink-0 items-center justify-end gap-2 border-t border-border bg-bg-accent px-5 py-3',
        className,
      )}
      {...props}
    />
  )
})

const DialogTitle = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn('min-w-0 truncate text-[15px] font-semibold text-text-strong', className)}
    {...props}
  />
))
DialogTitle.displayName = DialogPrimitive.Title.displayName

const DialogDescription = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn('text-[12px] leading-relaxed text-muted', className)}
    {...props}
  />
))
DialogDescription.displayName = DialogPrimitive.Description.displayName

export {
  Dialog,
  DialogTrigger,
  DialogPortal,
  DialogClose,
  DialogOverlay,
  DialogContent,
  DialogHeader,
  DialogBody,
  DialogFooter,
  DialogTitle,
  DialogDescription,
}
