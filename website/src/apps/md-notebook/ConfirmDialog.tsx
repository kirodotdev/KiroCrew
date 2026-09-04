/**
 * Themed confirmation dialog for the Notes app.
 *
 * Replaces `window.confirm`, which renders as an OS sheet in the desktop app —
 * the one place the app broke out of its own design language, and unthemeable by
 * construction. Local to this app rather than the dashboard's shared Dialog for
 * the same reason every other surface here is: geometry and colour live in inline
 * styles that no CSS purge can strip, and the app ships as a compiled builtin.
 */
import { useEffect, useRef } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { FONT_BODY } from './constants'

const btn: CSSProperties = {
  height: '30px',
  padding: '0 14px',
  borderRadius: '8px',
  fontSize: '12px',
  fontWeight: 500,
  fontFamily: FONT_BODY,
  cursor: 'pointer',
}

export function ConfirmDialog({
  title,
  body,
  footer,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onCancel,
}: {
  title: string
  /**
   * ReactNode, not a string: the delete copy carries an inline link (the `.trash`
   * folder), and splitting that sentence into two keys to get the link between
   * them would break every language whose word order differs from English.
   */
  body: ReactNode
  /**
   * Optional slot under the body, for a link that explains or acts on what the
   * body describes. Rendered outside the button row so it cannot be mistaken for
   * a third choice, and it never dismisses the dialog.
   */
  footer?: ReactNode
  confirmLabel: string
  cancelLabel: string
  onConfirm: () => void
  onCancel: () => void
}) {
  const confirmRef = useRef<HTMLButtonElement | null>(null)

  // Focus the destructive action on open so Enter confirms and Tab reaches the
  // two buttons immediately, and close on Escape. Capture phase: the app binds a
  // window-level sync shortcut, and Escape here must not also reach the note.
  useEffect(() => {
    confirmRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.preventDefault()
      e.stopPropagation()
      onCancel()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onCancel])

  return (
    // The scrim is a presentational backdrop, not a control: the dialog's own
    // Cancel button and Escape are the accessible ways out, and clicking away is
    // a mouse convenience on top of those.
    <div
      role="presentation"
      onClick={onCancel}
      style={{
        position: 'absolute',
        inset: 0,
        zIndex: 50,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'rgba(0,0,0,0.55)',
      }}
    >
      {/* The card's only handler shields it from the scrim's dismiss-on-click, so
          it adds no gesture of its own: Confirm and Cancel are real <button>s,
          and the window Escape bound above dismisses without the pointer. */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions -- stopPropagation only: keeps a click inside the dialog from reaching the scrim's dismiss */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={e => e.stopPropagation()}
        style={{
          width: 'min(420px, calc(100% - 48px))',
          boxSizing: 'border-box',
          padding: '18px 20px 16px',
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border)',
          borderRadius: '12px',
          boxShadow: '0 12px 32px rgba(0,0,0,0.35)',
          fontFamily: FONT_BODY,
        }}
      >
        <div style={{ fontSize: '15px', fontWeight: 600, color: 'var(--text)' }}>{title}</div>
        <div
          style={{
            marginTop: '8px',
            fontSize: '12.5px',
            lineHeight: 1.5,
            color: 'var(--muted)',
          }}
        >
          {body}
        </div>
        {footer && <div style={{ marginTop: '10px' }}>{footer}</div>}
        <div
          style={{
            marginTop: '18px',
            display: 'flex',
            justifyContent: 'flex-end',
            gap: '8px',
          }}
        >
          <button
            type="button"
            className="mdnb-dlg-cancel"
            onClick={onCancel}
            style={{
              ...btn,
              background: 'transparent',
              border: '1px solid var(--border)',
              color: 'var(--text)',
            }}
          >
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className="mdnb-dlg-danger"
            onClick={onConfirm}
            style={{
              ...btn,
              background: 'var(--danger)',
              border: '1px solid var(--danger)',
              color: 'var(--danger-fg)',
            }}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
