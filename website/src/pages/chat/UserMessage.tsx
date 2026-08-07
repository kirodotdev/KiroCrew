import { memo, useState, useRef, useEffect, useCallback } from 'react'
import { motion } from 'framer-motion'
import { Pencil, Send, Copy, Check, Link2, Target } from 'lucide-react'
import { copyToClipboard } from '../../utils/clipboard'
import { copySessionLink } from '../../utils/shareUrl'
import { useSearchHighlight, useCurrentOcc } from '../../hooks/SearchHighlightContext'
import { applySearchHighlights } from '../../utils/domHighlight'
import { scrollCurrentMatchIntoView } from '../../utils/searchScroll'
import { type PasteBlock, expandAll as expandPasteTokens } from '../../utils/pasteTokens'

import { i18nT } from '../../i18n/t'
// Steer bubbles play a one-shot entrance (slide-in + ring pulse) when they land.
// The chat transcript is virtualized, so a row can remount when scrolled away and
// back; without this guard the entrance would replay every time. Module-level set
// persists for the app session — each steered message animates exactly once.
const animatedSteers = new Set<string>()

interface UserMessageProps {
  content: string
  meta?: Record<string, unknown>
  timestamp?: string
  timestampTitle?: string
  renderContent: (content: string, meta: Record<string, unknown> | undefined) => React.ReactNode
  canEdit?: boolean
  messageIndex?: number
  messageTs?: string
  onEditResend?: (index: number, ts: string, newContent: string) => void
  slotKey?: string
  slotTitle?: string
  mode?: string
}

const UserMessage = memo(function UserMessage({ content, meta, timestamp, timestampTitle, renderContent, canEdit, messageIndex, messageTs, onEditResend, slotKey, slotTitle, mode }: UserMessageProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(content)
  const [copied, setCopied] = useState(false)
  const [linkCopied, setLinkCopied] = useState(false)
  const taRef = useRef<HTMLTextAreaElement>(null)
  // Track the copy-reset timer so it can be cleared on unmount.  Without this,
  // the 1.5 s setTimeout below survives test teardown and fires after jsdom
  // has been disposed, throwing "ReferenceError: window is not defined" from
  // React's `getCurrentEventPriority` and failing the build under vitest 3.x.
  const copyResetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (copyResetTimerRef.current) clearTimeout(copyResetTimerRef.current)
  }, [])

  // A steered message was injected into the running turn (meta.steer set by the
  // steer_push WS echo). Render it distinctly and animate it in exactly once.
  const isSteer = !!(meta && (meta as { steer?: boolean }).steer)
  const [playSteer] = useState(() => {
    if (!isSteer) return false
    // Stable identity across the steer lifecycle: the optimistic bubble mounts
    // with a client ts (messageTs), then the steer_push reconcile stashes that
    // client ts as meta.clientTs and swaps messageTs to the server ts. Keying
    // the guard on clientTs first keeps the identity constant, so a
    // virtualization remount after the reconcile still hits the set and the
    // entrance animation plays exactly once.
    const key = ((meta as { clientTs?: string })?.clientTs) || messageTs || content
    if (animatedSteers.has(key)) return false
    animatedSteers.add(key)
    return true
  })

  useEffect(() => {
    if (editing && taRef.current) {
      const ta = taRef.current
      ta.focus()
      ta.selectionStart = ta.selectionEnd = ta.value.length
    }
  }, [editing])

  const startEdit = useCallback(() => {
    // Expand any collapsed paste tokens into their original content so the
    // user can actually edit the pasted text. Once edited, the message is
    // resent as plain expanded text — no chip reconstruction.
    const pastes = (meta?.pastes as PasteBlock[] | undefined) || []
    const initial = pastes.length ? expandPasteTokens(content, pastes) : content
    setDraft(initial)
    setEditing(true)
  }, [content, meta])
  const cancel = useCallback(() => setEditing(false), [])
  const submit = useCallback(() => {
    const trimmed = draft.trim()
    if (!trimmed) { setEditing(false); return }
    onEditResend?.(messageIndex ?? 0, messageTs ?? '', trimmed)
    setEditing(false)
  }, [draft, onEditResend, messageIndex, messageTs])

  const userRef = useRef<HTMLDivElement>(null)
  const { term, caseSensitive } = useSearchHighlight()
  const currentOcc = useCurrentOcc()

  useEffect(() => {
    if (!userRef.current) return
    const el = userRef.current
    applySearchHighlights(el, term, caseSensitive, currentOcc)
    // Converge-center the exact occurrence (see scrollCurrentMatchIntoView).
    // Cancel on re-run/unmount so rapid navigation doesn't accumulate loops.
    const cancelScroll = currentOcc >= 0 ? scrollCurrentMatchIntoView(el) : undefined
    return () => cancelScroll?.()
  }, [term, caseSensitive, currentOcc, content])

  /** Native select+copy from a sent bubble gives the literal chip label
   *  ("Paste #1 · 5 lines") — worthless on the other end. Intercept the
   *  copy event, clone the selected DOM, swap each `[data-paste-seq]` chip
   *  for its expanded content, and write that to the clipboard instead. */
  const handleCopy = useCallback((e: React.ClipboardEvent<HTMLDivElement>) => {
    const pastes = (meta?.pastes as PasteBlock[] | undefined) || []
    if (!pastes.length) return
    const sel = window.getSelection()
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return
    const range = sel.getRangeAt(0)
    if (!userRef.current?.contains(range.commonAncestorContainer)) return
    const frag = range.cloneContents()
    const chips = frag.querySelectorAll('[data-paste-seq]')
    if (!chips.length) return
    const bySeq = new Map(pastes.map(p => [p.seq, p]))
    chips.forEach(chip => {
      const seq = Number(chip.getAttribute('data-paste-seq'))
      const block = bySeq.get(seq)
      if (block) chip.replaceWith(document.createTextNode(block.content))
    })
    const tmp = document.createElement('div')
    tmp.appendChild(frag)
    const text = tmp.textContent ?? ''
    if (!text) return
    e.clipboardData.setData('text/plain', text)
    e.preventDefault()
  }, [meta])

  if (editing) {
    return (
      <div data-role="user" className="group/msg flex flex-col items-end">
        {/* `edit-grow` is a CSS grid auto-sizer: a hidden ::after mirror (fed by
            data-replicated-value) drives the grid track so the textarea grows
            with its own content — width AND height — exactly like the read-only
            bubble it replaces, capped by max-w-[550px]. No JS measurement. */}
        <div
          className="edit-grow px-4 py-1.5 text-sm leading-relaxed rounded-xl bg-card text-card-fg overflow-hidden min-w-0 max-w-[550px] outline outline-2 -outline-offset-2 outline-accent/60"
          data-replicated-value={draft}
          style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}
        >
          <textarea
            ref={taRef}
            rows={1}
            aria-label={i18nT('pages.chat.userMessage.edit_message')}
            className="bg-transparent text-card-fg resize-none overflow-hidden focus:outline-none text-sm leading-relaxed"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } if (e.key === 'Escape') cancel() }}
          />
        </div>
        {/* Actions sit BELOW the bubble (like the read-only action row) so they
            never impose a min-width floor on the auto-sized bubble. */}
        <div className="flex justify-end gap-1.5 mt-1">
          <button onClick={cancel} className="px-2.5 py-1 text-[13px] text-muted hover:text-text rounded border border-border hover:bg-hover transition-colors" title={i18nT('pages.chat.userMessage.cancel_esc')}>
            {i18nT('pages.chat.userMessage.cancel')}
          </button>
          <button onClick={submit} className="flex items-center gap-1 px-2.5 py-1 text-[13px] bg-accent text-accent-fg rounded hover:bg-accent/80 transition-colors" title={i18nT('pages.chat.userMessage.send_enter')}>
            <Send size={10} /> {i18nT('pages.chat.userMessage.send')}
          </button>
        </div>
      </div>
    )
  }

  const bubble = (
    // 'message-bubble' is a stable theming hook — see website/docs/theming-contract.md
    <div ref={userRef} onCopy={handleCopy} className={`message-bubble msg-content px-4 py-1.5 text-sm leading-relaxed rounded-xl overflow-hidden min-w-0 max-w-[550px] ${isSteer ? 'bg-accent-subtle text-text' : 'bg-card text-card-fg'}`} style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
      {renderContent(content, meta)}
    </div>
  )

  return (
    <div data-role="user" className="group/msg flex flex-col items-end">
      {/* User-typed line breaks (Shift+Enter) are preserved at the markdown
          level, NOT via container `white-space: pre-wrap`. renderUserContentCb
          renders user content through MarkdownRenderer with `softBreaks`, which
          turns lone source newlines (CommonMark soft breaks) into hard breaks
          (<br>). Container pre-wrap is avoided because react-markdown emits
          literal "\n" text nodes between block elements; under pre-wrap those
          render as visible blank lines and inflate the gaps between list
          items and paragraphs. Assistant markdown keeps standard
          CommonMark soft-break-collapse. */}
      {isSteer ? (
        <>
          {/* Injected into the RUNNING turn — badge + accent bubble + one-shot
              entrance so the steer is visibly distinct from a normal message. */}
          <div className="inline-flex items-center gap-1 text-[12px] font-semibold text-accent mb-1 pr-1">
            <Target size={12} className="shrink-0" /> {i18nT('pages.chat.userMessage.steered_into_the_running_turn')}
          </div>
          <motion.div
            className="relative"
            initial={playSteer ? { opacity: 0, x: 16 } : false}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.32, ease: 'easeOut' }}
          >
            {bubble}
            {playSteer && (
              <motion.div
                aria-hidden="true"
                className="pointer-events-none absolute -inset-0.5 rounded-xl border-2 border-accent"
                initial={{ opacity: 0.55, scale: 1 }}
                animate={{ opacity: 0, scale: 1.04 }}
                transition={{ duration: 0.9, ease: 'easeOut' }}
              />
            )}
          </motion.div>
        </>
      ) : bubble}
      <div className="flex items-center gap-1.5 px-1 mt-1 opacity-0 transition-opacity duration-300 delay-100 group-hover/msg:opacity-100 group-hover/msg:delay-300 group-focus-within/msg:opacity-100 group-focus-within/msg:delay-300">
        <button
          onClick={() => {
            const pastes = (meta?.pastes as PasteBlock[] | undefined) || []
            const toCopy = pastes.length ? expandPasteTokens(content, pastes) : content
            copyToClipboard(toCopy).then(() => {
              setCopied(true)
              if (copyResetTimerRef.current) clearTimeout(copyResetTimerRef.current)
              copyResetTimerRef.current = setTimeout(() => {
                copyResetTimerRef.current = null
                setCopied(false)
              }, 1500)
            }).catch(() => {})
          }}
          className="text-muted hover:text-text p-0.5 rounded transition-colors"
          title={i18nT('pages.chat.userMessage.copy')}
          aria-label={i18nT('pages.chat.userMessage.copy')}
        >
          {copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
        </button>
        {messageTs && slotKey && (
          <button
            onClick={() => { copySessionLink(slotKey, slotTitle, messageTs, mode).then(() => { setLinkCopied(true); setTimeout(() => setLinkCopied(false), 1500) }).catch(() => {}) }}
            className="text-muted hover:text-text p-0.5 rounded transition-colors"
            title={i18nT('pages.chat.userMessage.copy_link_to_message')}
            aria-label={i18nT('pages.chat.userMessage.copy_link_to_message')}
          >
            {linkCopied ? <Check size={14} className="text-ok" /> : <Link2 size={14} />}
          </button>
        )}
        {canEdit && onEditResend && (
          <button
            onClick={startEdit}
            className="text-muted hover:text-text p-0.5 rounded transition-colors"
            title={i18nT('pages.chat.userMessage.edit_resend')}
            aria-label={i18nT('pages.chat.userMessage.edit_resend')}
          >
            <Pencil size={14} />
          </button>
        )}
        {/* No `font-mono`: see the twin in AssistantMessage's footer — a
            formatted date is prose, and `font-mono` pinned `var(--mono)`, which
            the Font Family setting never writes. */}
        {timestamp && <span className="text-muted text-[12px] tabular-nums" title={timestampTitle}>{timestamp}</span>}
      </div>
    </div>
  )
})

export default UserMessage
