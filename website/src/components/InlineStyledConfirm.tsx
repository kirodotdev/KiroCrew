/**
 * Mochi filters dashboard component CSS, while Crew Companion loads it.
 * Inline theme tokens let both galleries share one confirmation surface.
 */
import React, { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { i18nT } from '../i18n/t'
import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { useDocumentImeLatch } from '../hooks/useImeGuard'
import { guideConfirm } from '../uiLocations/targetRegistry'

export interface InlineConfirmOptions {
  title: string
  confirmLabel: string
  backdropInset?: React.CSSProperties['inset']
  backdropRadius?: React.CSSProperties['borderRadius']
}

interface Question extends InlineConfirmOptions {
  resolve: (ok: boolean) => void
}

const NO_ESCAPE = () => {}
const btn: React.CSSProperties = { padding: '6px 12px', borderRadius: 6, font: 'inherit', fontSize: 13, cursor: 'pointer' }

function InlineConfirmDialog({ question, settle }: { question: Question; settle: (ok: boolean) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const titleId = useId()
  const ime = useDocumentImeLatch(true)
  useDialogFocusTrap(ref, NO_ESCAPE, { handleEscape: false })
  // The window must skip the Escape that cancels this question.
  useLayoutEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || !ime.claimKey(e)) return
      e.preventDefault()
      settle(false)
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [ime, settle])
  // Neutral focus keeps Enter from approving a replacement question.
  useEffect(() => { ref.current?.focus({ preventScroll: true }) }, [question])
  return (
    <>
      <div role="presentation" aria-hidden="true" style={{ position: 'fixed', inset: question.backdropInset ?? 0, borderRadius: question.backdropRadius, zIndex: 1000, background: 'color-mix(in srgb, var(--bg) 60%, transparent)' }} onClick={() => settle(false)} />
      <div style={{ position: 'fixed', inset: 0, zIndex: 1001, display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
        <div
          ref={ref}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          tabIndex={-1}
          style={{ pointerEvents: 'auto', outline: 'none', background: 'var(--bg)', borderRadius: 10, padding: '16px 20px', width: 300, maxWidth: 'calc(100vw - 32px)', border: '1px solid var(--border)', boxShadow: 'var(--shadow-lg)' }}
        >
          <div id={titleId} style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', marginBottom: 12, overflowWrap: 'anywhere' }}>{question.title}</div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button type="button" onClick={() => settle(false)} style={{ ...btn, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text)' }}>
              {i18nT('components.confirmDialog.cancel')}
            </button>
            <button
              type="button"
              {...guideConfirm(confirmRef)}
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

  useEffect(() => () => {
    pending.current?.resolve(false)
    pending.current = null
  }, [])

  return {
    confirm,
    confirmDialog: question ? <InlineConfirmDialog question={question} settle={settle} /> : null,
  }
}
