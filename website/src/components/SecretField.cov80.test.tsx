import { render, screen, fireEvent } from '@testing-library/react'
import { SecretField, type SecretFieldProps } from './SecretField'

function setup(over: Partial<SecretFieldProps> = {}) {
  const onChange = vi.fn()
  const onClearedChange = vi.fn()
  const props: SecretFieldProps = {
    label: 'zzq-label',
    isSet: false,
    preview: 'zzq-••••1234',
    value: '',
    onChange,
    cleared: false,
    onClearedChange,
    ...over,
  }
  return { onChange, onClearedChange, ...render(<SecretField {...props} />), props }
}

describe('SecretField', () => {
  it('unset: shows a masked paste input that can be revealed and hidden again', () => {
    setup({ placeholder: 'zzq-placeholder', description: 'zzq-desc' })
    expect(screen.getByText('zzq-desc')).toBeInTheDocument()

    const input = screen.getByLabelText('zzq-label')
    expect(input).toHaveAttribute('type', 'password')
    fireEvent.click(screen.getByRole('button', { name: /show/i }))
    expect(screen.getByLabelText('zzq-label')).toHaveAttribute('type', 'text')
    fireEvent.click(screen.getByRole('button', { name: /hide/i }))
    expect(screen.getByLabelText('zzq-label')).toHaveAttribute('type', 'password')
  })

  it('unset: typing reports upward and offers no cancel', () => {
    const { onChange } = setup()
    fireEvent.change(screen.getByLabelText('zzq-label'), { target: { value: 'zzq-typed' } })
    expect(onChange).toHaveBeenCalledWith('zzq-typed')
    expect(screen.queryByRole('button', { name: /cancel/i })).not.toBeInTheDocument()
  })

  it('renders the optional setup link with its own label', () => {
    setup({ setupLink: { href: 'https://zzq.invalid/x', label: 'zzq-where' } })
    const link = screen.getByLabelText('zzq-where')
    expect(link).toHaveAttribute('href', 'https://zzq.invalid/x')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('set: shows the masked preview with Replace and Remove, never the raw value', () => {
    const { onClearedChange } = setup({ isSet: true })
    expect(screen.getByText('zzq-••••1234')).toBeInTheDocument()
    expect(screen.queryByLabelText('zzq-label')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /remove/i }))
    expect(onClearedChange).toHaveBeenCalledWith(true)
  })

  it('set: Replace opens the input and Cancel closes it, both resetting the pending value', () => {
    const { onChange } = setup({ isSet: true })
    fireEvent.click(screen.getByRole('button', { name: /replace/i }))
    expect(onChange).toHaveBeenLastCalledWith('')
    expect(screen.getByLabelText('zzq-label')).toHaveAttribute('type', 'password')

    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onChange).toHaveBeenLastCalledWith('')
    expect(screen.getByText('zzq-••••1234')).toBeInTheDocument()
  })

  it('cleared: shows the pending-removal row with an undo', () => {
    const { onClearedChange } = setup({ isSet: true, cleared: true })
    expect(screen.getByText(/will be removed on save/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /undo remove/i }))
    expect(onClearedChange).toHaveBeenCalledWith(false)
  })

  it('readOnly: masked display with no actions, and "(not set)" when unset', () => {
    const { unmount } = setup({ readOnly: true, isSet: true })
    expect(screen.getByText('zzq-••••1234')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    unmount()

    setup({ readOnly: true, isSet: false })
    expect(screen.getByText('(not set)')).toBeInTheDocument()
  })
})
