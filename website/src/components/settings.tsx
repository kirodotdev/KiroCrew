import React from 'react'
import Clickable from './Clickable'
import InfoTip from './InfoTip'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from './ui/select'
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

/**
 * Radix Select rejects empty-string item values (it reserves '' for
 * "no selection"), but some callers legitimately offer '' as an option
 * (e.g. the microphone picker's "System default"). Map '' to a sentinel
 * on the way in and back on the way out so the public API is unchanged.
 */
const EMPTY_VALUE_SENTINEL = '\u0000settings-select-empty'
/** Sentinel for the action row: selecting it fires action.onSelect instead of onChange. */
const ACTION_SENTINEL = '\u0000settings-select-action'

export function SettingsSelect({ label, description, hint, value, options, optionLabels, onChange, action, disabled }: SettingsSelectProps) {
  const toRadix = (v: string) => (v === '' ? EMPTY_VALUE_SENTINEL : v)
  const fromRadix = (v: string) => (v === EMPTY_VALUE_SENTINEL ? '' : v)
  return (
    <SettingsField label={label} description={description} hint={hint}>
      <Select
        value={options.includes(value) ? toRadix(value) : ''}
        onValueChange={v => {
          if (v === ACTION_SENTINEL) { action?.onSelect(); return }
          onChange(fromRadix(v))
        }}
        disabled={disabled}
      >
        <SelectTrigger aria-label={label}>
          {/* Radix's SelectValue renders the selected SelectItem's text; the
              placeholder covers values not present in options (legacy configs). */}
          <SelectValue placeholder={optionLabels?.[options.indexOf(value)] ?? (value || '—')} />
        </SelectTrigger>
        <SelectContent>
          {action && (
            <SelectItem value={ACTION_SENTINEL} className="text-accent data-[state=checked]:bg-transparent">
              {action.label}
            </SelectItem>
          )}
          {options.map((opt, i) => (
            <SelectItem key={opt} value={toRadix(opt)}>
              {optionLabels?.[i] ?? opt}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
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
  children?: React.ReactNode
}

export function SettingsSection({ title, children }: SettingsSectionProps) {
  return (
    <>
      <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2">{title}</h4>
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
  value: number
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
          title={onReset ? 'Click to reset' : undefined}
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
      <div className="inline-flex items-center gap-1 p-1 rounded-md bg-bg-elevated w-fit">
        {options.map(o => (
          <button
            key={o.value}
            type="button"
            disabled={disabled}
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded text-[13px] font-medium cursor-pointer border-none transition-colors ${
              value === o.value
                ? 'bg-bg-hover text-accent'
                : 'bg-transparent text-muted hover:text-text'
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
