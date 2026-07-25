import { useEffect, useMemo, useState } from 'react'
import DOMPurify from 'dompurify'
import hljs from '../utils/hljs'
import { parseUnifiedDiff } from '../utils/parseUnifiedDiff'
import { DIFF_BG, DIFF_FG } from '../utils/diffUtils'

/** Highlight language for a diff, keyed by file extension; null = plain. */
function diffLanguage(path: string): string | null {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  return ext && hljs.getLanguage(ext) ? ext : null
}

/** Defer heavy subtree mounting until just after the drawer's slide-in
 * animation (120ms), so opening the panel animates with lightweight file
 * headers instead of stuttering on thousands of highlighted diff rows. */
function useDeferredMount(delayMs = 140): boolean {
  const [ready, setReady] = useState(false)
  useEffect(() => {
    const id = window.setTimeout(() => setReady(true), delayMs)
    return () => window.clearTimeout(id)
  }, [delayMs])
  return ready
}

/** GitHub-style unified diff renderer with per-line syntax highlighting.
 *  Shared by the Changes panel's PR file rows and the Local Changes view. */
export default function DiffView({ patch, path }: { patch: string; path: string }) {
  const ready = useDeferredMount()
  const rows = useMemo(() => parseUnifiedDiff(patch), [patch])
  const language = useMemo(() => diffLanguage(path), [path])
  // Per-line highlighting keyed by file extension. Lines are highlighted
  // independently (multi-line constructs may reset), which matches the
  // fidelity GitHub's own diff view accepts. hljs escapes the input, so
  // its HTML output is safe to inject.
  const highlighted = useMemo(() => {
    if (!language || !ready) return null
    return rows.map(row =>
      row.kind === 'hunk-gap' ? '' : DOMPurify.sanitize(hljs.highlight(row.text, { language, ignoreIllegals: true }).value),
    )
  }, [rows, language, ready])
  if (!ready) return <div className="px-3 py-3 text-[11px] text-muted">Loading diff…</div>
  return (
    <div className="min-w-max text-[11px] leading-5 font-mono">
      {rows.map((row, index) => {
        if (row.kind === 'hunk-gap') {
          return (
            <div key={index} className="flex items-center gap-2 px-3 py-1 bg-bg-elevated/60 text-muted select-none">
              {row.hiddenCount > 0 ? `${row.hiddenCount} unmodified ${row.hiddenCount === 1 ? 'line' : 'lines'}` : <span className="w-full border-t border-border" />}
            </div>
          )
        }
        const tone = row.kind === 'add' ? DIFF_BG.add : row.kind === 'del' ? DIFF_BG.del : ''
        const marker = row.kind === 'add' ? '+' : row.kind === 'del' ? '-' : ' '
        const markerTone = row.kind === 'add' ? DIFF_FG.add : row.kind === 'del' ? DIFF_FG.del : 'text-muted/40'
        const html = highlighted?.[index]
        return (
          <div key={index} className={`flex min-w-fit ${tone}`}>
            <span className="w-10 shrink-0 px-1 text-right text-muted/50 select-none border-r border-border/30">{row.oldLine ?? ''}</span>
            <span className="w-10 shrink-0 px-1 text-right text-muted/50 select-none border-r border-border/30">{row.newLine ?? ''}</span>
            <span className={`w-4 shrink-0 text-center select-none ${markerTone}`}>{marker}</span>
            {html !== undefined && html !== '' ? (
              <span className="hljs flex-1 whitespace-pre px-2 !bg-transparent" dangerouslySetInnerHTML={{ __html: html }} />
            ) : (
              <span className="flex-1 whitespace-pre px-2 text-text">{row.text}</span>
            )}
          </div>
        )
      })}
    </div>
  )
}
