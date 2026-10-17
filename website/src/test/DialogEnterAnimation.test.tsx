/**
 * ui/dialog — the enter animation lives on `DialogContent` ITSELF.
 *
 * SketchDialog's placement gate holds the Excalidraw pad back until the dialog
 * has stopped moving, and the signal it keys on is `getAnimations()` called on
 * the element `DialogContent` forwards its ref to. `getAnimations()` is
 * element-scoped: if the enter animation were moved to a wrapper (or dropped),
 * that call would return `[]`, the gate would wave the pad through mid-flight,
 * and the ~7px pointer offset the gate exists to prevent would come back.
 *
 * jsdom runs no animations, so the unit tests for the gate drive
 * `getAnimations()` directly and the pixel harness
 * (scripts/capture-sketch-cursor-offset.mjs) is manual — neither would notice
 * the coupling breaking. This test pins it at the source: the animation
 * classes must sit on the ref'd, role="dialog" element, not a wrapper.
 */
import { describe, it, expect } from 'vitest'
import { createRef } from 'react'
import { render, screen } from '@testing-library/react'
import { Dialog, DialogContent, DialogTitle } from '../components/ui/dialog'

/** The `tw-animate-css` utilities that make up the enter animation.
 *  `animate-in` is what creates the CSS animation; the zoom is what moves the
 *  rendered rect (the thing Excalidraw measures once). */
const ENTER_ANIMATION_CLASSES = [
  'data-[state=open]:animate-in',
  'data-[state=open]:zoom-in-95',
]

describe('ui/dialog — enter animation placement', () => {
  it('keeps the enter animation classes on the ref/role="dialog" element the placement gate reads', () => {
    const ref = createRef<HTMLDivElement>()
    render(
      <Dialog open onOpenChange={() => {}}>
        <DialogContent ref={ref}>
          <DialogTitle>T</DialogTitle>
        </DialogContent>
      </Dialog>,
    )
    const dialog = screen.getByRole('dialog')
    // The forwarded ref is what SketchDialog hands to `getAnimations()`; it must
    // be the very element that carries the animation, not an ancestor of it.
    expect(ref.current).toBe(dialog)
    // Radix drives the `data-[state=open]:` variants off this attribute.
    expect(dialog).toHaveAttribute('data-state', 'open')
    for (const cls of ENTER_ANIMATION_CLASSES) {
      expect(dialog.classList.contains(cls), `missing ${cls} on role="dialog" element`).toBe(true)
    }
  })
})
