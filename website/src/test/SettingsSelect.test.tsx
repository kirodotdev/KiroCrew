import { describe, it, expect, vi } from 'vitest'
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))
import { render, screen, fireEvent } from '@testing-library/react'
import { SettingsSelect } from '../components/settings'

/**
 * SettingsSelect after the Radix Select migration (from StyledSelect).
 * Pins the public-API contract every settings panel depends on:
 * options/optionLabels mapping, empty-string option support (mic picker),
 * the action row, and disabled behavior.
 */
describe('SettingsSelect (Radix Select)', () => {
  const base = {
    label: 'Sound',
    options: ['silent', 'ping', 'glass'],
    optionLabels: ['Silent', 'Ping', 'Glass'],
  }

  it('renders the selected option label in the trigger', () => {
    render(<SettingsSelect {...base} value="glass" onChange={() => {}} />)
    expect(screen.getByRole('combobox', { name: 'Sound' })).toHaveTextContent('Glass')
  })

  it('opens on click and lists all option labels', () => {
    render(<SettingsSelect {...base} value="ping" onChange={() => {}} />)
    fireEvent.click(screen.getByRole('combobox', { name: 'Sound' }))
    const opts = screen.getAllByRole('option')
    expect(opts.map(o => o.textContent)).toEqual(['Silent', 'Ping', 'Glass'])
  })

  it('fires onChange with the option VALUE (not label) and closes', () => {
    const onChange = vi.fn()
    render(<SettingsSelect {...base} value="ping" onChange={onChange} />)
    fireEvent.click(screen.getByRole('combobox', { name: 'Sound' }))
    fireEvent.click(screen.getByRole('option', { name: /Glass/ }))
    expect(onChange).toHaveBeenCalledWith('glass')
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('marks the selected item checked with the themed accent state', () => {
    render(<SettingsSelect {...base} value="ping" onChange={() => {}} />)
    fireEvent.click(screen.getByRole('combobox', { name: 'Sound' }))
    const selected = screen.getByRole('option', { name: /Ping/ })
    expect(selected).toHaveAttribute('data-state', 'checked')
    expect(selected).toHaveAttribute('aria-selected', 'true')
  })

  it('supports an empty-string option (mic "System default") end to end', () => {
    const onChange = vi.fn()
    render(
      <SettingsSelect
        label="Microphone"
        value=""
        options={['', 'dev-1']}
        optionLabels={['System default', 'Microphone 1']}
        onChange={onChange}
      />
    )
    // Trigger shows the label mapped to the empty value
    expect(screen.getByRole('combobox', { name: 'Microphone' })).toHaveTextContent('System default')
    fireEvent.click(screen.getByRole('combobox', { name: 'Microphone' }))
    fireEvent.click(screen.getByRole('option', { name: /Microphone 1/ }))
    expect(onChange).toHaveBeenCalledWith('dev-1')
    // And selecting back to the empty option yields '' (not the sentinel)
    fireEvent.click(screen.getByRole('combobox', { name: 'Microphone' }))
    fireEvent.click(screen.getByRole('option', { name: /System default/ }))
    expect(onChange).toHaveBeenLastCalledWith('')
  })

  it('action row fires action.onSelect instead of onChange', () => {
    const onChange = vi.fn()
    const onSelect = vi.fn()
    render(
      <SettingsSelect
        {...base}
        value="ping"
        onChange={onChange}
        action={{ label: '+ New workspace…', onSelect }}
      />
    )
    fireEvent.click(screen.getByRole('combobox', { name: 'Sound' }))
    fireEvent.click(screen.getByRole('option', { name: /New workspace/ }))
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('does not open when disabled', () => {
    render(<SettingsSelect {...base} value="ping" onChange={() => {}} disabled />)
    fireEvent.click(screen.getByRole('combobox', { name: 'Sound' }))
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('falls back to raw value text when value is not in options (legacy configs)', () => {
    render(<SettingsSelect {...base} value="bloom" onChange={() => {}} />)
    expect(screen.getByRole('combobox', { name: 'Sound' })).toHaveTextContent('bloom')
  })
})
