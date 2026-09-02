import { memo, useCallback, useEffect, useRef, useState } from 'react'
import { Pencil, X, Copy, Check } from 'lucide-react'
import { copyCode } from '../utils/clipboard'
import { CodeBlock } from './CodeBlock'
import { PierreEditor } from '../pierre'
import RunInTerminalBtn, { SHELL_LANGS } from './RunInTerminalBtn'
import { useTerminalEnabled } from '../utils/terminalRegistry'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

/** djb2 over the snippet, so two same-language snippets that happen to share a
 *  character count get distinct cache keys. A hash and not the text itself:
 *  the key must stay short and bounded no matter how long the snippet is. */
function contentHash(text: string): string {
  let h = 5381
  for (let i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) | 0
  return (h >>> 0).toString(36)
}

/** Chat code block with an opt-in scratch editor: the pencil swaps the
 *  rendered block for an editable Pierre surface over a LOCAL copy (nothing
 *  is written back to the message), useful for tweaking a snippet before
 *  copying or running it. */
const EditableCodeBlock = memo(function EditableCodeBlock(
  { code, lang, complete }: { code: string; lang?: string; complete: boolean },
) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [editing, setEditing] = useState(false)
  const [copied, setCopied] = useState(false)
  const valueRef = useRef(code)
  const timerRef = useRef<ReturnType<typeof setTimeout>>()

  useEffect(() => {
    if (!editing) valueRef.current = code
  }, [code, editing])
  useEffect(() => () => clearTimeout(timerRef.current), [])

  const copy = useCallback(() => {
    copyCode(valueRef.current)
    setCopied(true)
    clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => setCopied(false), 1500)
  }, [])

  const terminalEnabled = useTerminalEnabled()
  const showRunBtn = complete && terminalEnabled && !!lang && SHELL_LANGS.has(lang)

  const headerActions = complete ? (
    <>
      {showRunBtn && <RunInTerminalBtn code={code} />}
      <button
        aria-label={i18nT('components.monacoCodeBlock.edit_code_block')}
        title={i18nT('components.monacoCodeBlock.edit_code_block')}
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
          <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={() => { valueRef.current = code; setEditing(false) }} title={i18nT('components.monacoCodeBlock.close_editor')} aria-label={i18nT('components.monacoCodeBlock.close_editor')}><X size={13} /></button>
          <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={copy} title={copied ? i18nT('components.monacoCodeBlock.copied') : i18nT('components.monacoCodeBlock.copy')} aria-label={copied ? i18nT('components.monacoCodeBlock.copied') : i18nT('components.monacoCodeBlock.copy')}>{copied ? <Check size={13} /> : <Copy size={13} />}</button>
        </div>
      </div>
      <div className="max-h-[480px] overflow-hidden flex flex-col">
        <PierreEditor
          file={{ name: `snippet.${lang || 'txt'}`, contents: code, cacheKey: `chat-edit:${lang}:${code.length}:${contentHash(code)}` }}
          onChange={v => { valueRef.current = v }}
        />
      </div>
    </div>
  )
})

export default EditableCodeBlock
