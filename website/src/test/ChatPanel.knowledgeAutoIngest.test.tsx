/**
 * The three Knowledge auto-ingest toggles are OPT-IN, so a config that has never
 * mentioned them must render them OFF.
 *
 * The fallback in the panel (`?? false`) is a second copy of a default the backend
 * already owns (`KnowledgeConfig.auto_*`). When the two disagree the switch reads
 * ON while the gateway ingests nothing -- or worse, reads OFF while it ingests --
 * and the user cannot tell which is true, because the config file has no key to
 * inspect. This pins the UI side of that pair; `TestKnowledgeAutoIngest` in
 * `test/test_config_loader.py` pins the backend side.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  // No `knowledge` section at all -- the shape a fresh install actually has.
  kirocrewConfigMock: vi.fn(() => Promise.resolve({
    agent: { completion_keep: 'head', completion_keep_chars: 3000, model: 'auto', reasoning_effort: '' },
  })),
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

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const TOGGLES: ReadonlyArray<readonly [string, string]> = [
  ['Auto-Add Documents', 'knowledge.auto_add_documents'],
  ['Auto-Register Project Documents', 'knowledge.auto_register_project_docs'],
  ['Auto-Add Saved Artifacts', 'knowledge.auto_ingest_artifacts'],
]

/**
 * Waits until the switch is interactive. Until the config query settles the row
 * renders `disabled`, so asserting before that would pass on a loading
 * placeholder rather than on the fallback under test.
 */
async function settledSwitch(label: string) {
  const sw = await screen.findByRole('switch', { name: label })
  await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
  return sw
}

describe('ChatPanel – Knowledge auto-ingest is opt-in', () => {
  it.each(TOGGLES)('renders %s off when the config never set it', async label => {
    wrap(<ChatPanel />)
    expect(await settledSwitch(label)).toHaveAttribute('aria-checked', 'false')
  })

  it.each(TOGGLES)('%s writes true on the first click', async (label, key) => {
    patchConfigMock.mockClear()
    wrap(<ChatPanel />)
    fireEvent.click(await settledSwitch(label))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(key, true))
  })
})
