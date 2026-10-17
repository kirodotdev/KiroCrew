/**
 * Mochi's fenced-code renderer, on the core's highlighting stack.
 *
 * The port arrived with `react-syntax-highlighter` (Prism + the `oneDark`
 * theme), which meant the product shipped TWO highlighters: Prism here and the
 * dashboard's highlight.js everywhere else. Two stacks means two language sets,
 * two theme vocabularies (Prism's palette is fixed, so Mochi's code blocks
 * stayed dark in a light theme) and two bundles for one job. This uses
 * `highlightAsync` — the same Web Worker the dashboard's CodeBlock uses.
 *
 * Not the core `CodeBlock` component itself: that is styled with Tailwind
 * utilities, and Mochi's windows deliberately receive core CSS *variables*
 * only, not component styles. So the ENGINE is shared and the presentation
 * stays Mochi's — inline styles matching the rest of the panel.
 *
 * Highlighting happens off the main thread and lands as `.hljs-*` spans, whose
 * colors are admitted into this window by `shared/themes.ts`. Until the worker
 * replies the code renders as plain (React-escaped) text, so there is no blank
 * frame and no layout shift when the colors arrive.
 */
import { useEffect, useRef, useState } from 'react'
import DOMPurify from 'dompurify'
import { highlightAsync } from '../../../utils/highlightClient'

export function MochiCodeBlock({ code, lang }: { code: string; lang: string }) {
  const [html, setHtml] = useState('')

  // Drop a stale highlight the instant the content changes: during streaming the
  // same block re-renders with more code on every chunk, and keeping the old
  // HTML would show the previous, shorter snippet until the worker caught up.
  const codeRef = useRef(code)
  if (codeRef.current !== code) {
    codeRef.current = code
    if (html) setHtml('')
  }

  useEffect(() => {
    let cancelled = false
    highlightAsync(code, lang).then((out) => {
      if (!cancelled && out) setHtml(out)
    })
    return () => {
      cancelled = true
    }
  }, [code, lang])

  return (
    <div style={{ position: 'relative', margin: '4px 0' }}>
      <div
        style={{
          position: 'absolute',
          top: 2,
          right: 6,
          fontSize: 9,
          color: '#888',
          textTransform: 'uppercase',
        }}
      >
        {lang}
      </div>
      <pre
        style={{
          margin: 0,
          padding: '20px 8px 8px',
          borderRadius: 6,
          fontSize: 11,
          overflowX: 'auto',
          background: 'var(--bg-input)',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {html ? (
          <code
            className={`hljs language-${lang}`}
            // Defense-in-depth: highlightAsync already DOMPurify-sanitizes its
            // output, but sanitize again at the sink so this never relies on an
            // upstream guarantee that a future refactor could drop.
            dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(html) }}
          />
        ) : (
          <code className={`hljs language-${lang}`}>{code}</code>
        )}
      </pre>
    </div>
  )
}
