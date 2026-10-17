// Has the detail pane's tall title scrolled out of its scroller?
//
// This replaces a scroll handler with a threshold. The threshold approach needed
// a magic number (how far is "past the title"?) that was wrong for every title
// whose line count differed from the one it was tuned on, plus a hysteresis band
// to stop the header flickering. Observing the title element answers the actual
// question — is it still on screen — with no constant to tune, no scroll
// listener on a hot path, and no band, because nothing here changes the scroll
// height while it fires.
//
// Both refs are callback refs on purpose: the scroller and the title mount and
// unmount with the pane (a pane opened from a cross-reference renders a skeleton
// first and swaps in the real title later), and a callback ref re-runs on that
// swap where a `useEffect` keyed on `.current` would not.
import { useCallback, useEffect, useRef, useState } from 'react'

export function useTitleScrolledOut() {
  const [scrolledOut, setScrolledOut] = useState(false)
  const scrollerRef = useRef<HTMLElement | null>(null)
  const titleRef = useRef<HTMLElement | null>(null)
  const observerRef = useRef<IntersectionObserver | null>(null)

  const connect = useCallback(() => {
    observerRef.current?.disconnect()
    observerRef.current = null
    const root = scrollerRef.current
    const target = titleRef.current
    if (!root || !target) return
    // `root` is the pane's own scroller, not the viewport: the pane is nested,
    // and above `sm:` the wrapper stops scrolling entirely — with a viewport
    // root the title would read as "on screen" forever there and the compact
    // echo would never be asked to fade in or out.
    const io = new IntersectionObserver(
      ([entry]) => setScrolledOut(!entry.isIntersecting),
      { root, threshold: 0 },
    )
    io.observe(target)
    observerRef.current = io
  }, [])

  const setScroller = useCallback((node: HTMLElement | null) => {
    scrollerRef.current = node
    connect()
  }, [connect])

  const setTitle = useCallback((node: HTMLElement | null) => {
    titleRef.current = node
    connect()
  }, [connect])

  useEffect(() => () => observerRef.current?.disconnect(), [])

  return { scrolledOut, setScroller, setTitle }
}
