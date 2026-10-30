/**
 * Confirmation for standalone windows (Mochi, the Crew Companion gallery) that
 * cannot load the dashboard stylesheet `ConfirmDialog` is styled with. Styled
 * inline from theme variables, with the same answers and focus handling.
 */
import React, { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { i18nT } from '../i18n/t'
import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'

export interface InlineConfirmOptions {
  title: string
  confirmLabel: string
}

interface Question extends InlineConfirmOptions {
  resolve: (ok: boolean) => void
}

const btn: React.CSSProperties = { padding: '6px 12px', borderRadius: 6, fontSize: 12, cursor: 'pointer' }

function InlineConfirmDialog({ question, settle }: { question: Question; settle: (ok: boolean) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const titleId = useId()
  // Marks the Escape that answers this question for window-level handlers:
  // their own "question open" flag is already false when the key reaches them.
  // A composing Escape is left to the IME.
  useLayoutEffect(() => {
    const mark = (e: KeyboardEvent) => { if (e.key === 'Escape' && !e.isComposing) e.preventDefault() }
    window.addEventListener('keydown', mark, true)
    return () => window.removeEventListener('keydown', mark, true)
  }, [])
  useDialogFocusTrap(ref, () => settle(false))
  // Open on the dialog itself, not Cancel: a focus ring on Cancel outweighs the
  // destructive button. Tab still reaches Cancel first.
  useEffect(() => { ref.current?.focus({ preventScroll: true }) }, [])
  return (
    <>
      <div role="presentation" aria-hidden="true" style={{ position: 'fixed', inset: 0, zIndex: 1000, background: 'rgba(0,0,0,0.5)' }} onClick={() => settle(false)} />
      <div style={{ position: 'fixed', inset: 0, zIndex: 1001, display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
        <div
          ref={ref}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          tabIndex={-1}
          style={{ pointerEvents: 'auto', outline: 'none', background: 'var(--bg)', borderRadius: 10, padding: '16px 20px', width: 300, maxWidth: 'calc(100vw - 32px)', border: '1px solid var(--border)', boxShadow: '0 8px 24px rgba(0,0,0,0.3)' }}
        >
          <div id={titleId} style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', marginBottom: 12, overflowWrap: 'anywhere' }}>{question.title}</div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button onClick={() => settle(false)} style={{ ...btn, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text)' }}>
              {i18nT('components.confirmDialog.cancel')}
            </button>
            {/* The danger Btn's emphasised (hover) state, so it outweighs Cancel.
                Theme variables only: both windows install the core tokens. */}
            <button
              data-guide-confirm
              onClick={() => settle(true)}
              style={{
                ...btn,
                border: '1px solid var(--danger)',
                background: 'var(--danger-subtle)',
                color: 'var(--danger-text)',
                fontWeight: 600,
              }}
            >
              {question.confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </>
  )
}

export function useInlineStyledConfirm(): {
  confirm: (opts: InlineConfirmOptions) => Promise<boolean>
  confirmDialog: React.ReactNode
} {
  const [question, setQuestion] = useState<Question | null>(null)
  const pending = useRef<Question | null>(null)

  const settle = useCallback((ok: boolean) => {
    pending.current?.resolve(ok)
    pending.current = null
    setQuestion(null)
  }, [])

  const confirm = useCallback((opts: InlineConfirmOptions) => new Promise<boolean>(resolve => {
    pending.current?.resolve(false)
    const next = { ...opts, resolve }
    pending.current = next
    setQuestion(next)
  }), [])

  useEffect(() => () => { pending.current?.resolve(false) }, [])

  return {
    confirm,
    confirmDialog: question ? <InlineConfirmDialog question={question} settle={settle} /> : null,
  }
}
