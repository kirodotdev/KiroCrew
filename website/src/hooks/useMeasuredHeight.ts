import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * The rendered height of an element, measured rather than predicted.
 *
 * This exists because the composer used to reserve space for its strips from
 * hand-computed constants — `81` derived from `h-16 + py-2 + border-t`, and two
 * more like it — each a number a human read off Tailwind classes that live in a
 * different file. Nothing enforced the correspondence, so changing `py-2` to
 * `py-1.5`, or a chip's font size, silently made the reservation wrong. The
 * failure is quiet: the strip renders either way, and only a reader who had
 * manually resized the composer (the path where the reserved floor is applied)
 * sees the strip eat into the textarea or leave a gap.
 *
 * Returns a CALLBACK ref, for the same reason `useScrollEdges` does: the
 * measured node mounts and unmounts as content is staged and cleared, long
 * after the component holding this hook mounted. A mount-only effect would see
 * a null node once and never run again. Binding from the ref callback attaches
 * whenever the node arrives, and reports **0** when it leaves — an absent strip
 * reserves nothing, which is what the caller's arithmetic already assumes.
 *
 * Measurement is `getBoundingClientRect().height`: it is fractional and
 * includes transforms, so a half-pixel border is not silently floored away, and
 * it matches what the layout actually did rather than what the classes imply.
 * The initial read happens in the ref callback, so the first measured value is
 * available in the same commit the node appears in rather than a frame later.
 *
 * Where `ResizeObserver` is unavailable (jsdom without a stub, an old embedded
 * webview) the initial read still lands and later size CHANGES are simply not
 * observed. That degrades to a stale value, never to a wrong constant, and the
 * common case — a strip appearing or disappearing — is a mount/unmount that the
 * ref callback catches regardless.
 *
 * The unmount effect must not be the only thing that disconnects: under
 * StrictMode (dev) React runs every effect's cleanup and then the effect again
 * on mount, while the ref callback runs once — so a cleanup-only effect left
 * the observer dead after the first render and the height frozen at its mount
 * value (the list dock stayed at its search-row height while an error notice
 * grew it). The effect therefore re-attaches to the node it still holds.
 */
export function useMeasuredHeight<T extends HTMLElement>(): [(node: T | null) => void, number] {
  const [height, setHeight] = useState(0)
  const nodeRef = useRef<T | null>(null)
  const observerRef = useRef<ResizeObserver | null>(null)

  const observe = useCallback((node: T) => {
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(entries => {
      for (const entry of entries) {
        setHeight(entry.target.getBoundingClientRect().height)
      }
    })
    observer.observe(node)
    observerRef.current = observer
  }, [])

  const measuredRef = useCallback((node: T | null) => {
    observerRef.current?.disconnect()
    observerRef.current = null
    nodeRef.current = node
    if (!node) {
      setHeight(0)
      return
    }
    setHeight(node.getBoundingClientRect().height)
    observe(node)
  }, [observe])

  useEffect(() => {
    if (nodeRef.current && !observerRef.current) observe(nodeRef.current)
    return () => {
      observerRef.current?.disconnect()
      observerRef.current = null
    }
  }, [observe])

  return [measuredRef, height]
}
