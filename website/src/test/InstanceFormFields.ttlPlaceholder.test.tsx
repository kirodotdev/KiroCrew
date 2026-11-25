/**
 * The Token TTL placeholder is a value the field accepts, in every language (#36386).
 *
 * German showed `20 Std.` and Italian `20 h`, and the field refuses both: it takes
 * only `<number>h` or `<number>m`. Typing exactly what the placeholder shows must
 * not raise the field's own error.
 */
import { afterAll, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// `/all` registers every catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'
import { SUPPORTED_CODES } from '../i18n/languages'
import { i18nT } from '../i18n/t'
import {
  EMPTY_INSTANCE_FORM,
  InstanceFormFields,
  useInstanceFormState,
} from '../pages/settings/InstanceFormFields'

vi.mock('../hooks/useGatewayPlatform', () => ({ useGatewayPlatform: () => 'other' }))
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => false }))

function Harness() {
  const form = useInstanceFormState(EMPTY_INSTANCE_FORM)
  return <InstanceFormFields idPrefix="t" form={form} />
}

function renderTtl(): HTMLInputElement {
  const { container } = render(
    <QueryClientProvider client={new QueryClient()}>
      <Harness />
    </QueryClientProvider>,
  )
  return container.querySelector<HTMLInputElement>('#t-ttl')!
}

const ttlError = () => screen.queryByText(i18nT('pages.settings.remoteCrewPanel.ttl_must_be_hours_or_minutes'))

afterAll(async () => { await i18next.changeLanguage('en') })

describe('Token TTL placeholder', () => {
  it('shows the field error for a value the field refuses', async () => {
    await i18next.changeLanguage('de')
    fireEvent.change(renderTtl(), { target: { value: '20 Std.' } })
    expect(ttlError()).not.toBeNull()
  })

  for (const code of SUPPORTED_CODES) {
    it(`is accepted when typed as shown (${code})`, async () => {
      await i18next.changeLanguage(code)
      const ttl = renderTtl()
      fireEvent.change(ttl, { target: { value: ttl.placeholder } })
      expect(ttl.value).toBe(ttl.placeholder)
      expect(ttlError()).toBeNull()
    })
  }
})
