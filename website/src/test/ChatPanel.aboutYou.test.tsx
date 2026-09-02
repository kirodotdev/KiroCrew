import { describe, it, expect, vi, beforeEach } from 'vitest'
// Radix Select renders a portalled listbox that jsdom cannot open; the repo's
// test double (used by SettingsSelect.test.tsx) makes the role picker driveable.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

/**
 * Settings → Chat → About You: the free-text role behind "Other".
 *
 * Its own file because it needs the Radix Select double to drive the role
 * picker, and ChatPanel.settings.test.tsx deliberately runs against the real
 * component.
 */

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({})),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }),
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    kirocrewConfig: kirocrewConfigMock,
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: patchConfigMock,
    updateDashboardConfig: () => Promise.resolve({}),
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

const BASE_CFG = {
  agent: { completion_keep: 'head', completion_keep_chars: 3000, model: 'auto', reasoning_effort: '' },
}

function seed(dashboard: Record<string, string>) {
  kirocrewConfigMock.mockImplementation(() => Promise.resolve({ ...BASE_CFG, dashboard }))
}

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('ChatPanel → About You custom role', () => {
  beforeEach(() => {
    patchConfigMock.mockClear()
    seed({ user_role: '', user_role_other: '', user_technical_level: '' })
  })

  it('hides the free-text field for a picked role', async () => {
    seed({ user_role: 'developer' })
    wrap(<ChatPanel />)
    expect(await screen.findByText('About You')).toBeInTheDocument()
    expect(screen.queryByLabelText('Describe your role')).not.toBeInTheDocument()
  })

  it('shows the field seeded from the server when the role is other', async () => {
    seed({ user_role: 'other', user_role_other: 'solutions architect' })
    wrap(<ChatPanel />)
    const input = (await screen.findByLabelText('Describe your role')) as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('solutions architect'))
  })

  it('PATCHes the trimmed value on blur, and not per keystroke', async () => {
    seed({ user_role: 'other', user_role_other: '' })
    wrap(<ChatPanel />)
    const input = (await screen.findByLabelText('Describe your role')) as HTMLInputElement
    fireEvent.change(input, { target: { value: '  SRE ' } })
    expect(patchConfigMock).not.toHaveBeenCalled()
    fireEvent.blur(input)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('dashboard.user_role_other', 'SRE'),
    )
  })

  it('does not PATCH on blur when the value is unchanged', async () => {
    seed({ user_role: 'other', user_role_other: 'founder' })
    wrap(<ChatPanel />)
    const input = (await screen.findByLabelText('Describe your role')) as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('founder'))
    fireEvent.blur(input)
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('leaves the custom role alone when the picker moves off other', async () => {
    seed({ user_role: 'other', user_role_other: 'solutions architect' })
    wrap(<ChatPanel />)
    await screen.findByLabelText('Describe your role')
    fireEvent.click(screen.getByRole('combobox', { name: 'Your Role' }))
    fireEvent.click(screen.getByRole('option', { name: /Developer/ }))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('dashboard.user_role', 'developer'),
    )
    // A second PATCH here could succeed while the role PATCH failed, leaving
    // `user_role=other` with its description deleted. The value is inert:
    // context.py reads it only while the role is 'other'.
    expect(patchConfigMock).toHaveBeenCalledTimes(1)
  })

  it('caps the typed role by code point, never splitting a surrogate pair', async () => {
    seed({ user_role: 'other', user_role_other: '' })
    wrap(<ChatPanel />)
    const input = (await screen.findByLabelText('Describe your role')) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'x'.repeat(59) + '😀' } })
    expect([...input.value]).toHaveLength(60)
    expect(input.value.endsWith('😀')).toBe(true)
    fireEvent.blur(input)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith(
        'dashboard.user_role_other',
        'x'.repeat(59) + '😀',
      ),
    )
  })
})
