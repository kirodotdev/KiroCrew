// Off-screen measure farm: renders small batches of UNMEASURED rows into a
// hidden, zero-height container inside the transcript column and records their
// real heights into the virtualizer's height cache.
//
// Why it exists: measured heights are the only jitter-free geometry. Estimates
// (flat or running-mean) are corrected the moment a row really mounts, and
// under a scroll gesture those corrections land under the reader's finger —
// measured on a 390px viewport as ~180 content-anchored jumps in a 12s
// back-scroll on an unmeasured transcript, versus zero once measured. The farm
// spends idle time converting estimate territory into measured territory so
// the correction never happens on-screen. Heights persist with the cache
// (localStorage), so the sweep is per device+width, once.
//
// Fidelity contract with the renderer:
//  - The caller's renderItem MUST reproduce the transcript's row wrapper
//    byte-for-byte (classes + maxWidth), rendered in DEFAULT disclosure state —
//    which is exactly how an unmeasured row would really mount.
//  - The farm wraps everything in PierreFarmHoldContext, so Pierre surfaces
//    stay on their equal-metrics text stand-ins (the stand-in IS the measured
//    geometry) and nothing queues or latches in the staging pipeline.
//
// Scheduling: a coarse tick picks the next batch only when the transcript is
// IDLE — no scroll within FARM_IDLE_MS, caller not streaming — and only one
// batch is in flight at a time. Measurement is double-rAF'd so the browser has
// committed layout for the batch before offsetHeight is read.

import { useEffect, useRef, useState } from 'react'
import { PierreFarmHoldContext } from '../../components/pierreStaging'

/** Rows measured per farm pass. Small on purpose: a farm mount pays real
 *  render cost for heavy rows; the sweep's throughput matters less than its
 *  invisibility. */
export const FARM_BATCH = 3
/** Deep-idle batch: with no scroll for FARM_DEEP_IDLE_MS the reader is not
 *  watching geometry, so the sweep can afford real throughput (a 700-row
 *  transcript finishes in ~30s instead of ~4min). */
export const FARM_BATCH_DEEP = 8
export const FARM_DEEP_IDLE_MS = 5000
/** USER INPUT (wheel/touch) within this window marks the transcript busy:
 *  the reader's hand is on the page and a farm batch would jank it. */
export const FARM_IDLE_MS = 900
/** Bare SCROLL events get only a short tail. They also arrive from the
 *  virtualizer's own landing compensations (one discrete write per landing),
 *  and giving those the full input window starved the farm during the
 *  top-park walk: every landing reset the idle clock, the farm crawled, the
 *  farm-gated walk crawled behind it, and the reader watched the spinner
 *  for minutes. Momentum coasting still holds the farm (it fires scroll
 *  events continuously, so the tail keeps renewing). */
export const FARM_SCROLL_TAIL_MS = 250
/** Cadence of the pick-next-batch tick. */
export const FARM_TICK_MS = 300

export interface MeasureFarmProps {
  /** Main switch (caller gates on having an active transcript). */
  enabled: boolean
  /** Caller veto for states the tick cannot see (streaming, slot loading). */
  paused?: boolean
  /** Row count of the live items array. */
  count: number
  /** Sweep origin: the first mounted row's index. The sweep walks upward from
   *  here to 0 (history above the reader — where unmeasured rows hurt), then
   *  downward to the end. */
  originIndex: number
  isMeasured: (index: number) => boolean
  /** Rows currently mounted in the live window -- the ResizeObserver owns
   *  their heights; the farm must not offer a second opinion. */
  isMounted?: (index: number) => boolean
  keyAt: (index: number) => string | null
  /** Write-back; returns false when identity moved and the write was dropped. */
  record: (index: number, key: string, px: number) => boolean
  renderItem: (index: number) => React.ReactNode
  /** The transcript's scroll container, for idle detection. */
  scrollerEl: () => HTMLElement | null
  /** Injectable for tests (jsdom has no layout). Defaults to offsetHeight. */
  measureEl?: (el: HTMLElement) => number
}

interface FarmTarget { index: number; key: string }

export function MeasureFarm({
  enabled,
  paused = false,
  count,
  originIndex,
  isMeasured,
  isMounted,
  keyAt,
  record,
  renderItem,
  scrollerEl,
  measureEl,
}: MeasureFarmProps) {
  const [batch, setBatch] = useState<FarmTarget[]>([])
  const batchRef = useRef(batch)
  batchRef.current = batch
  const lastScrollRef = useRef(0)
  const lastInputRef = useRef(0)
  // Live mirrors so the tick reads fresh values without re-arming the interval.
  const liveRef = useRef({ enabled, paused, count, originIndex, isMeasured, isMounted, keyAt })
  liveRef.current = { enabled, paused, count, originIndex, isMeasured, isMounted, keyAt }

  // Idle detection: any scroll on the transcript marks it busy.
  useEffect(() => {
    const el = scrollerEl()
    if (!el) return
    const onScroll = () => { lastScrollRef.current = Date.now() }
    const onInput = () => { lastInputRef.current = Date.now() }
    el.addEventListener('scroll', onScroll, { passive: true })
    el.addEventListener('wheel', onInput, { passive: true })
    el.addEventListener('touchmove', onInput, { passive: true })
    el.addEventListener('touchstart', onInput, { passive: true })
    return () => {
      el.removeEventListener('scroll', onScroll)
      el.removeEventListener('wheel', onInput)
      el.removeEventListener('touchmove', onInput)
      el.removeEventListener('touchstart', onInput)
    }
    // scrollerEl is a stable accessor by contract (caller passes a ref reader).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // The pick tick.
  useEffect(() => {
    if (!enabled) return
    const tick = setInterval(() => {
      const live = liveRef.current
      if (!live.enabled || live.paused) {
        if (batchRef.current.length > 0) setBatch([])
        return
      }
      if (batchRef.current.length > 0) return // a batch is mid-measure
      // The farm is a pure optimisation: it prefetches measurements while the
      // reader is idle, and nothing depends on it having run. So an incomplete
      // input must make it do NOTHING rather than throw out of this timer, where
      // there is no render to catch it and the exception escapes as an unhandled
      // rejection that fails a whole test run while every assertion passes.
      //
      // Deliberately not `live.isMeasured?.(i)` at the call site: `undefined` is
      // falsy, so that spelling reads a missing predicate as "nothing is measured
      // yet" and walks the farm over the entire list -- the most expensive
      // possible response to not knowing.
      if (typeof live.isMeasured !== 'function' || typeof live.keyAt !== 'function') return
      const sinceScroll = Date.now() - lastScrollRef.current
      const sinceInput = Date.now() - lastInputRef.current
      if (sinceInput < FARM_IDLE_MS) return
      if (sinceScroll < FARM_SCROLL_TAIL_MS) return
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      const batchSize = sinceInput >= FARM_DEEP_IDLE_MS ? FARM_BATCH_DEEP : FARM_BATCH
      const next: FarmTarget[] = []
      const consider = (i: number) => {
        if (next.length >= batchSize) return
        if (live.isMeasured(i)) return
        if (live.isMounted?.(i)) return
        const key = live.keyAt(i)
        if (key !== null) next.push({ index: i, key })
      }
      const origin = Math.max(0, Math.min(live.count - 1, live.originIndex))
      // Above the reader first (that is where corrections hurt), then below.
      for (let i = origin; i >= 0 && next.length < batchSize; i--) consider(i)
      for (let i = origin + 1; i < live.count && next.length < batchSize; i++) consider(i)
      if (next.length > 0) setBatch(next)
    }, FARM_TICK_MS)
    return () => clearInterval(tick)
  }, [enabled])

  // Measure the committed batch. Double-rAF: the first rAF fires before paint
  // of this commit; the second guarantees layout for the batch is final.
  const containerRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (batch.length === 0) return
    let cancelled = false
    const measure = () => {
      if (cancelled) return
      const box = containerRef.current
      if (box) {
        const kids = box.querySelectorAll<HTMLElement>('[data-farm-index]')
        kids.forEach((el) => {
          const idx = Number(el.dataset.farmIndex)
          const target = batchRef.current.find((t) => t.index === idx)
          if (!target) return
          const px = measureEl ? measureEl(el) : el.offsetHeight
          // px<=0 (jsdom, display:none edge) is not a measurement — leave the
          // row unmeasured rather than cache a lie.
          if (px > 0) record(target.index, target.key, px)
        })
      }
      setBatch([])
    }
    const r1 = requestAnimationFrame(() => { requestAnimationFrame(measure) })
    return () => { cancelled = true; cancelAnimationFrame(r1) }
    // record/measureEl are stable by contract; batch identity drives the pass.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batch])

  if (!enabled || batch.length === 0) return null
  return (
    <div
      ref={containerRef}
      aria-hidden
      // Zero-height overflow-hidden flow box: children lay out at the real
      // transcript column width (which is what makes the measurements true)
      // but contribute nothing to scrollHeight and paint nothing.
      style={{ height: 0, overflow: 'hidden', visibility: 'hidden', pointerEvents: 'none', overflowAnchor: 'none' }}
    >
      <PierreFarmHoldContext.Provider value={true}>
        {batch.map((t) => (
          <div key={t.key} data-farm-index={t.index}>{renderItem(t.index)}</div>
        ))}
      </PierreFarmHoldContext.Provider>
    </div>
  )
}
