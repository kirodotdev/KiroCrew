/**
 * PackInfoHeader — Shared header for pack editors (SVG/Lottie + Sprite).
 * Contains: title, name, author, description, flipX checkbox.
 */
import React, { useId } from 'react'
import { i18nT } from '../../../../i18n/t'

interface Props {
  /** ReactNode, not string: the heading carries a lucide icon component
   *  (an emoji inside the translated title could not follow the theme). */
  title: React.ReactNode
  name: string
  author: string
  description: string
  flipX: boolean
  onNameChange: (v: string) => void
  onAuthorChange: (v: string) => void
  onDescriptionChange: (v: string) => void
  onFlipXChange: (v: boolean) => void
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
    background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6,
    padding: '4px 8px', color: 'var(--text)', fontSize: 12, outline: 'none', width: '100%',
  },
}

export const PackInfoHeader: React.FC<Props> = ({
  title, name, author, description, flipX,
  onNameChange, onAuthorChange, onDescriptionChange, onFlipXChange,
}) => {
  // Each caption IS its field's name, so it is BOUND to the input rather than
  // duplicated as a second translated string: `htmlFor` makes clicking the
  // caption focus the field, `aria-labelledby` names the field from that same
  // node. `useId` because both editors mount this header and the ids must not
  // collide with anything else on the page.
  const base = useId()
  const ids = {
    name: `${base}-name`, author: `${base}-author`, description: `${base}-desc`,
  }
  return (
    <div style={S.header}>
      <div style={S.title}>{title}</div>
      <div style={S.row}>
        <div style={S.group}>
          <label id={`${ids.name}-label`} htmlFor={ids.name} style={S.label}>{i18nT('apps.mochi.editor.name')}</label>
          <input id={ids.name} aria-labelledby={`${ids.name}-label`} style={S.input} value={name} onChange={e => onNameChange(e.target.value)} placeholder={i18nT('apps.mochi.editor.name_placeholder')} />
        </div>
        <div style={S.group}>
          <label id={`${ids.author}-label`} htmlFor={ids.author} style={S.label}>{i18nT('apps.mochi.editor.author')}</label>
          <input id={ids.author} aria-labelledby={`${ids.author}-label`} style={S.input} value={author} onChange={e => onAuthorChange(e.target.value)} placeholder={i18nT('apps.mochi.editor.author_placeholder')} />
        </div>
      </div>
      <div style={{ marginBottom: 6 }}>
        <label id={`${ids.description}-label`} htmlFor={ids.description} style={S.label}>{i18nT('apps.mochi.editor.description')}</label>
        <input id={ids.description} aria-labelledby={`${ids.description}-label`} style={S.input} value={description} onChange={e => onDescriptionChange(e.target.value)} placeholder={i18nT('apps.mochi.editor.desc_placeholder')} />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 2 }}>
        <span style={{ fontSize: 12, color: 'var(--text)' }}>{i18nT('apps.mochi.editor.flip_x')}</span>
        {/* A real switch, not a styled div: role + aria-checked + keyboard, otherwise
            the only way to flip the sprite is a mouse. */}
        <div
          role="switch"
          tabIndex={0}
          aria-checked={flipX}
          aria-label={i18nT('apps.mochi.editor.flip_x')}
          onClick={() => onFlipXChange(!flipX)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault()
              onFlipXChange(!flipX)
            }
          }}
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
}
