/**
 * PackInfoHeader — Shared header for pack editors (SVG/Lottie + Sprite).
 * Contains: title, name, author, description, flipX checkbox.
 */
import React from 'react'
import { i18nT } from '../../i18n/t'
import type { CSSProperties } from 'react'

interface Props {
  title: string
  name: string
  author: string
  description: string
  flipX: boolean
  onNameChange: (v: string) => void
  onAuthorChange: (v: string) => void
  onDescriptionChange: (v: string) => void
  onFlipXChange: (v: boolean) => void
  tt: (key: any, vars?: Record<string, string>) => string
}

const S = {
  header: {
    padding: '14px 20px',
    borderBottom: '1px solid var(--border)',
    background: 'var(--header-bg)',
    flexShrink: 0,
  },
  title: { fontSize: 15, fontWeight: 600 as const, marginBottom: 10 },
  row: { display: 'flex', gap: 10, marginBottom: 6 },
  group: { flex: 1, display: 'flex', flexDirection: 'column' as const, gap: 2 },
  label: { fontSize: 11, color: 'var(--text-muted)' },
  input: {
    background: 'var(--cc-input-bg)', border: '1px solid var(--border)', borderRadius: 6,
    padding: '4px 8px', color: 'var(--text)', fontSize: 12, outline: 'none', width: '100%',
  },
} satisfies Record<string, CSSProperties>

export const PackInfoHeader: React.FC<Props> = ({
  title, name, author, description, flipX,
  onNameChange, onAuthorChange, onDescriptionChange, onFlipXChange,
}) => (
  <div style={S.header}>
    <div style={S.title}>{title}</div>
    <div style={S.row}>
      <div style={S.group}>
        <span style={S.label}>{i18nT('apps.crewCompanion.editor.name')}</span>
        <input style={S.input} value={name} onChange={e => onNameChange(e.target.value)} placeholder={i18nT('apps.crewCompanion.editor.namePlaceholder')} />
      </div>
      <div style={S.group}>
        <span style={S.label}>{i18nT('apps.crewCompanion.editor.author')}</span>
        <input style={S.input} value={author} onChange={e => onAuthorChange(e.target.value)} placeholder={i18nT('apps.crewCompanion.editor.authorPlaceholder')} />
      </div>
    </div>
    <div style={{ marginBottom: 6 }}>
      <span style={S.label}>{i18nT('apps.crewCompanion.editor.description')}</span>
      <input style={S.input} value={description} onChange={e => onDescriptionChange(e.target.value)} placeholder={i18nT('apps.crewCompanion.editor.descPlaceholder')} />
    </div>
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 2 }}>
      <span style={{ fontSize: 12, color: 'var(--text)' }}>{i18nT('apps.crewCompanion.editor.flipX')}</span>
      <div
            role="switch" aria-checked={flipX} tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onFlipXChange(!flipX) } }}
        onClick={() => onFlipXChange(!flipX)}
        style={{
          width: 34, height: 20, borderRadius: 10, cursor: 'pointer',
          background: flipX ? 'var(--accent)' : 'var(--border)',
          position: 'relative', transition: 'background 200ms',
        }}
      >
        <div style={{
          width: 16, height: 16, borderRadius: 8,
          background: '#fff', boxShadow: '0 1px 3px rgba(0,0,0,0.3)',
          position: 'absolute', top: 2,
          left: flipX ? 16 : 2,
          transition: 'left 200ms',
        }} />
      </div>
    </div>
  </div>
)
