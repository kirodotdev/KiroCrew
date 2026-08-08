/**
 * SaveDialog — Shared overwrite/save-as-new dialog for pack editors.
 */
import React from 'react'
import { PANEL_RADIUS } from './panelSkin'
import { GALLERY_PAD } from './constants'

interface Props {
  visible: boolean
  onOverwrite: () => void
  onSaveNew: () => void
  onCancel: () => void
  i18nT: (key: any) => string
}

export const SaveDialog: React.FC<Props> = ({ visible, onOverwrite, onSaveNew, onCancel, i18nT }) => {
  if (!visible) return null
  return (
    <div style={{ position: 'fixed', inset: GALLERY_PAD, borderRadius: PANEL_RADIUS.card, zIndex: 1000, background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ background: 'var(--bg)', borderRadius: 10, padding: '16px 20px', width: 280, border: '1px solid var(--border)', boxShadow: '0 8px 24px rgba(0,0,0,0.3)' }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', marginBottom: 12 }}>{i18nT('apps.crewCompanion.editor.saveAs')}</div>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button onClick={onCancel} style={{ padding: '6px 12px', borderRadius: 6, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text)', fontSize: 12, cursor: 'pointer' }}>{i18nT('apps.crewCompanion.editor.cancel')}</button>
          <button onClick={onOverwrite} style={{ padding: '6px 12px', borderRadius: 6, border: 'none', background: 'var(--accent)', color: 'var(--cc-accent-text)', fontSize: 12, fontWeight: 600, cursor: 'pointer' }}>{i18nT('apps.crewCompanion.editor.overwrite')}</button>
          <button onClick={onSaveNew} style={{ padding: '6px 12px', borderRadius: 6, border: '1px solid var(--accent)', background: 'transparent', color: 'var(--accent)', fontSize: 12, cursor: 'pointer' }}>{i18nT('apps.crewCompanion.editor.saveNew')}</button>
        </div>
      </div>
    </div>
  )
}
