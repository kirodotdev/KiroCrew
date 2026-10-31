/**
 * The App store's full-viewport overlay dialogs (the two uninstall confirms and
 * the screenshot viewer), wired to the shared modal keyboard contract.
 *
 * Each of those overlays used to focus itself on open and stop there, so Tab
 * walked out of an `aria-modal` dialog into the page hidden behind it, and
 * closing left focus on `<body>` instead of the control that opened it.
 * `useDialogFocusTrap` owns all three pieces (focus in, Tab ring, focus back)
 * plus Escape, but its focus effects key on MOUNT, so it has to live in a
 * component that mounts with the overlay. This is that component: it renders
 * the overlay root exactly as the callers did (their own className, label and
 * key handlers pass straight through), so nothing about how it looks changes.
 */
import { useEffect, useRef, type HTMLAttributes, type ReactNode, type RefObject } from 'react'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'

interface AppStoreDialogProps extends Omit<HTMLAttributes<HTMLDivElement>, 'role' | 'onClick' | 'tabIndex'> {
  /** Escape and (unless `onBackdropClick` says otherwise) a backdrop click. */
  onClose: () => void
  /** A backdrop click, when it must do more than close (the screenshot viewer
   *  ignores the click a pinch gesture leaves behind). Defaults to `onClose`. */
  onBackdropClick?: () => void
  /** The overlay root, for a caller that needs it too (the viewer's pinch-zoom
   *  bounds). Without one the component keeps its own. */
  dialogRef?: RefObject<HTMLDivElement>
  /** Where focus goes on close, for a dialog opened from a Radix menu item:
   *  the item is gone by the time the dialog mounts (and the menu's own
   *  trigger restore is suppressed, see useMenuOpensDialog), so the element
   *  focused at mount is `<body>`. When it holds nothing still on the page,
   *  the element focused at mount is used, as without it. */
  returnFocusTo?: RefObject<HTMLElement | null>
  children: ReactNode
}

export default function AppStoreDialog({ onClose, onBackdropClick, dialogRef, returnFocusTo, children, ...rest }: AppStoreDialogProps) {
  const ownRef = useRef<HTMLDivElement>(null)
  const ref = dialogRef ?? ownRef
  useDialogFocusTrap(ref, onClose, { restoreFocus: !returnFocusTo })
  // The restore half for `returnFocusTo`. The first render reads what was
  // focused before the dialog mounted; the trap only moves focus in later,
  // from its effect.
  const atMountRef = useRef<HTMLElement | null>(
    typeof document === 'undefined' ? null : (document.activeElement as HTMLElement | null),
  )
  useEffect(() => {
    if (!returnFocusTo) return
    // Both are read once, when the dialog opens: the opener is set before the
    // dialog mounts and nothing changes it while the dialog is up.
    const opener = returnFocusTo.current
    const fallback = atMountRef.current
    return () => {
      const target = opener?.isConnected ? opener : fallback
      target?.focus?.({ preventScroll: true })
    }
  }, [returnFocusTo])
  return (
    // Backdrop click-to-dismiss is a mouse affordance; keyboard users close
    // with Escape, which useDialogFocusTrap owns.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/click-events-have-key-events
    <div
      {...rest}
      ref={ref}
      role="dialog"
      aria-modal="true"
      tabIndex={-1}
      onClick={onBackdropClick ?? onClose}
    >
      {children}
    </div>
  )
}
