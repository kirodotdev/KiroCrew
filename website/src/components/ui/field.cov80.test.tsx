import { render, screen } from '@testing-library/react'

import {
  Field,
  FieldContent,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSeparator,
  FieldSet,
  FieldTitle,
} from './field'
import { FieldRow, FieldRows } from './field-row'
import { Separator } from './separator'

describe('shadcn Field', () => {
  it('lays a labelled control out in each orientation', () => {
    const { container } = render(
      <FieldSet>
        <FieldLegend variant="label">zzq-legend</FieldLegend>
        <FieldGroup>
          <Field orientation="vertical">
            <FieldLabel htmlFor="zzq-input">zzq-label</FieldLabel>
            <input id="zzq-input" aria-label="zzq-label" />
            <FieldDescription>zzq-hint</FieldDescription>
          </Field>
          <Field orientation="horizontal">
            <FieldTitle>zzq-title</FieldTitle>
            <FieldContent>zzq-content</FieldContent>
          </Field>
          <Field orientation="responsive" data-invalid>
            <FieldTitle>zzq-responsive</FieldTitle>
          </Field>
        </FieldGroup>
      </FieldSet>,
    )
    expect(screen.getByText('zzq-label').getAttribute('for')).toBe('zzq-input')
    expect(screen.getByText('zzq-legend').getAttribute('data-variant')).toBe('label')
    const orientations = Array.from(container.querySelectorAll('[data-slot="field"]')).map(el => el.getAttribute('data-orientation'))
    expect(orientations).toEqual(['vertical', 'horizontal', 'responsive'])
    // The fieldset and each Field are groups.
    expect(screen.getAllByRole('group')).toHaveLength(4)
    expect(screen.getByText('zzq-hint').getAttribute('data-slot')).toBe('field-description')
  })

  it('draws a separator, with a caption only when it has one', () => {
    const { container } = render(
      <>
        <FieldSeparator />
        <FieldSeparator>zzq-or</FieldSeparator>
        <Separator orientation="vertical" data-testid="zzq-vertical" />
      </>,
    )
    const separators = container.querySelectorAll('[data-slot="field-separator"]')
    expect(separators[0].getAttribute('data-content')).toBe('false')
    expect(separators[1].getAttribute('data-content')).toBe('true')
    expect(screen.getByText('zzq-or').getAttribute('data-slot')).toBe('field-separator-content')
    expect(screen.getByTestId('zzq-vertical').className).toContain('w-px')
  })

  it('shows its children, one error, a deduplicated list, or nothing', () => {
    const { rerender } = render(<FieldError>zzq-child</FieldError>)
    expect(screen.getByRole('alert').textContent).toBe('zzq-child')

    rerender(<FieldError errors={[{ message: 'zzq-one' }]} />)
    expect(screen.getByRole('alert').textContent).toBe('zzq-one')

    rerender(<FieldError errors={[{ message: 'zzq-a' }, { message: 'zzq-a' }, { message: 'zzq-b' }]} />)
    expect(screen.getAllByRole('listitem').map(li => li.textContent)).toEqual(['zzq-a', 'zzq-b'])

    rerender(<FieldError errors={[]} />)
    expect(screen.queryByRole('alert')).toBeNull()
    rerender(<FieldError />)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('renders read-only rows with an optional label, the code face and actions', () => {
    render(
      <FieldRows>
        <FieldRow label="zzq-row" mono actions={<span>zzq-action</span>}>
          zzq-value
        </FieldRow>
        <FieldRow>zzq-continued</FieldRow>
      </FieldRows>,
    )
    expect(screen.getByText('zzq-row')).toBeTruthy()
    expect(screen.getByText('zzq-value').className).toContain('font-mono')
    expect(screen.getByText('zzq-action')).toBeTruthy()
    expect(screen.getByText('zzq-continued').className).not.toContain('font-mono')
  })
})
