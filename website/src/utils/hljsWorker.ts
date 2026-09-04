/// <reference lib="webworker" />
// Highlight worker — runs highlight.js off the main thread. highlight.js builds
// one giant alternation regex per language and runs RegExp.exec; on some inputs
// that catastrophically backtracks (profiled at 327ms), which would freeze the
// UI if it ran on the main thread. Here a slow highlight only delays this
// worker's reply — the page stays responsive and scroll stays at full fps.
import hljs from 'highlight.js/lib/core'
import { registerHljsLanguages } from './hljsLanguages'

registerHljsLanguages(hljs)

interface HighlightRequest {
  id: number
  code: string
  lang?: string
}
interface HighlightResponse {
  id: number
  html: string
}

const ctx = self as unknown as DedicatedWorkerGlobalScope

ctx.onmessage = (e: MessageEvent<HighlightRequest>) => {
  const { id, code, lang } = e.data
  let html = ''
  try {
    if (lang && hljs.getLanguage(lang)) {
      html = hljs.highlight(code, { language: lang }).value
    } else {
      html = hljs.highlightAuto(code).value
    }
  } catch (e) {
    // Plain-text fallback is correct, but leave a breadcrumb in the worker
    // console (devtools) so a systematically-failing language is noticeable.
    html = ''
    // eslint-disable-next-line no-console
    console.warn('[hljsWorker] highlight failed', (e as Error)?.name)
  }
  const res: HighlightResponse = { id, html }
  ctx.postMessage(res)
}
