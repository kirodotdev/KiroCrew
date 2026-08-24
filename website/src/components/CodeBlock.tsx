import { memo, useMemo, useState } from 'react'
import { Copy, Check } from 'lucide-react'
import { copyCode } from '../utils/clipboard'
import { PierreCode } from '../pierre'
import { HOVER_NONE_ACTIONS_ROW_CLS } from '../utils/touchActions'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

export const CodeBlock = memo(function CodeBlock(
  { code, lang, complete, headerActions }: {
    code: string; lang?: string; complete: boolean; headerActions?: React.ReactNode
  },
) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [copied, setCopied] = useState(false)
  const copy = () => { copyCode(code); setCopied(true); setTimeout(() => setCopied(false), 1500) }
  // Stable file identity per (code, lang): Pierre diffs options/files by
  // reference first, so a fresh object every render would force re-renders.
  const file = useMemo(() => ({ name: `snippet.${lang || 'txt'}`, contents: code }), [code, lang])

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
      <div className="pierre-surface scroll-fade" tabIndex={0} role="region" aria-label={lang ? `${lang} code` : 'code'}>
        {complete ? (
          <PierreCode file={file} langHint={lang} />
        ) : (
          <pre className="overflow-x-auto px-3 py-2 m-0"><code className="text-[13px] font-mono leading-relaxed">{code}</code></pre>
        )}
        {!complete && <div className="px-3 pb-2 text-muted text-[12px] italic animate-pulse">{i18nT('components.codeBlock.generating')}</div>}
      </div>
    </div>
  )
})
