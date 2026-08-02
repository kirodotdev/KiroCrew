/**
 * A labelled on/off switch. The control is a real <button role="switch"> carrying
 * aria-checked, so keyboard toggling and screen-reader state work; the visual track
 * and knob are painted from the aria-checked state in CSS (styles.ts).
 */
export default function ToggleRow({ label, hint, on, onChange, disabled }: {
  label: string
  hint: string
  on: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
}) {
  return (
    <div className="cc-toggle">
      <span className="cc-toggle-text">
        <span className="cc-toggle-label">{label}</span>
        <span className="cc-toggle-hint">{hint}</span>
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={label}
        disabled={disabled}
        className="cc-switch"
        onClick={() => { if (!disabled) onChange(!on) }}
      >
        <span className="cc-switch-track" aria-hidden />
        <span className="cc-switch-knob" aria-hidden />
      </button>
    </div>
  )
}
