// Shared styling constants + small primitives for the Spec Builder builtin.
// Accent is theme-driven (var(--accent)) so light/dark/custom palettes all
// work; translucent tints use color-mix so opacity reads correctly on any bg.
import type { CSSProperties, ReactNode } from 'react'
import { twMerge } from 'tailwind-merge'
import { Btn as HostBtn } from '../../../components/ui'

// Re-exported from inlineStyles so every importer keeps its `from './shared'`
// path while the CSS text itself lives in the module the i18n lint exempts.
export { ACCENT, SEL_BG, SEL_BORDER } from '../inlineStyles'

export const inputStyle: CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  padding: '12px 14px',
  borderRadius: '8px',
  border: '1px solid var(--border)',
  fontSize: '14px',
  background: 'var(--bg)',
  color: 'var(--text)',
  fontFamily: 'inherit',
  outline: 'none',
}

/** Framer Motion pulse for running dots + working indicators.
 *  Spread onto a `motion.*` element — replaces the old CSS @keyframes, which
 *  the frontend style guide disallows for new code. */
export const PULSE_MOTION = {
  animate: { opacity: [0.25, 1, 0.25] },
  transition: { duration: 1.2, repeat: Infinity, ease: 'easeInOut' as const },
}

/** Pill-shaped button — the host `Btn` with a rounded-full skin.
 *  Delegates to the shared component so theme tokens, focus ring, disabled
 *  state and active-press feedback stay identical to the rest of the
 *  dashboard; only the pill radius is app-specific. */
export function Btn({
  label, onClick, primary, danger, disabled, title, big, ariaLabel,
}: {
  label: ReactNode
  onClick?: () => void
  primary?: boolean
  danger?: boolean
  disabled?: boolean
  title?: string
  big?: boolean
  /** Required when `label` renders an icon with no adjacent text. */
  ariaLabel?: string
}) {
  return (
    <HostBtn
      onClick={onClick}
      disabled={disabled}
      title={title}
      primary={primary}
      danger={danger}
      aria-label={ariaLabel}
      className={twMerge('rounded-full', big && 'px-6 py-2 text-sm')}
    >
      {label}
    </HostBtn>
  )
}
