/// <reference lib="webworker" />
/**
 * Runs app `textPatterns` off the dashboard's thread. The main thread terminates
 * this worker when a batch overruns its time budget (see `textPatterns.ts`).
 */
import { scanTexts, type FromWorker, type ToWorker } from './textPatternScan'

const ctx = self as unknown as DedicatedWorkerGlobalScope

ctx.onmessage = (e: MessageEvent<ToWorker>) => {
  const msg = e.data
  if (msg?.type !== 'scan') return
  const post = (m: FromWorker) => ctx.postMessage(m)
  const rejected: number[] = []
  const hits = scanTexts(
    msg.patterns,
    msg.texts,
    (i) => post({ type: 'pattern-start', batch: msg.batch, i }),
    (pattern) => rejected.push(pattern),
  )
  post({ type: 'done', batch: msg.batch, hits, rejected })
}
