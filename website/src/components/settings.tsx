import React from 'react'
import Clickable from './Clickable'
import InfoTip from './InfoTip'
import SimpleSelect from './SimpleSelect'
import { Input, Toggle } from './ui'

import { i18nT } from '../i18n/t'
/* ── Settings-specific UI primitives ──
 *
 * These match the pencil design system components:
 *   - SettingsToggle  → flat row: label+description left, toggle right
 *   - SettingsSelect  → vertical: label, description, dropdown
 *   - SettingsInput   → vertical: label, description, text/number input
 *   - SettingsSection → standalone section header above cards
 *
 * Layout rule: all settings within a card stack vertically (gap-3).
 * Section headers sit outside the card.
 */

/* ── Toggle ── */

interface SettingsToggleProps {
  label: string
  // ReactNode (not just string) so callers can pass rich copy — e.g. the
  // Telegram forum toggle describes setup with inline <span className="font-mono">
  // fragments. The render path already wraps it in a <div>, so any node is safe.
  description?: React.ReactNode
  checked: boolean
  onChange: (value: boolean) => void
  disabled?: boolean
}

export function SettingsToggle({ label, description, checked, onChange, disabled }: SettingsToggleProps) {
  return (
    <Clickable data-setting-label={label} className={`flex items-center justify-between py-1.5 group ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`} onClick={() => onChange(!checked)} disabled={disabled}>
      <div className="flex-1 min-w-0 mr-4">
        <div className="text-[13px] font-semibold text-text group-hover:text-text-strong transition-colors">{label}</div>
        {description && <div className="text-[12px] text-muted mt-0.5">{description}</div>}
      </div>
      {/* stopPropagation prevents the row's mouse-click convenience from double-
          toggling; the inner Toggle carries all keyboard/AT semantics. */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */}
      <div onClick={e => e.stopPropagation()}>
        <Toggle checked={checked} onChange={onChange} disabled={disabled} label={label} />
      </div>
    </Clickable>
  )
}


/* ── Select ── */

/** Shared field wrapper: label + optional hint + optional description */
function SettingsField({ label, description, hint, children }: { label: string; description?: string; hint?: string; children: React.ReactNode }) {
  return (
    <div data-setting-label={label} className="flex flex-col gap-1.5 py-1.5">
      <div className="flex items-center gap-1.5">
        <span className="text-[13px] font-semibold text-text">{label}</span>
        {hint && <InfoTip text={hint} />}
      </div>
      {description && <div className="text-[12px] text-muted">{description}</div>}
      {children}
    </div>
  )
}

interface SettingsSelectProps {
  label: string
  description?: string
  hint?: string
  value: string
  options: string[]
  /** Optional display labels for each option (same order as options). Falls back to the option value. */
  optionLabels?: string[]
  onChange: (value: string) => void
  /** Optional action at top of dropdown (e.g. "+ New workspace…") */
  action?: { label: string; onSelect: () => void }
  disabled?: boolean
}

export function SettingsSelect({ label, description, hint, value, options, optionLabels, onChange, action, disabled }: SettingsSelectProps) {
  return (
    <SettingsField label={label} description={description} hint={hint}>
      <SimpleSelect
        options={options}
        optionLabels={optionLabels}
        value={value}
        onChange={onChange}
        action={action}
        disabled={disabled}
        aria-label={label}
        triggerFallback={optionLabels?.[options.indexOf(value)] ?? (value || '—')}
      />
    </SettingsField>
  )
}

/* ── Input ── */

interface SettingsInputProps {
  label: string
  description?: string
  hint?: string
  value: string
  onChange: (value: string) => void
  onBlur?: () => void
  placeholder?: string
  type?: 'text' | 'number'
  min?: number
  max?: number
  step?: number
  disabled?: boolean
  multiline?: boolean
  'aria-label'?: string
}

export function SettingsInput({ label, description, hint, value, onChange, onBlur, placeholder, type = 'text', min, max, step, disabled, multiline, 'aria-label': ariaLabel }: SettingsInputProps) {
  return (
    <SettingsField label={label} description={description} hint={hint}>
      {multiline ? (
        <textarea
          value={value}
          onChange={e => onChange(e.target.value)}
          onBlur={onBlur}
          placeholder={placeholder}
          disabled={disabled}
          rows={3}
          aria-label={ariaLabel ?? label}
          className="w-full rounded border border-border bg-bg px-2 py-1 text-sm text-text focus:border-accent focus:outline-none resize-y flex-none"
        />
      ) : (
        <Input
          type={type}
          value={value}
          onChange={e => onChange(e.target.value)}
          onBlur={onBlur}
          placeholder={placeholder}
          min={min}
          max={max}
          step={step}
          disabled={disabled}
          aria-label={ariaLabel}
          className="flex-none"
        />
      )}
    </SettingsField>
  )
}

/* ── Section header (sits outside the Card) ── */

interface SettingsSectionProps {
  title: string
  /**
   * Optional node rendered inline after the title — a platform/status tag such
   * as the Computer Use panel's "macOS only" badge. Kept as a sibling of the
   * title text (not concatenated into it) so a `getByText(title)` query still
   * matches the header exactly.
   */
  badge?: React.ReactNode
  children?: React.ReactNode
}

export function SettingsSection({ title, badge, children }: SettingsSectionProps) {
  return (
    <>
      <div className="flex items-center gap-2 mt-4 mb-2">
        <h4 className="text-sm font-semibold text-text-strong">{title}</h4>
        {badge}
      </div>
      {children}
    </>
  )
}

/* ── Settings Card (thin wrapper around Card with vertical gap) ── */

export function SettingsCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="card-glow border border-border bg-card rounded-lg p-5 mb-4 animate-rise shadow-sm transition-all">
      <div className="flex flex-col gap-1">
        {children}
      </div>
    </div>
  )
}

/* ── Stepper (numeric value with −/+ buttons) ── */

interface SettingsStepperProps {
  label: string
  description?: string
  hint?: string
  /**
   * The displayed value. `string` is allowed for an ALREADY-FORMATTED, localised
   * value — a duration rendered as "0.5 seconds", for instance, where the unit
   * word is part of the catalog string and cannot be split off into `suffix`
   * (locales place and inflect it differently). The stepper only interpolates
   * this, so the numeric state stays with the caller either way.
   */
  value: number | string
  onIncrement: () => void
  onDecrement: () => void
  onReset?: () => void
  suffix?: string
  disabled?: boolean
}

export function SettingsStepper({ label, description, hint, value, onIncrement, onDecrement, onReset, suffix = '', disabled }: SettingsStepperProps) {
  return (
    <SettingsField label={label} description={description} hint={hint}>
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={disabled}
          className="w-8 h-8 rounded-md border border-border bg-bg-elevated text-text text-sm font-bold cursor-pointer hover:border-border-strong hover:bg-bg-hover transition-all disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center"
          onClick={onDecrement}
          aria-label={i18nT('components.settings.decrease')}
        >−</button>
        <button
          type="button"
          disabled={!onReset || disabled}
          className={`min-w-[56px] h-8 rounded-md border border-border bg-bg-elevated text-text-strong text-sm font-bold flex items-center justify-center px-2 transition-all ${
            onReset ? 'cursor-pointer hover:border-accent hover:text-accent' : 'cursor-default'
          } disabled:opacity-40 disabled:cursor-not-allowed`}
          onClick={onReset}
          title={onReset ? i18nT('components.settings.click_to_reset') : undefined}
        >{value}{suffix}</button>
        <button
          type="button"
          disabled={disabled}
          className="w-8 h-8 rounded-md border border-border bg-bg-elevated text-text text-sm font-bold cursor-pointer hover:border-border-strong hover:bg-bg-hover transition-all disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center"
          onClick={onIncrement}
          aria-label={i18nT('components.settings.increase')}
        >+</button>
      </div>
    </SettingsField>
  )
}

/* ── Button Group (mutually exclusive options) ── */

interface SettingsButtonGroupProps {
  label: string
  description?: string
  hint?: string
  value: string
  options: { value: string; label: string; icon?: React.ReactNode }[]
  onChange: (value: string) => void
  disabled?: boolean
}

export function SettingsButtonGroup({ label, description, hint, value, options, onChange, disabled }: SettingsButtonGroupProps) {
  return (
    <SettingsField label={label} description={description} hint={hint}>
      {/* Segmented control: a RECESSED track (`bg-accent`) holding a RAISED
          selected thumb (`bg-elevated` + border + shadow).

          The track must not be `bg-elevated`: in every light theme
          `--bg-elevated` and `--card` are both #ffffff (index.css), so a
          `bg-elevated` track is invisible against the card it sits on. Only
          the selected pill rendered, reading as one stray grey box rather
          than as a three-way choice. `bg-accent` is a step DARKER than the
          card in light themes and darker than `bg-elevated` in dark ones, so
          the track is visible in both directions.

          Selection is conveyed by elevation + weight, not by hue alone, so it
          survives a theme whose accent is low-contrast — and `aria-pressed`
          carries it to screen readers, which no amount of styling does. */}
      <div role="group" aria-label={label} className="inline-flex items-center gap-0.5 p-[3px] rounded-lg border border-border bg-bg-accent w-fit">
        {options.map(o => (
          <button
            key={o.value}
            type="button"
            disabled={disabled}
            aria-pressed={value === o.value}
            className={`flex items-center gap-1.5 px-3 py-[5px] rounded-md text-[13px] cursor-pointer border transition-colors ${
              value === o.value
                ? 'bg-bg-elevated text-text-strong border-border-strong shadow-sm font-semibold'
                : 'bg-transparent text-muted border-transparent font-medium hover:text-text-strong'
            } disabled:opacity-40 disabled:cursor-not-allowed`}
            onClick={() => !disabled && onChange(o.value)}
          >
            {o.icon}
            {o.label}
          </button>
        ))}
      </div>
    </SettingsField>
  )
}
