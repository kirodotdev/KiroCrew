/**
 * Settings ▸ Chat ▸ Advanced, Context card: the idle compaction switch
 * (`session.idle_compaction`) and its minimum context field
 * (`session.idle_compaction_min_context_pct`).
 *
 * The claims: both rows show the stored value; the switch saves the new value
 * through the config API; the switch row says what it saves, what each summary
 * costs measured in messages, and when that pays; the minimum row says which
 * chats it lets through; the field is disabled and dimmed while the switch is off,
 * and its row then says why; a typed minimum saves on blur or Enter, and a value
 * outside 40-90 shows the value the server clamped it to (the write path clamps
 * through the loader's own coercer and returns a clamp notice) with a notice that
 * opens with the saved value, in neutral text because the save worked, while the
 * panel's other field notices keep the warn colour; a failed save keeps the typed
 * value; Settings search finds both rows and deep-links to their config keys.
 */

// SettingsSelect wraps Radix Select, whose portalled listbox jsdom cannot open.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, type NavigateFunction } from 'react-router-dom'
import { createTestStore } from './helpers'

const { kirocrewConfigMock, patchConfigMock } = vi.hoisted(() => ({
  kirocrewConfigMock: vi.fn(),
  patchConfigMock: vi.fn(() => Promise.resolve({})),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({
      restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false,
      default_memory_mode: 'persistent', widget_density: 'more', verbosity: 'default',
      quick_send: false, session_grid: false, tail_fork_enabled: false, link_previews: false,
      mcp_app_panel: false, folder_suggestions_enabled: true,
    }),
    updateDashboardConfig: () => Promise.resolve({}),
    kirocrewConfig: kirocrewConfigMock,
    kirocrewAgents: () => Promise.resolve({ agents: [], default_agent: 'default' }),
    agentResolvedModel: () => Promise.resolve({ model: '', pinned: false }),
    patchConfig: patchConfigMock,
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
    featureVideoStatus: () => Promise.resolve({
      enabled: true, download_enabled: false, release: 'r1', cached: 0, total: 0, downloading: null,
    }),
    featureVideoFetchAll: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'
import { idleCompactionPct } from '../pages/settings/useIdleCompactionFields'
import { i18next } from '../i18n/all'
import { i18nT } from '../i18n/t'
import { createSettingsProvider } from '../components/commandPalette/providers/settingsProvider'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'

const SWITCH = 'Summarize Idle Chats to Save Credits'
const FIELD = 'Minimum Context to Summarize (%)'
const MIN_PATH = 'session.idle_compaction_min_context_pct'
const LOCKED = 'Turn on Summarize Idle Chats to Save Credits to change this.'
const RANGE = /^Only idle chats whose context is at least this full are summarized\./

/** The element that holds the minimum row, which dims it while the switch is off. */
function minRowHolder(input: HTMLElement) {
  return input.closest(`[data-setting-key="${MIN_PATH}"]`)!.parentElement!
}

function seedSession(session: Record<string, unknown>) {
  kirocrewConfigMock.mockImplementation(() => Promise.resolve({
    session: { autocompact_pct: 70, ...session },
    agent: { model: 'auto', reasoning_effort: '', completion_keep: 'head', completion_keep_chars: 3000, soft_stop_budget_secs: 10 },
    dashboard: { user_role: '', user_role_other: '', user_technical_level: '', prevent_sleep: false },
    knowledge: { embed_rate_limit: 120 },
  }) as never)
}

let lastQc: QueryClient

function tree(qc: QueryClient) {
  return (
    <MemoryRouter initialEntries={['/settings?tab=chat&sub=advanced']}>
      <Provider store={createTestStore()}>
        <QueryClientProvider client={qc}>
          <ChatPanel />
        </QueryClientProvider>
      </Provider>
    </MemoryRouter>
  )
}

/** Renders the panel. Pass the previous client to remount on the same cache. */
function wrap(reuse?: QueryClient) {
  const qc = reuse ?? new QueryClient({ defaultOptions: { queries: { retry: false } } })
  lastQc = qc
  return render(tree(qc))
}

async function settledSwitch() {
  const sw = await screen.findByRole('switch', { name: SWITCH })
  await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
  return sw
}

async function field() {
  return (await screen.findByRole('spinbutton', { name: FIELD })) as HTMLInputElement
}

/** The field once the config has loaded and the switch is on. */
async function enabledField(value: number) {
  const input = await field()
  await waitFor(() => expect(input).not.toBeDisabled())
  await waitFor(() => expect(input).toHaveValue(value))
  return input
}

beforeEach(() => {
  localStorage.clear()
  kirocrewConfigMock.mockReset()
  patchConfigMock.mockReset()
  patchConfigMock.mockImplementation(() => Promise.resolve({}) as never)
  seedSession({})
})

describe('ChatPanel — idle compaction switch', () => {
  it('is off by default and keeps the minimum field disabled and dimmed, with the reason on its row', async () => {
    wrap()
    const sw = await settledSwitch()
    expect(sw).toHaveAttribute('aria-checked', 'false')
    const input = await field()
    await waitFor(() => expect(input).toHaveValue(50))
    expect(input).toBeDisabled()
    const row = minRowHolder(input)
    expect(row.className).toContain('opacity-40')
    // The locked reason replaces the range line while the switch is off.
    expect(row).toHaveTextContent(LOCKED)
    expect(screen.queryByText(RANGE)).not.toBeInTheDocument()
  })

  it('shows a saved on value and enables the minimum field without dimming it', async () => {
    seedSession({ idle_compaction: true, idle_compaction_min_context_pct: 65 })
    wrap()
    const sw = await settledSwitch()
    expect(sw).toHaveAttribute('aria-checked', 'true')
    const input = await enabledField(65)
    const row = minRowHolder(input)
    expect(row.className).not.toContain('opacity-40')
    // The range line is back and the locked reason is gone. It says which idle
    // chats are summarized, which is what tells it apart from Auto-Compact
    // Threshold on the same card.
    expect(screen.getByText(RANGE)).toHaveTextContent(
      /^Only idle chats whose context is at least this full are summarized\. 40% to 90%\. Default 50%\.$/,
    )
    expect(row).not.toHaveTextContent(LOCKED)
  })

  it('saves the new value through the config API and holds it while the save runs', async () => {
    let settle: (v: unknown) => void = () => {}
    patchConfigMock.mockImplementationOnce(() => new Promise(r => { settle = r }) as never)
    wrap()
    const sw = await settledSwitch()
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('session.idle_compaction', true))
    expect(sw).toHaveAttribute('aria-checked', 'true')
    seedSession({ idle_compaction: true })
    settle({})
    await enabledField(50)
    expect(sw).toHaveAttribute('aria-checked', 'true')
  })

  it('surfaces a failed save and shows the stored value again', async () => {
    patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')))
    wrap()
    const sw = await settledSwitch()
    fireEvent.click(sw)
    expect(await screen.findByText(/Failed to save idle compaction$/)).toBeInTheDocument()
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
  })

  it('stays on after a save that worked when the refetch that follows fails', async () => {
    wrap()
    const sw = await settledSwitch()
    kirocrewConfigMock.mockImplementation(() => Promise.reject(new Error('refetch down')))
    fireEvent.click(sw)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('session.idle_compaction', true))
    // The save has settled and the refetch after it has failed, so the switch
    // reads only the cache.
    await waitFor(() => expect(lastQc.isMutating()).toBe(0))
    await waitFor(() => expect(lastQc.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    expect(sw).toHaveAttribute('aria-checked', 'true')
  })

  it('scrolls the save-error banner into view when a save fails', async () => {
    const scroll = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scroll
    try {
      patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')))
      wrap()
      fireEvent.click(await settledSwitch())
      await screen.findByText(/Failed to save idle compaction$/)
      await waitFor(() => expect(scroll).toHaveBeenCalled())
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })

  it('says on the row what it saves, what each summary costs in messages, and when that pays', async () => {
    wrap()
    await settledSwitch()
    const line = screen.getByText(/Each summary can cost about as much as one or two messages in that chat/)
    expect(line).toBeInTheDocument()
    // One visible line: the benefit, then the cost sized against a message in
    // the same chat, then when it pays, so no tooltip holds the deciding fact.
    expect(line).toHaveTextContent(
      /^Summarizes a large chat that has gone idle, so your next message in it costs less\. Each summary can cost about as much as one or two messages in that chat, even if you never come back\. Worth it if you often return to long chats\.$/,
    )
  })
})

describe('ChatPanel — idle compaction minimum context', () => {
  beforeEach(() => seedSession({ idle_compaction: true, idle_compaction_min_context_pct: 50 }))

  it('saves an in-range value on blur and shows no notice', async () => {
    wrap()
    const input = await enabledField(50)
    fireEvent.change(input, { target: { value: '62.5' } })
    fireEvent.blur(input)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(MIN_PATH, 62.5))
    expect(screen.queryByText(/The allowed range is/)).not.toBeInTheDocument()
  })

  it('saves on Enter', async () => {
    wrap()
    const input = await enabledField(50)
    fireEvent.change(input, { target: { value: '80' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(MIN_PATH, 80))
  })

  it.each([
    [20, 40, 'raised'],
    [95, 90, 'lowered'],
  ])('shows the value the server clamped %d to, with a notice', async (typed, stored, verb) => {
    patchConfigMock.mockImplementationOnce(() => Promise.resolve({
      clamp_notice: { path: MIN_PATH, requested: typed, stored, min: 40, max: 90 },
    }) as never)
    wrap()
    const input = await enabledField(50)
    fireEvent.change(input, { target: { value: String(typed) } })
    fireEvent.blur(input)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(MIN_PATH, typed))
    // The notice opens with the saved value, then gives the range and says
    // which way the typed value moved.
    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent(
      `Saved ${stored}%. The allowed range is 40% to 90%, so ${typed}% was ${verb} to ${stored}%.`,
    )
    // The save worked, so the notice is secondary text, not a warning.
    expect(notice).toHaveClass('text-muted')
    expect(notice).not.toHaveClass('text-warn')
    expect(input).toHaveValue(stored)
  })

  it('leaves the compaction time limit notice on the same card in the warn colour', async () => {
    seedSession({ idle_compaction: true, compact_wait_secs: 0 })
    patchConfigMock.mockImplementationOnce(() => Promise.resolve({
      clamp_notice: { path: 'session.compact_wait_secs', requested: 20, stored: 60, min: 60, max: 3600 },
    }) as never)
    wrap()
    const input = (await screen.findByRole('spinbutton', { name: 'Compaction Time Limit (seconds)' })) as HTMLInputElement
    await waitFor(() => expect(input).toHaveValue(0))
    fireEvent.change(input, { target: { value: '20' } })
    fireEvent.blur(input)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('session.compact_wait_secs', 20))
    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent(/outside the allowed range/)
    expect(notice).toHaveClass('text-warn')
    expect(notice).not.toHaveClass('text-muted')
  })

  it('sends nothing for an empty field and shows the stored value again', async () => {
    wrap()
    const input = await enabledField(50)
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.blur(input)
    await waitFor(() => expect(input).toHaveValue(50))
    expect(patchConfigMock).not.toHaveBeenCalledWith(MIN_PATH, expect.anything())
  })

  it.each([
    { typed: 62.5, reply: {}, stored: 62.5 },
    { typed: 20, reply: { clamp_notice: { requested: 20, stored: 40, min: 40, max: 90 } }, stored: 40 },
  ])('keeps the stored minimum $stored in the config cache when the refetch after a save of $typed fails', async ({ typed, reply, stored }) => {
    seedSession({ idle_compaction: true })
    patchConfigMock.mockImplementationOnce(() => Promise.resolve(reply) as never)
    wrap()
    const input = await enabledField(50)
    kirocrewConfigMock.mockImplementation(() => Promise.reject(new Error('refetch down')))
    fireEvent.change(input, { target: { value: String(typed) } })
    fireEvent.blur(input)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(MIN_PATH, typed))
    await waitFor(() => expect(lastQc.isMutating()).toBe(0))
    await waitFor(() => expect(lastQc.getQueryState(['kirocrewConfig'])?.status).toBe('error'))
    const cached = lastQc.getQueryData<{ session?: Record<string, unknown> }>(['kirocrewConfig'])
    expect(cached?.session?.idle_compaction_min_context_pct).toBe(stored)
  })

  it('sends a revert to the cached value while an earlier save from a previous mount is pending', async () => {
    seedSession({ idle_compaction: true })
    let settleFirst: (v: unknown) => void = () => {}
    patchConfigMock.mockImplementationOnce(() => new Promise(r => { settleFirst = r }) as never)
    const first = wrap()
    const before = await enabledField(50)
    fireEvent.change(before, { target: { value: '70' } })
    fireEvent.blur(before)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(MIN_PATH, 70))
    first.unmount()
    wrap(lastQc)
    // The remounted field reads the cache, which still holds 50.
    const after = await enabledField(50)
    fireEvent.change(after, { target: { value: '51' } })
    fireEvent.change(after, { target: { value: '50' } })
    fireEvent.keyDown(after, { key: 'Enter' })
    settleFirst({})
    await waitFor(() => expect(patchConfigMock).toHaveBeenLastCalledWith(MIN_PATH, 50))
    expect(patchConfigMock).toHaveBeenCalledTimes(2)
  })

  it('drops the clamp notice when another writer saves a different minimum', async () => {
    seedSession({ idle_compaction: true })
    patchConfigMock.mockImplementationOnce(() => Promise.resolve({
      clamp_notice: { requested: 20, stored: 40, min: 40, max: 90 },
    }) as never)
    wrap()
    const input = await enabledField(50)
    fireEvent.change(input, { target: { value: '20' } })
    fireEvent.blur(input)
    expect(await screen.findByText(/so 20% was raised to 40%/)).toBeInTheDocument()
    // Another tab or the CLI then stores 60.
    seedSession({ idle_compaction: true, idle_compaction_min_context_pct: 60 })
    await lastQc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    await waitFor(() => expect(input).toHaveValue(60))
    expect(screen.queryByText(/so 20% was raised to 40%/)).not.toBeInTheDocument()
  })

  it('sends minimum saves in the order the user made them, also across a remount', async () => {
    seedSession({ idle_compaction: true })
    let settleFirst: (v: unknown) => void = () => {}
    patchConfigMock.mockImplementationOnce(() => new Promise(r => { settleFirst = r }) as never)
    const first = wrap()
    const before = await enabledField(50)
    fireEvent.change(before, { target: { value: '62.5' } })
    fireEvent.blur(before)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(MIN_PATH, 62.5))
    first.unmount()
    wrap(lastQc)
    const after = await enabledField(50)
    fireEvent.change(after, { target: { value: '70' } })
    fireEvent.blur(after)
    // Let react-query start whatever it will start for the newer save.
    await act(async () => {
      for (let i = 0; i < 5; i++) await Promise.resolve()
    })
    // The newer save waits for the older one, so it cannot be overwritten by it.
    expect(patchConfigMock).toHaveBeenCalledTimes(1)
    settleFirst({})
    await waitFor(() => expect(patchConfigMock).toHaveBeenLastCalledWith(MIN_PATH, 70))
    expect(patchConfigMock).toHaveBeenCalledTimes(2)
  })

  it('shows the clamp notice in the new language after a language change', async () => {
    seedSession({ idle_compaction: true })
    patchConfigMock.mockImplementationOnce(() => Promise.resolve({
      clamp_notice: { requested: 20, stored: 40, min: 40, max: 90 },
    }) as never)
    const view = wrap()
    const input = await enabledField(50)
    fireEvent.change(input, { target: { value: '20' } })
    fireEvent.blur(input)
    const english = i18nT('pages.settings.chatPanel.idle_compaction_min_context_raised', {
      requested: idleCompactionPct(20), stored: idleCompactionPct(40), min: idleCompactionPct(40), max: idleCompactionPct(90),
    })
    expect(await screen.findByText(english)).toBeInTheDocument()
    await act(async () => { await i18next.changeLanguage('de') })
    try {
      view.rerender(tree(lastQc))
      const german = i18nT('pages.settings.chatPanel.idle_compaction_min_context_raised', {
        requested: idleCompactionPct(20), stored: idleCompactionPct(40), min: idleCompactionPct(40), max: idleCompactionPct(90),
      })
      expect(german).not.toBe(english)
      // German puts a no-break space before %, which the default text matcher
      // collapses on the page but not in the expected string.
      expect(await screen.findByText((_, el) => el?.textContent === german)).toBeInTheDocument()
      expect(screen.queryByText(english)).not.toBeInTheDocument()
    } finally {
      await act(async () => { await i18next.changeLanguage('en') })
    }
  })

  it('does not write the cache when a minimum save settles after the panel unmounts', async () => {
    seedSession({ idle_compaction: true })
    let settle: (v: unknown) => void = () => {}
    patchConfigMock.mockImplementationOnce(() => new Promise(r => { settle = r }) as never)
    const view = wrap()
    const input = await enabledField(50)
    fireEvent.change(input, { target: { value: '62.5' } })
    fireEvent.blur(input)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(MIN_PATH, 62.5))
    view.unmount()
    // A remounted panel saved 70 in the meantime.
    const cached = lastQc.getQueryData<{ session?: Record<string, unknown> }>(['kirocrewConfig'])
    lastQc.setQueryData(['kirocrewConfig'], { ...cached, session: { ...cached?.session, idle_compaction_min_context_pct: 70 } })
    settle({})
    await waitFor(() => expect(lastQc.isMutating()).toBe(0))
    const after = lastQc.getQueryData<{ session?: Record<string, unknown> }>(['kirocrewConfig'])
    expect(after?.session?.idle_compaction_min_context_pct).toBe(70)
  })

  it('scrolls the save-error banner into view when a save of the minimum fails', async () => {
    const scroll = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scroll
    try {
      seedSession({ idle_compaction: true })
      patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')))
      wrap()
      const input = await enabledField(50)
      fireEvent.change(input, { target: { value: '70' } })
      fireEvent.blur(input)
      await screen.findByText(/Failed to save idle compaction minimum context$/)
      await waitFor(() => expect(scroll).toHaveBeenCalled())
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })

  it('refetches the config after a failed save, because the server can have stored it', async () => {
    // The PATCH stores 20 as 40, then its response is lost.
    patchConfigMock.mockImplementationOnce(() => {
      seedSession({ idle_compaction: true, idle_compaction_min_context_pct: 40 })
      return Promise.reject(new Error('response lost'))
    })
    wrap()
    const input = await enabledField(50)
    fireEvent.change(input, { target: { value: '20' } })
    fireEvent.blur(input)
    await screen.findByText(/Failed to save idle compaction minimum context/)
    await waitFor(() => {
      const after = lastQc.getQueryData<{ session?: Record<string, unknown> }>(['kirocrewConfig'])
      expect(after?.session?.idle_compaction_min_context_pct).toBe(40)
    })
    expect(input).toHaveValue(20)
  })

  it('clears the save error on the next blur when the failed save was stored anyway', async () => {
    // The PATCH stores 70, then its response is lost; the refetch reads 70.
    patchConfigMock.mockImplementationOnce(() => {
      seedSession({ idle_compaction: true, idle_compaction_min_context_pct: 70 })
      return Promise.reject(new Error('response lost'))
    })
    wrap()
    const input = await enabledField(50)
    fireEvent.change(input, { target: { value: '70' } })
    fireEvent.blur(input)
    await screen.findByText(/Failed to save idle compaction minimum context/)
    await waitFor(() => {
      const after = lastQc.getQueryData<{ session?: Record<string, unknown> }>(['kirocrewConfig'])
      expect(after?.session?.idle_compaction_min_context_pct).toBe(70)
    })
    patchConfigMock.mockClear()
    fireEvent.focus(input)
    fireEvent.blur(input)
    await waitFor(() => expect(screen.queryByText(/Failed to save idle compaction minimum context/)).toBeNull())
    expect(patchConfigMock).not.toHaveBeenCalled()
    expect(input).toHaveValue(70)
  })

  it('keeps the typed value after a failed save and retries it on the next blur', async () => {
    patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')))
    wrap()
    const input = await enabledField(50)
    fireEvent.change(input, { target: { value: '70' } })
    fireEvent.blur(input)
    expect(await screen.findByText(/Failed to save idle compaction minimum context/)).toBeInTheDocument()
    await waitFor(() => expect(input).not.toBeDisabled())
    expect(input).toHaveValue(70)
    patchConfigMock.mockClear()
    fireEvent.focus(input)
    fireEvent.blur(input)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(MIN_PATH, 70))
  })
})

describe('Settings search — idle compaction rows', () => {
  it('lists both rows on Chat ▸ Advanced under their config keys', () => {
    for (const key of ['session.idle_compaction', MIN_PATH]) {
      const entry = SETTINGS_REGISTRY.find(e => e.configKey === key)
      expect(entry, key).toBeDefined()
      expect(entry!.tab).toBe('chat')
      expect(entry!.params).toEqual({ sub: 'advanced' })
    }
  })

  it('finds both rows and opens Chat ▸ Advanced with the row highlighted', () => {
    const spy = vi.fn()
    const p = createSettingsProvider(spy as unknown as NavigateFunction, {
      decisionsEnabled: true, tipsEnabled: true, updateSwitches: { app: true, gateway: true },
    })
    const results = p.search('idle compaction')
    for (const [title, key] of [[SWITCH, 'session.idle_compaction'], [FIELD, MIN_PATH]]) {
      const hit = results.find(r => r.title === title)
      expect(hit, title).toBeDefined()
      spy.mockClear()
      hit!.onActivate()
      const entry = SETTINGS_REGISTRY.find(e => e.configKey === key)!
      expect(spy).toHaveBeenCalledWith(`/settings/chat/advanced?highlight=${entry.id}`)
    }
  })
})
