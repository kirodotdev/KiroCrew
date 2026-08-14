import { memo, useEffect, useRef, useState } from 'react'
import { Copy, Check } from 'lucide-react'
import { copyCode } from '../utils/clipboard'
import { highlightAsync } from '../utils/highlightClient'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../utils/touchActions'

import { i18nT } from '../i18n/t'
export function HighlightedCode({ code, lang, className }: { code: string; lang: string | undefined; className: string }) {
  const [html, setHtml] = useState('')
  // Reset to plain text the instant the code changes, so a stale highlight from
  // the previous content never lingers while the worker re-highlights.
  const codeRef = useRef(code)
  if (codeRef.current !== code) {
    codeRef.current = code
    if (html) setHtml('')
  }

  useEffect(() => {
    let cancelled = false
    // Highlight in the worker. The row renders as plain text immediately and
    // colorizes when the worker replies — the main thread never runs hljs, so
    // even a backtracking input can't stall scroll/interaction. Highlighting
    // only adds <span> color (same text/lines), so the swap doesn't shift layout.
    highlightAsync(code, lang).then((out) => {
      if (!cancelled && out) setHtml(out)
    })
    return () => { cancelled = true }
  }, [code, lang])

  // Plain (React-escaped) text until the worker returns colorized HTML.
  if (!html) {
    return <code className={`hljs text-[13px] font-mono leading-relaxed ${className}`}>{code}</code>
  }

  return (
    <code
      className={`hljs text-[13px] font-mono leading-relaxed ${className}`}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}

export const CodeBlock = memo(function CodeBlock(
  { code, lang, complete, headerActions }: {
    code: string; lang?: string; complete: boolean; headerActions?: React.ReactNode
  },
) {
  const [copied, setCopied] = useState(false)
  const copy = () => { copyCode(code); setCopied(true); setTimeout(() => setCopied(false), 1500) }

  return (
    <div className="code-block group/code rounded-xl border border-border bg-bg-elevated overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1">
        <span className="text-muted text-[13px] font-mono">{lang || 'code'}</span>
        <div className={`flex items-center gap-1 opacity-0 group-hover/code:opacity-100 group-focus-within/code:opacity-100 transition-opacity ${HOVER_NONE_ACTIONS_ROW_CLS}`}>
          {headerActions}
          <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={copy} title={copied ? i18nT('components.codeBlock.copied') : i18nT('components.codeBlock.copy')} aria-label={copied ? i18nT('components.codeBlock.copied') : i18nT('components.codeBlock.copy')}>
            {copied ? <Check size={13} /> : <Copy size={13} />}
          </button>
        </div>
      </div>
      {/* tabIndex=0 + role/label: a horizontally-scrollable region must be keyboard
          focusable so keyboard-only users can scroll it (axe scrollable-region-focusable).
          The region role is a labelled landmark, so the tabIndex here is intentional. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <pre className="overflow-x-auto scroll-fade px-3 py-2" tabIndex={0} role="region" aria-label={lang ? `${lang} code` : 'code'}>
        <HighlightedCode code={code} lang={lang} className={lang ? `language-${lang}` : ''} />
        {!complete && <span className="text-muted text-[12px] italic animate-pulse ml-2">{i18nT('components.codeBlock.generating')}</span>}
      </pre>
    </div>
  )
})
