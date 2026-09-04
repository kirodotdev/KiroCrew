/**
 * NumberField — Shared number input with string state to avoid leading zeros,
 * clamp on blur, optional label.
 */
import React, { useId, useState, useEffect } from 'react'

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
  background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6,
  padding: '4px 8px', color: 'var(--text)', fontSize: 12, outline: 'none',
}

export const NumberField: React.FC<Props> = ({ label, value, min, max, onChange, width = 60, style }) => {
  const [text, setText] = useState(String(value))
  // The caption IS this input's name, so it is bound rather than duplicated as a
  // second translated string: `htmlFor` makes clicking the caption focus the
  // input, `aria-labelledby` names the input from that same node. `useId` because
  // several NumberFields sit in one row and a shared id would cross the wires.
  // Both are conditional on there being a caption — an aria-labelledby pointing
  // at an element that was never rendered leaves the input with NO name at all.
  const fieldId = useId()
  const labelId = `${fieldId}-label`
  useEffect(() => { setText(String(value)) }, [value])
  return (
    <div style={style}>
      {label && <label id={labelId} htmlFor={fieldId} style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 2, display: 'block' }}>{label}</label>}
      <input id={fieldId} aria-labelledby={label ? labelId : undefined} type="number" value={text} min={min} max={max}
        onChange={e => { setText(e.target.value); const n = Number(e.target.value); if (!isNaN(n)) onChange(n) }}
        onBlur={() => { const c = Math.max(min ?? -Infinity, Math.min(max ?? Infinity, value)); onChange(c); setText(String(c)) }}
        style={{ ...inputStyle, width }} />
    </div>
  )
}
