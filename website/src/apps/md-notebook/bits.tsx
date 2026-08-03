/** Small shared pieces for the Notes app. */
import { GithubIcon } from '../../components/BrandIcon'
import { ACCENT, ACCENT_FG } from './constants'

export { GithubIcon }

/** Track-and-knob switch, shared by the sync and knowledge toggles. */
export function Switch({
  on,
  onChange,
  label,
  disabled,
}: {
  on: boolean
  onChange: (next: boolean) => void
  label: string
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={Boolean(on)}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!on)}
      style={{
        flexShrink: 0,
        width: '34px',
        height: '20px',
        borderRadius: '9999px',
        border: 'none',
        padding: '2px',
        cursor: disabled ? 'default' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        background: on ? ACCENT : 'var(--border)',
        display: 'flex',
        justifyContent: on ? 'flex-end' : 'flex-start',
        alignItems: 'center',
        transition: 'background .15s',
      }}
    >
      <span
        style={{
          width: '16px',
          height: '16px',
          borderRadius: '9999px',
          background: on ? ACCENT_FG : 'var(--bg-elevated)',
          display: 'block',
        }}
      />
    </button>
  )
}
