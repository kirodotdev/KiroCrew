/**
 * EditorFooter — Shared footer for pack editors with missing hints + cancel/save.
 */
import React from 'react'
import { i18nT } from '../../i18n/t'
import type { CSSProperties } from 'react'
import { slotLabel } from './slotLabel'

interface Props {
  missingStates: string[]
  canSave: boolean
  saving?: boolean
  onCancel: () => void
  onSave: () => void
  tt: (key: any) => string
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
} satisfies Record<string, CSSProperties>

export const EditorFooter: React.FC<Props> = ({ missingStates, canSave, saving, onCancel, onSave}) => {
  const disabled = !canSave || !!saving
  return (
    <div style={S.footer}>
      {missingStates.length > 0 && (
        <span style={{ fontSize: 11, color: 'var(--danger)', flex: 1 }}>
          {i18nT('apps.crewCompanion.editor.missing', { slots: missingStates.map((s) => slotLabel(s)).join(', ') })}
        </span>
      )}
      <div style={{ flex: missingStates.length > 0 ? undefined : 1 }} />
      <button style={S.cancelBtn} onClick={onCancel}>{i18nT('apps.crewCompanion.editor.cancel')}</button>
      <button
        disabled={disabled}
        onClick={onSave}
        style={{
          padding: '8px 20px', borderRadius: 8, border: 'none', fontSize: 12, fontWeight: 600,
          cursor: disabled ? 'default' : 'pointer',
          background: disabled ? 'var(--cc-input-bg)' : 'var(--accent)',
          color: disabled ? 'var(--text-muted)' : 'var(--cc-accent-text)',
          opacity: disabled ? 0.5 : 1,
        }}
      >
        {saving ? i18nT('apps.crewCompanion.editor.saving') : i18nT('apps.crewCompanion.editor.save')}
      </button>
    </div>
  )
}
