import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import React from 'react'

import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { i18nT } from '../i18n/t'

/**
 * Settings ▸ Chat — every search hit lands on the rail page that renders it.
 *
 * The extractor tags each Chat control with the `case '<key>':` it sits under
 * and emits that as `params.sub`; settingsCoverage.test.ts only checks the tag
 * names a page the rail HAS. A control tagged with the wrong valid page (say
 * `models` for a row rendered under `advanced`) passes that gate, and the deep
 * link then opens a pane where the highlight hook finds nothing. This mounts
 * each page and looks every entry up the way useSettingHighlight does.
 */

vi.mock('../api/client', () => ({
  api: {
    // restore_sessions on: the Restore window select renders only under it.
    dashboardConfig: () => Promise.resolve({
      restore_sessions: true,
      restore_window_minutes: 30,
      merge_queued_messages: false,
      default_memory_mode: 'persistent',
      widget_density: 'more',
      verbosity: 'default',
      quick_send: false,
      session_grid: false,
      tail_fork_enabled: false,
      link_previews: false,
      mcp_app_panel: false,
      auto_open_git_panel: false,
      session_card_source_links: true,
      folder_suggestions_enabled: true,
      model_picker_hidden_models: [],
      model_picker_configured: false,
      link_patterns: [],
    }),
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    // user_role 'other': the Describe your role input renders only for it.
    kirocrewConfig: () => Promise.resolve({
      agent: { completion_keep: 'head', completion_keep_chars: 3000, model: 'auto', reasoning_effort: '' },
      dashboard: { user_role: 'other', user_role_other: 'SRE', user_technical_level: 'expert' },
    }),
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: () => Promise.resolve({}),
    updateDashboardConfig: () => Promise.resolve({}),
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    // Tips enabled + a live video status: the rail drops Discovery otherwise.
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
    featureVideoStatus: () => Promise.resolve({
      enabled: true, download_enabled: true, release: 'r1', cached: 1, total: 1, downloading: null,
    }),
    featureVideoFetchAll: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'
import { createTestStore } from './helpers'

const CHAT_ENTRIES = SETTINGS_REGISTRY.filter(e => e.tab === 'chat')
const RAIL_KEYS = [...new Set(CHAT_ENTRIES.map(e => String(e.params?.sub)))].sort()

describe('ChatPanel — registry entries resolve on their tagged rail page', () => {
  it('has a page tag on every Chat entry', () => {
    expect(CHAT_ENTRIES.length).toBeGreaterThan(0)
    expect(CHAT_ENTRIES.filter(e => !e.params?.sub).map(e => e.id)).toEqual([])
  })

  it.each(RAIL_KEYS)('every entry tagged %s is on that page', async key => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter initialEntries={[`/settings/chat/${key}`]}>
        <Provider store={createTestStore()}>
          <QueryClientProvider client={qc}><ChatPanel basePath="/settings" /></QueryClientProvider>
        </Provider>
      </MemoryRouter>,
    )
    await waitFor(() => expect(document.querySelector('[role="option"][aria-selected="true"]')).not.toBeNull())

    const tagged = CHAT_ENTRIES.filter(e => e.params?.sub === key)
    expect(tagged.length).toBeGreaterThan(0)
    // Same lookup as useSettingHighlight: a declared settingId row, else the
    // Nth data-setting-label match for the label rendered in this locale.
    await waitFor(() => {
      const missing = tagged.filter(entry => {
        if (entry.settingId) {
          return document.querySelector(`[data-setting-id="${CSS.escape(entry.settingId)}"]`) === null
        }
        const label = entry.labelKey ? i18nT(entry.labelKey) : entry.label
        const matches = document.querySelectorAll(`[data-setting-label="${CSS.escape(label)}"]`)
        return (matches[entry.occurrence - 1] ?? null) === null
      }).map(e => e.id)
      expect(missing, `tagged '${key}' but not rendered on /settings/chat/${key}`).toEqual([])
    }, { timeout: 3000 })
  })
})
