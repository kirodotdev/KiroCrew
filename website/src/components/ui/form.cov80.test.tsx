import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'

import { Form, FormControl, FormDescription, FormField, FormItem, FormLabel, FormMessage, useFormField } from './form'

const schema = z.object({ name: z.string().min(1, 'zzq-required') })

function NameForm() {
  const form = useForm<{ name: string }>({ resolver: zodResolver(schema), defaultValues: { name: '' }, mode: 'onChange' })
  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(() => {})}>
        <FormField
          control={form.control}
          name="name"
          render={({ field }) => (
            <FormItem>
              <FormLabel>zzq-name</FormLabel>
              <FormControl>
                <input {...field} />
              </FormControl>
              <FormDescription>zzq-description</FormDescription>
              <FormMessage>zzq-fallback</FormMessage>
            </FormItem>
          )}
        />
        <button type="submit">zzq-submit</button>
      </form>
    </Form>
  )
}

function Orphan() {
  useFormField()
  return null
}

describe('shadcn Form', () => {
  it('wires the label, description and message to the control', async () => {
    const user = userEvent.setup()
    render(<NameForm />)
    const input = screen.getByLabelText('zzq-name')
    const description = screen.getByText('zzq-description')
    expect(input.getAttribute('aria-describedby')).toBe(description.id)
    expect(input.getAttribute('aria-invalid')).toBe('false')
    // With no error the message shows its own children.
    expect(screen.getByText('zzq-fallback')).toBeTruthy()

    await user.click(screen.getByRole('button', { name: 'zzq-submit' }))
    const message = await screen.findByText('zzq-required')
    await waitFor(() => expect(input.getAttribute('aria-invalid')).toBe('true'))
    expect(input.getAttribute('aria-describedby')).toBe(`${description.id} ${message.id}`)
    expect(screen.getByText('zzq-name').className).toContain('text-danger')
  })

  it('refuses to read a field outside FormField', () => {
    function Host() {
      const form = useForm()
      return (
        <Form {...form}>
          <Orphan />
        </Form>
      )
    }
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      expect(() => render(<Host />)).toThrow('useFormField should be used within <FormField>')
    } finally {
      error.mockRestore()
    }
  })
})
