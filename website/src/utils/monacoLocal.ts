/**
 * Configure Monaco to use the locally-bundled package instead of fetching
 * from cdn.jsdelivr.net (which is blocked on corporate networks).
 *
 * Must be called once before any Monaco component mounts. Subsequent calls
 * are no-ops (loader.config is idempotent when the instance hasn't changed).
 */
import { loader } from '@monaco-editor/react'

let configPromise: Promise<void> | null = null

export function ensureMonacoLocal(): Promise<void> {
  if (!configPromise) {
    configPromise = (async () => {
      const monaco = await import('monaco-editor')

      // Point Monaco's web-worker factory at Vite-bundled worker entries.
      // Without this, Monaco tries to load workers from the CDN path and
      // falls back to the main thread (degraded but functional).
      self.MonacoEnvironment = {
        getWorker(_workerId: string, label: string) {
          switch (label) {
            case 'json':
              return new Worker(new URL('monaco-editor/esm/vs/language/json/json.worker.js', import.meta.url), { type: 'module' })
            case 'css':
            case 'scss':
            case 'less':
              return new Worker(new URL('monaco-editor/esm/vs/language/css/css.worker.js', import.meta.url), { type: 'module' })
            case 'html':
            case 'handlebars':
            case 'razor':
              return new Worker(new URL('monaco-editor/esm/vs/language/html/html.worker.js', import.meta.url), { type: 'module' })
            case 'typescript':
            case 'javascript':
              return new Worker(new URL('monaco-editor/esm/vs/language/typescript/ts.worker.js', import.meta.url), { type: 'module' })
            default:
              return new Worker(new URL('monaco-editor/esm/vs/editor/editor.worker.js', import.meta.url), { type: 'module' })
          }
        },
      }

      loader.config({ monaco })
    })()
  }
  return configPromise
}
