/**
 * `useDialogFocusTrap`'s keyboard half is armed by the time its dialog is in the
 * DOM. The listener used to attach in a passive effect, which React runs a task
 * after the commit when the open came from a default-priority update (a fetch
 * answering) and the render ran past the scheduler's 5 ms slice. A key pressed
 * in that gap reached a dialog with no Escape listener and was dropped.
 */
import { describe, it, expect, vi } from 'vitest'
import { useRef, useState } from 'react'
import { render, fireEvent } from '@testing-library/react'
import { useDialogFocusTrap } from './useDialogFocusTrap'

/** Outlasts the scheduler slice, so React yields before passive effects. */
function SlowBody() {
  const end = performance.now() + 20
  while (performance.now() < end) { /* outlast the scheduler slice */ }
  return null
}

function Dialog({ onEscape }: { onEscape: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  useDialogFocusTrap(ref, onEscape)
  return <div ref={ref} role="dialog"><SlowBody /></div>
}

describe('useDialogFocusTrap — arming', () => {
  it('an Escape pressed the moment the dialog appears reaches onEscape', async () => {
    const onEscape = vi.fn()
    let show: () => void = () => {}
    function Host() {
      const [open, setOpen] = useState(false)
      show = () => setOpen(true)
      return open ? <Dialog onEscape={onEscape} /> : null
    }
    render(<Host />)

    const g = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
    const prior = g.IS_REACT_ACT_ENVIRONMENT
    // Let React schedule the open as it does in the app, not flush it in act().
    g.IS_REACT_ACT_ENVIRONMENT = false
    try {
      // The DOM mutation that inserts the dialog is the first moment anything
      // can see it, and the moment findByRole resolves on.
      const pressed = new Promise<void>(resolve => {
        const watch = new MutationObserver(() => {
          const dialog = document.querySelector('[role="dialog"]')
          if (!dialog) return
          watch.disconnect()
          fireEvent.keyDown(dialog, { key: 'Escape' })
          resolve()
        })
        watch.observe(document.body, { childList: true, subtree: true })
      })
      show()
      await pressed
    } finally {
      g.IS_REACT_ACT_ENVIRONMENT = prior
    }
    expect(onEscape).toHaveBeenCalledTimes(1)
  })
})
