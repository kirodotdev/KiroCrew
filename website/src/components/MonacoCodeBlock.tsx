import { memo, useState, useCallback, useEffect, useRef, lazy, Suspense } from 'react'
import { Pencil, X, Copy, Check } from 'lucide-react'
import { copyCode } from '../utils/clipboard'
import { CodeBlock } from './CodeBlock'
import RunInTerminalBtn, { SHELL_LANGS } from './RunInTerminalBtn'
import { useTerminalEnabled } from '../utils/terminalRegistry'

import { i18nT } from '../i18n/t'
const Editor = lazy(async () => {
  const { ensureMonacoLocal } = await import('../utils/monacoLocal')
  await ensureMonacoLocal()
  return import('@monaco-editor/react')
})

const LANG_MAP: Record<string, string> = {
  js: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript',
  py: 'python', sh: 'bash', shell: 'bash', zsh: 'bash',
  yml: 'yaml', md: 'markdown', rs: 'rust',
}

export function monacoLang(lang?: string): string {
  if (!lang) return 'plaintext'
  return LANG_MAP[lang] || lang
}

/** Reactively track dark/light theme via MutationObserver on data-theme. */
export function useIsDark(): boolean {
  const [dark, setDark] = useState(
    () => (document.documentElement.getAttribute('data-theme') || '').includes('dark'),
  )
  useEffect(() => {
    const obs = new MutationObserver(() =>
      setDark((document.documentElement.getAttribute('data-theme') || '').includes('dark')),
    )
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => obs.disconnect()
  }, [])
  return dark
}

export function lineCount(code: string): number {
  return Math.min(Math.max(code.split('\n').length, 3), 40)
}

const MonacoCodeBlock = memo(function MonacoCodeBlock(
  { code, lang, complete }: { code: string; lang?: string; complete: boolean },
) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(code)
  const [copied, setCopied] = useState(false)
  const dark = useIsDark()
  const timerRef = useRef<ReturnType<typeof setTimeout>>()

  // #1 Critical: sync value via useEffect instead of during render to avoid cascade
  useEffect(() => {
    if (!editing) setValue(code)
  }, [code, editing])

  // Clean up timeout on unmount
  useEffect(() => () => clearTimeout(timerRef.current), [])

  const copy = useCallback(() => {
    copyCode(value)
    setCopied(true)
    clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => setCopied(false), 1500)
  }, [value])

  const terminalEnabled = useTerminalEnabled()
  const showRunBtn = complete && terminalEnabled && !!lang && SHELL_LANGS.has(lang)

  const headerActions = complete ? (
    <>
      {showRunBtn && <RunInTerminalBtn code={code} />}
      <button
        aria-label={i18nT('components.monacoCodeBlock.edit_code_block')}
        title={i18nT('components.monacoCodeBlock.edit_in_monaco_editor')}
        className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
        onClick={() => setEditing(true)}
      >
        <Pencil size={13} />
      </button>
    </>
  ) : undefined

  if (!editing) {
    return <CodeBlock code={code} lang={lang} complete={complete} headerActions={headerActions} />
  }

  return (
    <div className="code-block rounded-xl border border-border bg-bg-elevated overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1">
        <span className="text-muted text-[13px] font-mono">{lang || 'code'}</span>
        <div className="flex items-center gap-1">
          <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={() => { setValue(code); setEditing(false) }} title={i18nT('components.monacoCodeBlock.close_editor')} aria-label={i18nT('components.monacoCodeBlock.close_editor')}><X size={13} /></button>
          <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={copy} title={copied ? 'Copied!' : 'Copy'} aria-label={copied ? 'Copied!' : 'Copy'}>{copied ? <Check size={13} /> : <Copy size={13} />}</button>
        </div>
      </div>
      <div className="overflow-hidden">
        <Suspense fallback={<div className="p-3 text-muted text-[12px] animate-pulse">{i18nT('components.monacoCodeBlock.loading_editor')}</div>}>
          <Editor
            height={`${lineCount(value) * 19 + 16}px`}
            language={monacoLang(lang)}
            value={value}
            onChange={v => setValue(v ?? '')}
            theme={dark ? 'vs-dark' : 'vs'}
            options={{
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
              fontSize: 13,
              lineNumbers: 'on',
              readOnly: false,
              wordWrap: 'on',
              automaticLayout: true,
              padding: { top: 8, bottom: 8 },
            }}
          />
        </Suspense>
      </div>
    </div>
  )
})

export default MonacoCodeBlock
