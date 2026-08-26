/** Small shared pieces for the Notes app. */
import type { CSSProperties, ReactNode } from 'react'
import { GithubIcon } from '../../components/BrandIcon'
import { ACCENT, ACCENT_FG } from './constants'

/** Shared link treatment: an underlined accent button with no chrome. */
const linkStyle: CSSProperties = {
  padding: 0,
  border: 'none',
  background: 'transparent',
  color: ACCENT,
  font: 'inherit',
  textDecoration: 'underline',
  textUnderlineOffset: '2px',
  cursor: 'pointer',
}

export { GithubIcon }

/**
 * Underlined text link that runs an action rather than navigating.
 *
 * A `<button>`, not an `<a>`: there is no href — the click asks the backend to
 * reveal a folder — and an anchor without one is unreachable by keyboard. The
 * underline is what makes it read as a link at this size, where a bare accent
 * colour is easy to miss.
 */
export function TextLink({
  label,
  onClick,
  disabled,
  title,
}: {
  label: string
  onClick: () => void
  disabled?: boolean
  title?: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        ...linkStyle,
        fontSize: '11.5px',
        cursor: disabled ? 'default' : 'pointer',
        opacity: disabled ? 0.6 : 1,
      }}
    >
      {label}
    </button>
  )
}

/**
 * The same link, sized to sit INSIDE a sentence — `font: inherit` with no size
 * of its own, so it matches the surrounding prose rather than the app's default.
 *
 * Takes children rather than a label because it is rendered by `<Trans>`, which
 * clones the component around the translated text between its tags. That is the
 * only way to put a link mid-sentence without splitting the sentence across two
 * catalog keys — a split reads fine in English and breaks in every language whose
 * word order differs.
 */
export function InlineLink({
  children,
  onClick,
  title,
}: {
  children?: ReactNode
  onClick: () => void
  title?: string
}) {
  return (
    <button type="button" onClick={onClick} title={title} style={linkStyle}>
      {children}
    </button>
  )
}

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
