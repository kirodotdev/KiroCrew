import { useState, useCallback, type ReactNode, type Ref } from 'react'
import { Eye, EyeOff, X, ExternalLink, RotateCcw, Trash2, Lock } from 'lucide-react'
import { Input } from './ui'

import { i18nT } from '../i18n/t'

export interface SecretFieldPermanentRemoval {
  label: string
  text: string
  buttonRef?: Ref<HTMLButtonElement>
  confirmation?: ReactNode
}

/**
 * A write-only credential field. Stored secrets are never displayed: the
 * stored state renders a masked preview (e.g. `xoxb-••••wxyz`) with Replace /
 * Remove only — no API path returns the raw value. When unset — or while
 * replacing — it shows a paste input with a show/hide toggle (that toggle
 * covers only the user's own in-flight input, not a stored secret).
 *
 * The parent owns the pending `value` and `cleared` state. Replacement-editor
 * state is local unless controlled through `editing` / `onEditingChange`; the
 * parent also decides whether a remove request is deferred or confirmed now.
 */
export interface SecretFieldProps {
  label: string
  description?: string
  placeholder?: string
  /** True when a secret is currently persisted on the server. */
  isSet: boolean
  /** Masked preview to show when a secret is stored and not revealed. */
  preview: string
  /** Read-only view (remote session): masked display only, no actions. */
  readOnly?: boolean
  /** Pending new value being typed (empty when none). */
  value: string
  onChange: (v: string) => void
  /** Optional controlled replacement-editor state. */
  editing?: boolean
  onEditingChange?: (editing: boolean) => void
  /** True when the user has marked the stored secret for removal. */
  cleared: boolean
  onClearedChange: (cleared: boolean) => void
  /** Optional permanent removal UI; absent means the standard deferred icon action. */
  permanentRemoval?: SecretFieldPermanentRemoval
  /** Optional "where do I get this" link rendered as an external-link icon. */
  setupLink?: { href: string; label?: string }
}

export function SecretField({
  label, description, placeholder, isSet, preview, value, onChange,
  editing: controlledEditing, onEditingChange,
  cleared, onClearedChange, permanentRemoval,
  setupLink, readOnly = false,
}: SecretFieldProps) {
  const [localEditing, setLocalEditing] = useState(false)
  const [revealed, setRevealed] = useState(false)
  const editing = controlledEditing ?? localEditing
  const removeActionLabel = permanentRemoval?.label ?? i18nT('components.secretField.remove')

  const updateEditing = useCallback((next: boolean) => {
    setLocalEditing(next)
    onEditingChange?.(next)
  }, [onEditingChange])

  const startReplace = useCallback(() => {
    updateEditing(true)
    setRevealed(false)
    onChange('')
  }, [onChange, updateEditing])

  const cancelReplace = useCallback(() => {
    updateEditing(false)
    setRevealed(false)
    onChange('')
  }, [onChange, updateEditing])

  const requestRemove = () => onClearedChange(true)

  const undoRemove = () => onClearedChange(false)

  const buttonBase = 'h-8 flex items-center justify-center rounded-md border border-border bg-bg-elevated text-muted hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all disabled:opacity-40 disabled:cursor-not-allowed'
  const iconBtn = `w-8 ${buttonBase}`

  return (
    <div data-setting-label={label} className="flex flex-col gap-1.5 py-1.5">
      <div className="flex items-center gap-1.5">
        <span className="text-[13px] font-semibold text-text">{label}</span>
        {setupLink && (
          <a href={setupLink.href} target="_blank" rel="noopener noreferrer"
            className="text-muted hover:text-accent transition-colors" aria-label={setupLink.label ?? 'Where to find this'}>
            <ExternalLink size={12} />
          </a>
        )}
      </div>
      {description && <div className="text-[12px] text-muted">{description}</div>}

      {readOnly ? (
        <div className="flex flex-col gap-1.5">
          <code className="truncate rounded-md border border-border bg-bg-elevated px-3 py-2 text-[13px] text-muted font-mono">
            {isSet ? preview : '(not set)'}
          </code>
          <div className="flex items-center gap-1.5 text-[12px] text-muted">
            <Lock size={12} />
            {i18nT('components.secretField.managed_on_the_server_read_only_from_remote_sess')}
          </div>
        </div>
      ) : cleared ? (
        <div className="flex items-center justify-between gap-2 rounded-md border border-danger bg-bg-elevated px-3 py-2">
          <span className="text-[12px] text-danger">{i18nT('components.secretField.will_be_removed_on_save')}</span>
          <button type="button" className={iconBtn} onClick={undoRemove} aria-label={i18nT('components.secretField.undo_remove')} title={i18nT('components.secretField.undo')}>
            <RotateCcw size={14} />
          </button>
        </div>
      ) : isSet && !editing ? (
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <code className="flex-1 truncate rounded-md border border-border bg-bg-elevated px-3 py-2 text-[13px] text-text font-mono">
              {preview}
            </code>
            <button type="button" className={iconBtn} onClick={startReplace} aria-label={i18nT('components.secretField.replace')} title={i18nT('components.secretField.replace')}>
              <RotateCcw size={14} />
            </button>
            {permanentRemoval?.confirmation ? (
              <div className="flex basis-full flex-wrap items-center gap-2">
                {permanentRemoval.confirmation}
              </div>
            ) : (
              <button
                type="button"
                ref={permanentRemoval?.buttonRef}
                className={permanentRemoval
                  ? `${buttonBase} gap-1.5 border-danger px-2 text-danger hover:border-danger hover:text-danger`
                  : `${iconBtn} hover:text-danger hover:border-danger`}
                onClick={requestRemove}
                aria-label={removeActionLabel}
                title={removeActionLabel}
              >
                <Trash2 size={14} />
                {permanentRemoval && (
                  <span className="text-[12px] font-medium">{permanentRemoval.text}</span>
                )}
              </button>
            )}
          </div>
          <div className="flex items-center gap-1.5 text-[12px] text-muted">
            <Lock size={12} />
            {i18nT('components.secretField.stored_securely_and_never_displayed_replace_to_r')}
          </div>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <Input
            type={revealed ? 'text' : 'password'}
            value={value}
            onChange={e => onChange(e.target.value)}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            aria-label={label}
            className="font-mono"
          />
          <button type="button" className={iconBtn} onClick={() => setRevealed(r => !r)}
            aria-label={revealed ? i18nT('components.secretField.hide') : i18nT('components.secretField.show')} title={revealed ? i18nT('components.secretField.hide') : i18nT('components.secretField.show')}>
            {revealed ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
          {isSet && (
            <button type="button" className={iconBtn} onClick={cancelReplace} aria-label={i18nT('components.secretField.cancel')} title={i18nT('components.secretField.cancel')}>
              <X size={14} />
            </button>
          )}
        </div>
      )}

    </div>
  )
}
