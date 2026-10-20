/**
 * The push-down reveal the redaction cards open and close with, split out of
 * RedactionCards.tsx to keep that file under the line cap. RedactionCards
 * re-exports Reveal and REVEAL_MS, so callers keep importing them from there.
 */
import React, { useLayoutEffect, useEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { noteInPlaceResize } from '../hooks/virtualizer/inPlaceResize'

/** How long a reveal takes; the exit keeps its content mounted this long. */
export const REVEAL_MS = 180
/** Control points of the reveal's curves: a decelerating open and a gentle
 *  ease-in-out close. */
const EASE_OPEN = [0.2, 0, 0, 1] as const
const EASE_CLOSE = [0.4, 0, 0.2, 1] as const
const bezier = (p: readonly number[]) => `cubic-bezier(${p.join(', ')})`

/**
 * Opens and closes its content so the card pushes the text below it down as
 * it appears, and draws that text back up as it closes.
 *
 * The layout changes ONCE, at the start of an open or the end of a close; the
 * movement in between is a transform on the card and on everything after it
 * up to the scroll container (FLIP). Transform and opacity animations run on
 * the compositor, so they keep a steady frame rate while the main thread is
 * busy with the commit that inserted the card. A height transition moves on
 * the main thread instead, and a phone recording showed it skipping straight
 * from closed to nearly open. The card slides out from under the line above
 * inside a clipping box, and the text below moves by the same amount, so the
 * two edges stay together on every frame. A reveal mounted already open
 * animates in unless `appear` is false (the coach, which is part of the reply
 * rather than something the reader opened). Reduced-motion users get the
 * instant switch.
 */
export function Reveal(props: RevealProps) {
  return <PushReveal {...props} />
}

interface RevealProps {
  show: boolean
  children: React.ReactNode
  className?: string
  appear?: boolean
  /** Names what the open reveal is showing. A change while open swaps the
   *  content in place and moves the text below by the height difference,
   *  instead of closing one panel while another opens. */
  contentKey?: string
}

function PushReveal({ show, children, className = '', appear = true, contentKey }: RevealProps) {
  const [mounted, setMounted] = useState(show)
  const [clip, setClip] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const running = useRef<Animation[]>([])
  const firstRender = useRef(true)
  // The box's height as last laid out. A resize observer keeps it, so when a
  // content change commits, this still holds the height from before it.
  const laidOutH = useRef(0)
  const lastKey = useRef(contentKey)
  if (show && !mounted) setMounted(true)
  useEffect(() => {
    const box = boxRef.current
    if (!box || typeof ResizeObserver === 'undefined') return
    laidOutH.current = box.offsetHeight
    const ro = new ResizeObserver(() => { laidOutH.current = box.offsetHeight })
    ro.observe(box)
    return () => ro.disconnect()
  }, [mounted])
  useLayoutEffect(() => {
    const prevKey = lastKey.current
    lastKey.current = contentKey
    const box = boxRef.current
    const inner = box?.firstElementChild as HTMLElement | null
    if (prevKey === contentKey || !show || !box || !inner || running.current.length || !canAnimate(box)) return
    const d = box.offsetHeight - laidOutH.current
    const timing: KeyframeAnimationOptions = { duration: REVEAL_MS, easing: bezier(EASE_OPEN), fill: 'forwards' }
    noteInPlaceResize(box.parentElement ?? box, box.getBoundingClientRect().top)
    const shift = `translateY(${-d}px)`
    const moving = [
      inner.animate([{ opacity: 0 }, { opacity: 1 }], timing),
      ...(d === 0 ? [] : followingContent(box, Math.abs(d)).map(el => el.animate([{ transform: shift }, { transform: 'none' }], { ...timing, composite: 'add' }))),
      // The frame to resize is the card this area sits in, or, when the area
      // IS a card (one card replacing another), that card's own frame.
      ...(d === 0 ? [] : frameAnimations(box, d, true, { ...timing, composite: 'add' },
        box.parentElement?.closest<HTMLElement>('[data-card-frame-host]')
          ?? inner.querySelector<HTMLElement>(':scope > [data-card-frame-host]'))),
    ]
    running.current = moving
    moving[0].onfinish = () => {
      if (running.current !== moving) return
      running.current = []
      settle(moving)
    }
  }, [contentKey, show])
  useLayoutEffect(() => {
    const initial = firstRender.current
    firstRender.current = false
    const box = boxRef.current
    const inner = box?.firstElementChild as HTMLElement | null
    settle(running.current)
    running.current = []
    if (!box || !inner) return
    // A box that starts above the top of the viewport changes nothing the
    // reader is looking at: the virtualizer holds their view still across it,
    // so it swaps without motion instead of dragging the visible text with it.
    const boxTop = box.getBoundingClientRect().top
    const above = boxTop < viewportTop(box)
    if ((initial && !appear) || !canAnimate(box) || above) {
      // Tell the virtualizer how much of this row changed above the fold, so
      // a change on screen in the same row at the same moment does not hide
      // it (see inPlaceResize).
      if (above && !initial) {
        const travel = measuredTravel(box, followingContent(box, 0)[0]) ?? box.getBoundingClientRect().height
        noteInPlaceResize(box.parentElement ?? box, boxTop, 0, show ? travel : -travel)
      }
      if (!show) setMounted(false)
      return
    }
    const h = box.getBoundingClientRect().height
    // The box's own height change is on screen, so the virtualizer must not
    // treat it as a reprice above the reader (see inPlaceResize).
    const anchor = box.parentElement ?? box
    if (show) noteInPlaceResize(anchor, box.getBoundingClientRect().top)
    const shift = `translateY(${-h}px)`
    const from = show ? shift : 'none'
    const to = show ? 'none' : shift
    const timing: KeyframeAnimationOptions = {
      duration: REVEAL_MS,
      // Open decelerates into place; close eases in so it does not jump at its first frame.
      easing: bezier(show ? EASE_OPEN : EASE_CLOSE),
      fill: 'forwards',
    }
    setClip(true)
    // Opening fades the content in as it slides out. Closing a whole card
    // keeps it opaque, so it visibly retracts under the line above as the
    // text below draws up over its place; closing a section inside a card
    // fades its text out as it retracts, which reads more gently in the small
    // space.
    const inCard = !!box.parentElement?.closest('[data-card-frame-host]')
    const card = inner.animate(
      show
        ? [{ transform: from, opacity: 0 }, { transform: to, opacity: 1 }]
        : inCard
          ? [{ transform: from, opacity: 1 }, { transform: to, opacity: 0 }]
          : [{ transform: from }, { transform: to }],
      timing,
    )
    const followers = followingContent(box, h)
    const travel = measuredTravel(box, followers[0]) ?? h
    const follow = show ? [`translateY(${-travel}px)`, 'none'] : ['none', `translateY(${-travel}px)`]
    const moving = [
      card,
      // `add`: when one card closes while another opens further down, the
      // text below both moves by the sum of the two, not by whichever
      // animation happened to start last.
      ...followers.map(el => el.animate([{ transform: follow[0] }, { transform: follow[1] }], { ...timing, composite: 'add' })),
      ...frameAnimations(box, travel, show, { ...timing, composite: 'add' }),
    ]
    running.current = moving
    card.onfinish = () => {
      if (running.current !== moving) return
      running.current = []
      if (show) {
        setClip(false)
        settle(moving)
        return
      }
      // Remove the box and drop the transforms in one commit, so the text
      // below lands where the transforms were holding it.
      noteInPlaceResize(anchor, box.getBoundingClientRect().top)
      flushSync(() => setMounted(false))
      settle(moving)
    }
  }, [show, mounted, appear])
  useEffect(() => () => settle(running.current), [])
  if (!mounted) return null
  return (
    // flow-root keeps the content's margins inside the box whether or not it
    // clips, so the height the transforms travel is the height it occupies.
    <div ref={boxRef} data-reveal={show ? 'open' : 'closed'} className={`flow-root ${clip ? 'overflow-hidden' : ''} ${className}`}>
      <div>{children}</div>
    </div>
  )
}

/**
 * Ends each animation and removes its effect. Native animations with
 * `fill: 'forwards'` hold their last frame until cancelled and never write
 * the element's inline style, so cancelling hands every element back to the
 * page exactly as it was, with no inline style left to race a cleanup
 * (Framer's `animate()` writes its final frame into the inline style).
 */
function settle(moving: Animation[]): void {
  for (const anim of moving) anim.cancel()
}

/**
 * A section opening inside a details card also has to grow the card's frame
 * with it. The frame is drawn by three layers behind the content (see
 * CardFrame) so it can follow with transforms alone: the middle layer scales
 * from its top edge, the bottom cap slides with the text below, and the
 * shadow layer scales with the whole card. The layout is already at its
 * opened height when this runs, so an open starts the frame `h` shorter and a
 * close ends it `h` shorter, matching the content.
 */
function frameAnimations(
  box: HTMLElement,
  h: number,
  show: boolean,
  timing: KeyframeAnimationOptions,
  host?: HTMLElement | null,
): Animation[] {
  const card = host ?? box.parentElement?.closest<HTMLElement>('[data-card-frame-host]')
  if (!card) return []
  const mid = card.querySelector<HTMLElement>(':scope > [data-card-frame] > [data-frame="mid"]')
  const cap = card.querySelector<HTMLElement>(':scope > [data-card-frame] > [data-frame="bottom"]')
  const shadow = card.querySelector<HTMLElement>(':scope > [data-card-frame] > [data-frame="shadow"]')
  if (!mid || !cap || !shadow) return []
  const midH = mid.offsetHeight
  const cardH = shadow.offsetHeight
  if (midH <= 0 || cardH <= 0) return []
  const shortMid = `scaleY(${Math.max(0, (midH - h) / midH)})`
  const shortCard = `scaleY(${Math.max(0, (cardH - h) / cardH)})`
  const up = `translateY(${-h}px)`
  const pair = (el: HTMLElement, short: string) =>
    el.animate(show ? [{ transform: short }, { transform: 'none' }] : [{ transform: 'none' }, { transform: short }], timing)
  return [pair(mid, shortMid), pair(shadow, shortCard), pair(cap, up)]
}

/**
 * How far the first block below moves when `box` appears or goes: its top
 * with the box laid out, minus its top with the box taken out of the layout
 * for one synchronous measurement. This is the box's height plus whatever
 * margin its neighbours stop sharing, however deeply those margins collapse,
 * which no reading of the box alone can know.
 */
function measuredTravel(box: HTMLElement, first: HTMLElement | undefined): number | null {
  if (!first) return null
  const withBox = first.getBoundingClientRect().top
  const display = box.style.display
  box.style.display = 'none'
  const without = first.getBoundingClientRect().top
  box.style.display = display
  return withBox - without
}

/** The viewport-y of the top of `el`'s scroll container (0 for the page). */
function viewportTop(el: HTMLElement): number {
  for (let p = el.parentElement; p; p = p.parentElement) {
    if (scrollsVertically(p)) return p.getBoundingClientRect().top
  }
  return 0
}

/**
 * Whether `el` is a vertical scroll container. A computed `overflow-y: auto`
 * alone is not enough: `overflow-x: auto` on a code block computes the other
 * axis to `auto` too, and the body's overflow belongs to the viewport.
 */
function scrollsVertically(el: HTMLElement): boolean {
  if (el === document.body || el === document.documentElement) return false
  const overflowY = getComputedStyle(el).overflowY
  return (overflowY === 'auto' || overflowY === 'scroll') && el.scrollHeight > el.clientHeight
}

function canAnimate(el: HTMLElement): boolean {
  return typeof el.animate === 'function' && !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

/** Upper bound on the elements a reveal moves; past it the rest is off screen. */
const MAX_FOLLOWERS = 60

/**
 * Everything laid out after `box` inside its scroll container: its later
 * siblings, then its parent's later siblings, and so on up to the scroller.
 * Those are the boxes the reveal's height change moves, so they are the ones
 * that must ride along with it. Content that stays below the viewport throughout is left
 * alone.
 */
function followingContent(box: HTMLElement, travel: number): HTMLElement[] {
  const out: HTMLElement[] = []
  // Tops are measured with the card in place, so anything within `travel` of
  // the viewport's bottom edge is on screen at some point of the movement.
  const limit = window.innerHeight + travel
  // A `display: contents` wrapper draws no box and ignores a transform, so its
  // children are the boxes that move.
  const take = (s: Element): boolean => {
    if (out.length >= MAX_FOLLOWERS || !(s instanceof HTMLElement)) return true
    const display = getComputedStyle(s).display
    if (display === 'none') return true
    if (display === 'contents') {
      for (const c of Array.from(s.children)) if (!take(c)) return false
      return true
    }
    if (s.getBoundingClientRect().top > limit) return false
    out.push(s)
    return true
  }
  let el: HTMLElement = box
  while (el.parentElement && out.length < MAX_FOLLOWERS) {
    for (let s = el.nextElementSibling; s; s = s.nextElementSibling) if (!take(s)) break
    const parent = el.parentElement
    if (scrollsVertically(parent)) break
    el = parent
  }
  return out
}
