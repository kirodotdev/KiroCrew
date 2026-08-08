/**
 * NumberField — Shared number input with string state to avoid leading zeros,
 * clamp on blur, optional label.
 */
import React, { useState, useEffect } from 'react'

interface Props {
  label?: string
  value: number
  min?: number
  max?: number
  onChange: (v: number) => void
  width?: number
  style?: React.CSSProperties
}

const inputStyle: React.CSSProperties = {
  background: 'var(--cc-input-bg)', border: '1px solid var(--border)', borderRadius: 6,
  padding: '4px 8px', color: 'var(--text)', fontSize: 12, outline: 'none',
}

export const NumberField: React.FC<Props> = ({ label, value, min, max, onChange, width = 60, style }) => {
  const [text, setText] = useState(String(value))
  useEffect(() => { setText(String(value)) }, [value])
  return (
    <div style={style}>
      {label && <span style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 2, display: 'block' }}>{label}</span>}
      <input type="number" value={text} min={min} max={max}
        onChange={e => { setText(e.target.value); const n = Number(e.target.value); if (!isNaN(n)) onChange(n) }}
        onBlur={() => { const c = Math.max(min ?? -Infinity, Math.min(max ?? Infinity, value)); onChange(c); setText(String(c)) }}
        style={{ ...inputStyle, width }} />
    </div>
  )
}
