import { memo, useCallback, useEffect, useRef, useState } from 'react'
import { Pencil, X, Copy, Check } from 'lucide-react'
import { copyCode } from '../utils/clipboard'
import { CodeBlock } from './CodeBlock'
import { PierreEditor } from '../pierre'
import RunInTerminalBtn, { SHELL_LANGS } from './RunInTerminalBtn'
import { useTerminalEnabled } from '../utils/terminalRegistry'

import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import { contentHash } from '../lib/contentHash'

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
  const wrapperRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!editing) valueRef.current = code
  }, [code, editing])
  useEffect(() => () => clearTimeout(timerRef.current), [])

  const copy = useCallback(async () => {
    if (!(await copyCode(valueRef.current))) return
    setCopied(true)
    clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => setCopied(false), 1500)
  }, [])

  // The editor caps at max-h-[480px], so opening it can collapse a tall block
  // by thousands of pixels with the scroll offset left where it was -- most
  // visibly from the FOOTER'S edit button, whose whole point is reachability
  // on a block long enough that its start has scrolled off screen. One code
  // path handles both triggers, gated on the wrapper's top actually being
  // above the viewport: unconditionally calling scrollIntoView would also
  // fire for a HEADER edit on a block sitting mid-viewport (top already
  // visible, just not flush with it), yanking the page up for no reason the
  // click gave it.
  const startEditing = useCallback(() => {
    setEditing(true)
    requestAnimationFrame(() => {
      const el = wrapperRef.current
      if (el && el.getBoundingClientRect().top < 0) {
        el.scrollIntoView({ block: 'start', behavior: 'smooth' })
      }
    })
  }, [])

  const terminalEnabled = useTerminalEnabled()
  const showRunBtn = complete && terminalEnabled && !!lang && SHELL_LANGS.has(lang)

  const editBtn = (
    <button
      aria-label={i18nT('components.monacoCodeBlock.edit_code_block')}
      title={i18nT('components.monacoCodeBlock.edit_code_block')}
      className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
      onClick={startEditing}
    >
      <Pencil size={13} />
    </button>
  )

  // Run + Edit in the header is pre-existing (legacy status under the
  // dashboard's max-two-buttons-per-row cap); the footer is a NEW row this
  // component adds, so it stays under the cap on its own -- Edit only, no
  // Run. Run-in-terminal is also the less likely action to want from the
  // bottom of a long block: it targets the block's start, not wherever the
  // reader scrolled to.
  const headerActions = complete ? <>{showRunBtn && <RunInTerminalBtn code={code} lang={lang} />}{editBtn}</> : undefined
  const footerActions = complete ? editBtn : undefined

  if (!editing) {
    return <CodeBlock code={code} lang={lang} complete={complete} headerActions={headerActions} footerActions={footerActions} />
  }

  return (
    <div ref={wrapperRef} className="code-block rounded-xl border border-border bg-bg-elevated overflow-hidden">
      {/* The header closes off the scroll box below it: once the editor scrolls,
          a partly visible first line meets the header edge, and without a rule
          between them it reads as text sliding under the title bar. Same token
          as the read-only block's footer rule. */}
      <div className="flex items-center justify-between px-3 py-1 border-b border-border">
        <span className="flex items-baseline gap-2 min-w-0">
          <span className="text-muted text-[13px] font-mono">{lang || 'code'}</span>
          {/* The editor never writes back to the chat and the X discards
              silently, so say both up front. No "copy" in the wording: the
              Copy button sits an inch to the right. The span wraps rather than
              truncates: a clipped second sentence would hide the discard
              warning exactly on the narrow (touch) widths that have no hover
              to recover it. */}
          <span className="text-muted text-[11px]">{i18nT('components.monacoCodeBlock.not_saved_hint')}</span>
        </span>
        <div className="flex items-center gap-1">
          <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={() => { valueRef.current = code; setEditing(false) }} title={i18nT('components.monacoCodeBlock.close_editor')} aria-label={i18nT('components.monacoCodeBlock.close_editor')}><X size={13} /></button>
          <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={copy} title={copied ? i18nT('components.monacoCodeBlock.copied') : i18nT('components.monacoCodeBlock.copy')} aria-label={copied ? i18nT('components.monacoCodeBlock.copied') : i18nT('components.monacoCodeBlock.copy')}>{copied ? <Check size={13} /> : <Copy size={13} />}</button>
        </div>
      </div>
      {/* The height cap goes on the editor's OWN scroller (Pierre's Virtualizer,
          which is `overflow-auto` and receives `className`), not on a wrapper
          around it. A wrapper with only `max-height` has an indefinite height,
          so the editor's `h-full` inside it resolves to auto: the surface grows
          to its full content height, the wrapper clips it, and the wheel finds
          nothing scrollable -- it scrolls the transcript instead, and only the
          caret (arrow keys) can move the view. Capping the scroller itself gives
          it a definite max height, so it scrolls under the pointer. */}
      <PierreEditor
        file={{ name: `snippet.${lang || 'txt'}`, contents: code, cacheKey: `chat-edit:${lang}:${code.length}:${contentHash(code)}` }}
        onChange={v => { valueRef.current = v }}
        className="max-h-[480px]"
      />
    </div>
  )
})

export default EditableCodeBlock
