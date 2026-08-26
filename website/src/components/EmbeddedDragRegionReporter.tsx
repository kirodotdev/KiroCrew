/**
 * EmbeddedDragRegionReporter — the embedded (remote-pane) reporter that lets the
 * Electron host make the pane's title bar draggable.
 *
 * A remote pane is a cross-origin <iframe>; the host's injected drag region and
 * its `no-drag` blanket cover only the host's main frame (the whole iframe is
 * `no-drag`), and draggable regions do not descend into a subframe. So neither
 * side can make the pane's header move the window on its own. This reporter
 * measures the header's control-free gaps and relays them up; the host
 * (InstancesViewport) re-adds `-webkit-app-region: drag` in exactly those gaps.
 *
 * Runs only when this SPA is an embedded pane AND its host is an Electron window
 * (relayed via the host model) — a browser host has no native window to drag, so
 * there is nothing to report. No-op everywhere else.
 */
import { useEffect, useRef } from 'react'
import { useAppSelector } from '../store'
import { isEmbeddedPane } from '../lib/embedded'
import { computeHeaderDragGaps } from '../lib/dragGaps'

export default function EmbeddedDragRegionReporter() {
  // The host model tells the pane whether its host is an Electron window; only
  // then is a draggable title bar meaningful. We also watch the model OBJECT
  // itself (not just the electron flag): the host relays a fresh model every
  // time it re-engages this pane (tab switch, pane readiness, the instances
  // poll), and each such relay is our cue to RE-ASSERT the gaps.
  const hostModel = useAppSelector(s => s.instances.host)
  const hostElectron = !!hostModel?.electron

  // A single mc-drag-gaps post is fire-and-forget: if the host drops it (its
  // listener/port map not yet ready when a pane first shows) the geometry-change
  // dedup below means nothing re-sends it, so the pane silently stays undraggable
  // until an incidental header reflow. These refs let a second effect force a
  // re-post whenever the host re-engages, closing that race.
  const lastJsonRef = useRef('')
  const scheduleRef = useRef<() => void>(() => {})

  useEffect(() => {
    if (!isEmbeddedPane() || !hostElectron) return
    const parent = window.parent
    if (!parent || parent === window) return

    // A fresh (re)subscription re-asserts: clear the dedup so the first post
    // always fires even if the geometry is unchanged from a prior mount.
    lastJsonRef.current = ''
    let raf = 0
    const post = () => {
      raf = 0
      const header = document.querySelector('header.topbar-glass')
      // Skip while the pane is not laid out (hidden/inactive): a zero-width band
      // yields no gaps, and posting stale geometry for an off-screen pane is
      // pointless (the host only reads the active pane's gaps).
      if (!header || window.innerWidth === 0) return
      const gaps = computeHeaderDragGaps(header, window.innerWidth)
      const json = JSON.stringify(gaps)
      if (json === lastJsonRef.current) return
      lastJsonRef.current = json
      try {
        // The host validates our loopback ORIGIN (resolveTunnelOrigin), so the
        // wildcard target is safe and matches the sibling mc-embedded-ready ping.
        // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
        parent.postMessage({ type: 'mc-drag-gaps', v: 1, gaps }, '*')
      } catch {
        /* parent gone / mid-navigation — the next change reposts */
      }
    }
    // Coalesce bursts (a metrics tick + a theme change in one frame) into one
    // measurement.
    const schedule = () => {
      // Cancel-and-reschedule, not `if (raf) return`: requestAnimationFrame may
      // return a handle whose callback never runs (a frame queued for a page the
      // browser then puts in the back/forward cache is dropped), and a latch on
      // such a handle would stop drag-region reporting permanently. Same defect
      // and same fix as CliPanel's theme scheduler.
      if (raf) window.cancelAnimationFrame(raf)
      raf = window.requestAnimationFrame(post)
    }
    // Expose the live scheduler so the re-engagement effect below can trigger a
    // re-post without tearing down and re-attaching the observers.
    scheduleRef.current = schedule

    const header = document.querySelector('header.topbar-glass')
    // Header-scoped observers only — cheap, and they catch every reflow that
    // moves a control: window/zoom resize, a badge widening a cluster (header
    // stays the same size, so a header-only ResizeObserver would miss it), a
    // theme swap, and the pane going visible (0 -> full width fires the RO).
    const ro = new ResizeObserver(schedule)
    const mo = new MutationObserver(schedule)
    if (header) {
      ro.observe(header)
      for (const c of Array.from(header.children)) ro.observe(c)
      mo.observe(header, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['class', 'style'],
      })
    }
    window.addEventListener('resize', schedule)
    schedule()

    return () => {
      if (raf) window.cancelAnimationFrame(raf)
      ro.disconnect()
      mo.disconnect()
      window.removeEventListener('resize', schedule)
      scheduleRef.current = () => {}
    }
  }, [hostElectron])

  // Re-assert the gaps whenever the host relays a fresh model. The host
  // rebroadcasts on every tab switch / pane-ready / poll (InstancesViewport's
  // broadcast effect), so this fires exactly when the host has (re)started
  // listening — the moment a previously-dropped initial post would otherwise be
  // lost forever. Clearing the dedup makes the re-post fire even if the geometry
  // is byte-identical to what we last sent. Cheap: the host reads only the active
  // pane's gaps, so an extra post to a background pane is harmless.
  useEffect(() => {
    if (!isEmbeddedPane() || !hostElectron) return
    lastJsonRef.current = ''
    scheduleRef.current()
  }, [hostModel, hostElectron])

  return null
}
