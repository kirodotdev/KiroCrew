import { memo, useEffect, useRef, type ReactNode } from 'react'

/**
 * Smoothly animates its own height as the child content grows. Wraps streaming
 * code/diff blocks so new lines extend the block with an eased height
 * transition instead of an instant per-line snap.
 *
 * While `enabled` (the block is still streaming), the outer element is clipped
 * (overflow:hidden) and its height is driven to the measured content height via
 * a ResizeObserver, with a CSS height transition doing the easing. The inner
 * content lays out at full height immediately, so it is revealed from the
 * bottom as the wrapper catches up. When not enabled (block complete, or a
 * smooth-mode message reloaded from history) the wrapper is inert — height:auto,
 * overflow:visible, no transition — so completed blocks keep native sizing and
 * their own horizontal scroll / interactive chrome stay unclipped.
 *
 * The wrapper carries the `ft-resize` class; index.css relocates the block's
 * own 8px margin onto it (and zeroes the child's) so overflow:hidden during the
 * animation doesn't swallow inter-block spacing. borderRadius:inherit +
 * minWidth:100% on both layers avoid the rounded-box clip artifact a plain
 * overflow:hidden wrapper introduces on the block's rounded container.
 *
 * The parent should only mount SmoothResize in smooth mode (a per-message
 * constant), so a block never gains/loses the wrapper mid-stream — the child
 * therefore never remounts when streaming flips to complete.
 */
export const SmoothResize = memo(function SmoothResize(
  { enabled, children }: { enabled: boolean; children: ReactNode },
) {
  const outer = useRef<HTMLDivElement>(null)
  const inner = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const o = outer.current, i = inner.current
    if (!o || !i) return
    if (!enabled) {
      // Inert: let the block size itself natively.
      o.style.height = 'auto'
      return
    }
    // Drive the clipped wrapper to the content height; the CSS transition eases
    // each growth. offsetHeight (layout box) is stable against sub-pixel jitter.
    const apply = () => { o.style.height = i.offsetHeight + 'px' }
    apply()
    const ro = new ResizeObserver(apply)
    ro.observe(i)
    return () => ro.disconnect()
  }, [enabled])

  return (
    <div
      ref={outer}
      className="ft-resize"
      style={{
        overflow: enabled ? 'hidden' : 'visible',
        transition: enabled ? 'height .32s cubic-bezier(.4,0,.2,1)' : 'none',
        borderRadius: 'inherit',
        minWidth: '100%',
      }}
    >
      <div ref={inner} style={{ minWidth: '100%' }}>{children}</div>
    </div>
  )
})
