/**
 * EditorFooter — Shared footer for pack editors with missing hints + cancel/save.
 */
import { AlertTriangle, Save } from 'lucide-react'
import React from 'react'
import { i18nT } from '../../../../i18n/t'
import { stateLabel } from '../../i18nKeys'

interface Props {
  missingStates: string[]
  canSave: boolean
  saving?: boolean
  /**
   * A failed SAVE, as opposed to `missingStates` (a failed validation). Shown
   * here so the editor can stay mounted when a save fails: the owning page's
   * error banner only renders on its gallery view, so reporting there forced a
   * navigation away — which threw away every frame, row and name the user had
   * configured. Same slot, because both answer "why did Save not work?".
   */
  saveError?: string | null
  onCancel: () => void
  onSave: () => void
}

const S = {
  footer: {
    padding: '12px 20px',
    borderTop: '1px solid var(--border)',
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    flexShrink: 0,
  },
  cancelBtn: {
    padding: '8px 20px', borderRadius: 8, border: '1px solid var(--border)',
    background: 'transparent', color: 'var(--text)', fontSize: 12, cursor: 'pointer',
  },
}

export const EditorFooter: React.FC<Props> = ({
  missingStates,
  canSave,
  saving,
  saveError,
  onCancel,
  onSave,
}) => {
  const disabled = !canSave || !!saving
  // A save failure outranks a validation hint: the user already pressed Save, so
  // "it did not save" is the newer and more actionable fact.
  const notice = saveError || (missingStates.length > 0
    ? i18nT('apps.mochi.editor.missing', { states: missingStates.map(s => stateLabel(s)).join(', ') })
    : '')
  return (
    <div style={S.footer}>
      {notice !== '' && (
        <span
          // Announced, because the failure arrives after the click rather than
          // in response to a keystroke the user can see the result of.
          role={saveError ? 'alert' : undefined}
          style={{ fontSize: 11, color: 'var(--danger)', flex: 1, display: 'inline-flex', alignItems: 'center', gap: 4 }}
        >
          <AlertTriangle size={12} />
          {notice}
        </span>
      )}
      <div style={{ flex: notice !== '' ? undefined : 1 }} />
      <button style={S.cancelBtn} onClick={onCancel}>{i18nT('apps.mochi.editor.cancel')}</button>
      <button
        disabled={disabled}
        onClick={onSave}
        style={{
          padding: '8px 20px', borderRadius: 8, border: 'none', fontSize: 12, fontWeight: 600,
          cursor: disabled ? 'default' : 'pointer',
          background: disabled ? 'var(--bg-input)' : 'var(--accent)',
          color: disabled ? 'var(--text-muted)' : 'var(--accent-text)',
          opacity: disabled ? 0.5 : 1,
        }}
      >
        <Save size={12} style={{ marginRight: 5, verticalAlign: '-2px' }} />
        {saving ? i18nT('apps.mochi.editor.saving') : i18nT('apps.mochi.editor.save')}
      </button>
    </div>
  )
}
