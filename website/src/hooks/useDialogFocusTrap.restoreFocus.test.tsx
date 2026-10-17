/**
 * `useDialogFocusTrap`'s focus-return half, and the `restoreFocus` opt-out a
 * trigger-anchored popover needs (#5542).
 *
 * The hook's default is right for the Modal family: capture
 * `document.activeElement` on mount, put focus back there on unmount. It is
 * wrong for a popover anchored to a trigger whose host restores focus itself,
 * and it fails in a way no consumer test would notice — the restore runs on
 * UNMOUNT, which orders AFTER the host's own `trigger.focus()`, so it silently
 * undoes it. The capture is what makes that reachable: on Safari a clicked
 * button is not focused, so a popover opened by a click captures `<body>` and
 * "restores" focus to nothing.
 *
 * The third test below is the pin for that: it reproduces the Safari shape (no
 * element focused when the dialog mounts) and asserts the host's restore
 * survives. Flip the option back to the default and it goes red.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { useRef, useState } from 'react'
import { render, cleanup, fireEvent } from '@testing-library/react'
import { useDialogFocusTrap } from './useDialogFocusTrap'

function Dialog({ restoreFocus }: { restoreFocus?: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  useDialogFocusTrap(ref, () => {}, { restoreFocus })
  return (
    <div ref={ref} role="dialog">
      <button data-testid="inside">inside</button>
    </div>
  )
}

/**
 * A trigger-anchored popover: the dialog mounts on open (which is what makes
 * the hook's mount-keyed focus effects usable), and the host — not the hook —
 * returns focus to the trigger when it closes.
 */
function Host({ restoreFocus, focusTriggerOnClose = true }: { restoreFocus?: boolean; focusTriggerOnClose?: boolean }) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  return (
    <>
      <button
        ref={triggerRef}
        data-testid="trigger"
        onClick={() => {
          setOpen((o) => !o)
          if (open && focusTriggerOnClose) triggerRef.current?.focus()
        }}
      >
        open
      </button>
      {open ? <Dialog restoreFocus={restoreFocus} /> : null}
    </>
  )
}

afterEach(cleanup)

describe('useDialogFocusTrap focus restoration', () => {
  it('moves focus into the dialog on mount and back to the opener on unmount (default)', () => {
    const utils = render(<Host />)
    const trigger = utils.getByTestId('trigger')
    // A real click focuses the button; the test DOM does not, so focus it the
    // way Chrome/Firefox would before opening.
    trigger.focus()
    fireEvent.click(trigger)
    expect(utils.getByTestId('inside')).toHaveFocus()

    // Close WITHOUT the host restoring focus: the hook's own restore is the
    // only thing that can put focus back, and it does.
    fireEvent.click(trigger)
    expect(trigger).toHaveFocus()
  })

  it('still moves focus INTO the dialog when the return half is off', () => {
    // `restoreFocus` gates only the return: a dialog that opts out must not
    // lose focus entry, or a keyboard user is left outside an aria-modal
    // surface.
    const utils = render(<Host restoreFocus={false} focusTriggerOnClose={false} />)
    const trigger = utils.getByTestId('trigger')
    trigger.focus()
    fireEvent.click(trigger)
    expect(utils.getByTestId('inside')).toHaveFocus()
  })

  it('does not undo the host\u2019s own focus restore when the return half is off', () => {
    // The Safari shape: the trigger was clicked but is NOT the activeElement,
    // so the hook's capture would be <body>. The host restores focus to the
    // trigger as it closes, and the unmount-ordered restore must not blur it.
    const utils = render(<Host restoreFocus={false} />)
    const trigger = utils.getByTestId('trigger')
    expect(document.activeElement).not.toBe(trigger)
    fireEvent.click(trigger)
    expect(utils.getByTestId('inside')).toHaveFocus()

    fireEvent.click(trigger)
    expect(trigger).toHaveFocus()
  })
})
