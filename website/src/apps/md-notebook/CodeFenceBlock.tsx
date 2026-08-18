/**
 * Rendered fenced code block for the Notes preview.
 *
 * Colours only when the fence names a language. The worker falls back to
 * `highlightAuto()` on an unrecognized-but-present language, matching the
 * dashboard's chat code blocks, but a BARE fence must never reach the worker
 * at all: a note's unlabelled fence is usually a log, a directory tree or
 * pasted output, and `highlightAuto()` would paint arbitrary words in keyword
 * colours. That gate is why this doesn't just reuse `components/CodeBlock.tsx`'s
 * `HighlightedCode` — the other reason is its Tailwind-baked 13px face, which
 * would desync from this block's own 12px `FONT_MONO` sizing.
 *
 * Plain text renders immediately; the worker's reply swaps in without moving
 * the block, same contract as the chat renderer's own code blocks.
 */
import { useEffect, useRef, useState } from 'react'
import { highlightAsync } from '../../utils/highlightClient'
import { FONT_MONO } from './constants'

const PRE_STYLE: React.CSSProperties = {
  background: 'var(--card)',
  border: '1px solid var(--border)',
  borderRadius: '6px',
  padding: '10px',
  fontSize: '12px',
  overflowX: 'auto',
  fontFamily: FONT_MONO,
}

export function CodeFenceBlock({ code, lang }: { code: string; lang: string | undefined }) {
  const [html, setHtml] = useState('')
  // Reset to plain text the instant the code or language changes, so a stale
  // highlight from the previous content never lingers while the worker
  // re-highlights — the preview re-renders on every keystroke in the block
  // being edited.
  const key = `${lang ?? ''}\n${code}`
  const keyRef = useRef(key)
  if (keyRef.current !== key) {
    keyRef.current = key
    if (html) setHtml('')
  }

  useEffect(() => {
    if (!lang) return
    let cancelled = false
    highlightAsync(code, lang).then(out => {
      if (!cancelled && out) setHtml(out)
    })
    return () => {
      cancelled = true
    }
  }, [code, lang])

  return (
    <pre style={PRE_STYLE}>
      {html ? <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} /> : code}
    </pre>
  )
}
