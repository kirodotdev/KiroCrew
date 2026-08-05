import hljs from 'highlight.js/lib/core'
import { registerHljsLanguages } from './hljsLanguages'

// Main-thread hljs instance. Code blocks highlight in a Web Worker (see
// highlightClient.ts / hljsWorker.ts) so the main thread never runs hljs's
// backtracking-prone matcher regex; this instance is kept for any direct
// callers and as a non-worker fallback.
registerHljsLanguages(hljs)

export default hljs
