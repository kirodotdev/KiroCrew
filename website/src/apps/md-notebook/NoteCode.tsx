/**
 * Fenced code block in the Notes preview, coloured when the fence names a
 * language.
 *
 * The colouring runs on the dashboard's existing highlight stack:
 * `highlightAsync` posts the code to the shared Web Worker, so highlight.js
 * never runs on the main thread. That matters here more than elsewhere: a note
 * can hold several long blocks and the preview re-renders on every keystroke in
 * the block being edited, and highlight.js is the library that has been
 * profiled catastrophically backtracking on hostile input.
 *
 * A fence with NO language stays plain. The worker would happily
 * `highlightAuto()` it, but a note's unlabelled fence is usually a log, a tree
 * or pasted output, and guessing a grammar for it paints arbitrary words in
 * keyword colours. Colour is applied when the author asked for it.
 *
 * Until the worker replies the code renders as plain React-escaped text, so
 * there is no blank frame and no layout shift when the colours land. The
 * `.hljs-*` classes it emits are coloured by `src/index.css`, which carries a
 * light-mode palette too, so the block follows the app's theme rather than
 * pinning one of its own.
 *
 * The block keeps the app's click-to-edit contract: this component adds no
 * click handler, so the click still reaches the wrapper that opens the source.
 */
import { useEffect, useState } from 'react'
import DOMPurify from 'dompurify'

import { FONT_MONO } from './constants'
import { highlightAsync } from '../../utils/highlightClient'

const PRE_STYLE: React.CSSProperties = {
  background: 'var(--card)',
  border: '1px solid var(--border)',
  borderRadius: '6px',
  padding: '10px',
  fontSize: '12px',
  overflowX: 'auto',
  fontFamily: FONT_MONO,
  margin: 0,
}

export function NoteCode({ code, lang }: { code: string; lang?: string }) {
  const [html, setHtml] = useState('')

  useEffect(() => {
    if (!lang) {
      setHtml('')
      return
    }
    let cancelled = false
    void highlightAsync(code, lang).then(out => {
      if (!cancelled && out) setHtml(out)
    })
    return () => {
      cancelled = true
    }
  }, [code, lang])

  // The pending state renders `code` itself rather than an empty box, so the
  // reader never sees the block disappear while the worker is busy.
  if (!html) return <pre style={PRE_STYLE}>{code}</pre>
  return (
    <pre style={PRE_STYLE}>
      {/* highlightAsync sanitizes its own output; sanitizing again at the sink
          keeps that from being an upstream guarantee a refactor could drop, and
          this HTML derives from note text, which is untrusted input. */}
      <code className="hljs" dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(html) }} />
    </pre>
  )
}
