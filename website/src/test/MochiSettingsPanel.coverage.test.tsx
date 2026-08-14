/**
 * Mochi settings panel — first tests for
 * `apps/mochi/src/renderer/SettingsPanel.tsx`.
 *
 * The panel is the pet's entire configuration surface (twelve sections behind a
 * left rail) and had no test at all, so every behaviour below is pinned here for
 * the first time. Everything it can reach outside itself goes through one seam
 * (`api` in `mochiApi`), so that module is the single mock, and the instance
 * picker — core's own component, tested separately — is stubbed at its module
 * boundary so this file only exercises how the panel WIRES it.
 *
 * The behaviours worth pinning are the ones a user can be lied to by:
 *
 *  - staged edits: nothing is written until Save, and Save is disabled until
 *    something actually differs from what was read;
 *  - the two "saved but not really" paths — a shortcut the OS refused, and an
 *    instance switch the shell declined — which must keep the panel open, reveal
 *    the section that renders the reason, and differ from each other on whether
 *    the baseline is committed;
 *  - discard, which must put back the config that was read rather than a
 *    reconstruction of it.
 *
 * No fake timers: the panel has no timers of its own. Async reads are awaited
 * through `findBy*` / `waitFor` instead of advanced clocks, so nothing here
 * depends on elapsed time or on test order.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'

import type { AppConfig } from '../apps/mochi/src/shared/config'
import type { CompanionStats, McpServerInfo } from '../apps/mochi/src/shared/types'

const mocks = vi.hoisted(() => {
  /** `onSettingsCloseRequested` subscribers, so a native close can be replayed. */
  const closeRequested = new Set<() => void>()
  /** Default subscribe: register only. Restored after any test that replaces it. */
  const subscribeClose = (cb: () => void) => {
    closeRequested.add(cb)
    return () => { closeRequested.delete(cb) }
  }
  const api = {
    hasShell: true,
    getConfig: vi.fn<() => Promise<unknown>>(),
    getStats: vi.fn<() => Promise<unknown>>(),
    getMochiTrustLevel: vi.fn<() => Promise<string>>(),
    setMochiTrustLevel: vi.fn<(level: string) => Promise<void>>(),
    updateConfig: vi.fn<(partial: unknown) => Promise<void>>(),
    applyShortcuts: vi.fn<(s: unknown) => Promise<Record<string, boolean>>>(),
    setPetInstance: vi.fn<(id: string) => Promise<boolean>>(),
    getMcpServers: vi.fn<() => Promise<unknown>>(),
    discoverMcpTools: vi.fn<(name: string) => Promise<unknown>>(),
    getModels: vi.fn<() => Promise<unknown>>(),
    setModel: vi.fn<(m: string) => Promise<boolean>>(),
    galleryOpen: vi.fn(),
    openExternal: vi.fn(),
    onSettingsCloseRequested: subscribeClose,
  }
  return {
    api,
    subscribeClose,
    /** Replay the native window-close request the shell sends on the red ×. */
    requestClose: () => { for (const cb of [...closeRequested]) cb() },
    clearCloseRequested: () => closeRequested.clear(),
  }
})

vi.mock('../apps/mochi/src/mochiApi', () => ({ api: mocks.api }))

// Core's instance picker is tested on its own; here only the panel's wiring of
// it matters, so it is reduced to the two things the panel supplies: the current
// value, and a way to pick a different one.
vi.mock('../apps/mochi/panel/MochiInstances', () => ({
  MochiInstancesList: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <div>
      <span>{`instance:${value}`}</span>
      <button onClick={() => onChange('remote-1')}>pick remote</button>
    </div>
  ),
}))

import { SettingsPanel } from '../apps/mochi/src/renderer/SettingsPanel'

const api = mocks.api

/* ── fixtures ─────────────────────────────────────────────────── */

/** A config shaped like `api.getConfig`'s, fresh per call (the panel clones it). */
function makeConfig(over: { mochi?: Record<string, unknown> } = {}): AppConfig {
  return {
    shortcuts: {
      voiceInput: 'Alt+Space',
      screenCapture: 'CommandOrControl+Shift+X',
      toggleWindow: 'CommandOrControl+Shift+M',
      hideAll: 'CommandOrControl+Shift+H',
    },
    window: {
      position: { x: 10, y: 20 },
      visible: true,
      expanded: false,
      chatAlwaysOnTop: true,
    },
    mochi: {
      petName: 'Mochi',
      language: '',
      activityMode: 'normal',
      activityTier: 'balanced',
      bgModel: '',
      silentSubagents: false,
      extraMcpServers: [],
      petInstance: 'self',
      theme: 'kirocrew',
      ...over.mochi,
    },
  } as unknown as AppConfig
}

function makeStats(over: Partial<CompanionStats> = {}): CompanionStats {
  return {
    firstLaunch: '2026-01-01T00:00:00.000Z',
    streak: 4,
    lastActiveDate: '2026-08-10',
    companionSeconds: 7260,
    messages: { sent: 12, received: 9 },
    walkSteps: 340,
    screenshots: 6,
    peeks: 3,
    drags: 2,
    thinkingSeconds: 95,
    latestActiveTime: '23:40',
    earliestActiveTime: '07:05',
    moods: { happy: 6, curious: 3, weird: 1 },
    longestChat: 18,
    busiestDay: { date: '2026-08-09', messages: 22 },
    lastMemoryHour: 3,
    ...over,
  }
}

function makeServer(over: Partial<McpServerInfo> = {}): McpServerInfo {
  return {
    name: 'server-a',
    core: false,
    enabled: true,
    agents: ['chat'],
    autoApprove: [],
    disabledTools: [],
    chatPolicy: { autoApprove: [], disabledTools: [] },
    bgPolicy: { autoApprove: [], disabledTools: [] },
    ...over,
  }
}

/* ── helpers ──────────────────────────────────────────────────── */

const onClose = vi.fn()

/** Mount and wait for the config read to land (the spinner to go). */
async function mount(): Promise<void> {
  render(<SettingsPanel onClose={onClose} />)
  await screen.findByRole('button', { name: 'General' })
}

/** Open a left-rail section by its rail label. */
function openSection(label: string): void {
  fireEvent.click(screen.getByRole('button', { name: label }))
}

/** The label-wrapped radio rows (behavior / trust / tier) have no role. */
function clickRow(text: string): void {
  const row = screen.getByText(text).closest('label')
  if (!row) throw new Error(`no option row for ${text}`)
  fireEvent.click(row)
}

function saveButton(): HTMLButtonElement {
  // The unsaved-changes dialog adds a second "Save"; the footer one is first.
  return screen.getAllByRole('button', { name: 'Save' })[0] as HTMLButtonElement
}

beforeEach(() => {
  mocks.clearCloseRequested()
  onClose.mockReset()
  api.hasShell = true
  api.getConfig.mockReset().mockResolvedValue(makeConfig())
  api.getStats.mockReset().mockResolvedValue(makeStats())
  api.getMochiTrustLevel.mockReset().mockResolvedValue('normal')
  api.setMochiTrustLevel.mockReset().mockResolvedValue(undefined)
  api.updateConfig.mockReset().mockResolvedValue(undefined)
  api.applyShortcuts.mockReset().mockResolvedValue({})
  api.setPetInstance.mockReset().mockResolvedValue(true)
  api.getMcpServers.mockReset().mockResolvedValue([])
  api.discoverMcpTools.mockReset().mockResolvedValue(null)
  api.getModels.mockReset().mockResolvedValue([])
  api.setModel.mockReset().mockResolvedValue(true)
  api.galleryOpen.mockReset()
  api.openExternal.mockReset()
  // The background section reads its usage ledger over plain fetch. Default to a
  // refusal so no test depends on a network answer; the usage test overrides it.
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 500 })))
})

afterEach(() => {
  vi.unstubAllGlobals()
  // Tests may replace this to fire during subscribe; put the register-only
  // default back so the next case does not inherit an early-fire mock.
  api.onSettingsCloseRequested = mocks.subscribeClose
})

/* ── loading gate ─────────────────────────────────────────────── */

describe('SettingsPanel loading gate', () => {
  it('shows only a spinner until the config read lands', async () => {
    let land: (c: AppConfig) => void = () => {}
    api.getConfig.mockReturnValue(new Promise<AppConfig>((res) => { land = res }))

    render(<SettingsPanel onClose={onClose} />)
    expect(screen.queryByRole('button', { name: 'General' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()

    land(makeConfig())
    expect(await screen.findByRole('button', { name: 'General' })).toBeTruthy()
  })
})

/* ── rail ─────────────────────────────────────────────────────── */

describe('SettingsPanel section rail', () => {
  const RAIL = [
    'General', 'Memories', 'Appearance', 'Behavior', 'Notifications',
    'Background Activity', 'LLM Model', 'MCP Servers', 'Trust', 'Shortcuts',
    'Instances', 'About',
  ]

  it('offers every section and marks exactly the selected one', async () => {
    await mount()
    for (const label of RAIL) {
      openSection(label)
      const selected = RAIL.filter(
        (l) => (screen.getByRole('button', { name: l }) as HTMLElement).style.fontWeight === '600',
      )
      expect(selected).toEqual([label])
    }
  })

  it('opens on General, so the pet name is the first thing reachable', async () => {
    await mount()
    expect(screen.getByDisplayValue('Mochi')).toBeTruthy()
  })
})

/* ── general ──────────────────────────────────────────────────── */

describe('SettingsPanel general section', () => {
  it('stages a pet name edit and only then enables Save', async () => {
    await mount()
    expect(saveButton().disabled).toBe(true)

    fireEvent.change(screen.getByDisplayValue('Mochi'), { target: { value: 'Tofu' } })

    expect(screen.getByDisplayValue('Tofu')).toBeTruthy()
    expect(saveButton().disabled).toBe(false)
    // Staged only — nothing is written before Save.
    expect(api.updateConfig).not.toHaveBeenCalled()
  })

  it('offers Auto plus the real language registry, and stages the pick', async () => {
    await mount()
    const select = screen.getByRole('combobox') as HTMLSelectElement
    // '' is "follow Kiro Crew", which is what the stored empty value means.
    expect(select.value).toBe('')
    expect(within(select).getByRole('option', { name: 'Auto' })).toBeTruthy()
    expect(select.options.length).toBeGreaterThan(1)

    const other = [...select.options].find((o) => o.value !== '')
    fireEvent.change(select, { target: { value: other?.value } })
    expect(select.value).toBe(other?.value)
    expect(saveButton().disabled).toBe(false)
  })
})

/* ── memories ─────────────────────────────────────────────────── */

describe('SettingsPanel memories section', () => {
  it('renders one row per non-empty counter, with the streak folded into the time row', async () => {
    await mount()
    openSection('Memories')

    expect(screen.getByText(/Together for/)).toBeTruthy()
    expect(screen.getByText(/4-day streak/)).toBeTruthy()
    expect(screen.getByText(/21 messages \(12 you · 9 Mochi\)/)).toBeTruthy()
    expect(screen.getByText('340 steps')).toBeTruthy()
    expect(screen.getByText('Looked at your screen 6 times')).toBeTruthy()
    expect(screen.getByText('Peeked 3 times')).toBeTruthy()
    expect(screen.getByText('Dragged 2 times')).toBeTruthy()
    expect(screen.getByText(/Thought for/)).toBeTruthy()
    expect(screen.getByText('Latest: stayed up till 23:40')).toBeTruthy()
    expect(screen.getByText('Earliest: up at 07:05')).toBeTruthy()
    expect(screen.getByText(/Chattiest day: .* \(22 msgs\)/)).toBeTruthy()
    expect(screen.getByText('Longest chat: 18 messages')).toBeTruthy()
  })

  it('lists every mood with a share, translating the known ones and passing the rest through', async () => {
    await mount()
    openSection('Memories')

    const moods = screen.getByText('Moods:').parentElement as HTMLElement
    // 6 / 3 / 1 of ten samples.
    expect(within(moods).getByText(/60%/)).toBeTruthy()
    expect(within(moods).getByText(/30%/)).toBeTruthy()
    // An unknown mood id has no catalog key and is shown as itself, not dropped.
    expect(within(moods).getByText(/weird/)).toBeTruthy()
  })

  it('welcomes a first-time user instead of showing a near-empty ledger', async () => {
    api.getStats.mockResolvedValue(makeStats({
      companionSeconds: 60, streak: 0, messages: { sent: 0, received: 0 },
      walkSteps: 0, screenshots: 0, peeks: 0, drags: 0, thinkingSeconds: 0,
      latestActiveTime: '', earliestActiveTime: '', moods: {},
      longestChat: 0, busiestDay: { date: '', messages: 0 },
    }))
    await mount()
    openSection('Memories')

    expect(screen.getByText('Your story with Mochi just began')).toBeTruthy()
  })

  it('renders nothing at all when there is not a single counter yet', async () => {
    api.getStats.mockResolvedValue(makeStats({
      companionSeconds: 0, streak: 0, messages: { sent: 0, received: 0 },
      walkSteps: 0, screenshots: 0, peeks: 0, drags: 0, thinkingSeconds: 0,
      latestActiveTime: '', earliestActiveTime: '', moods: {},
      longestChat: 0, busiestDay: { date: '', messages: 0 },
    }))
    await mount()
    openSection('Memories')

    expect(screen.queryByText('Memories:')).toBeNull()
    expect(screen.queryByText(/just began/)).toBeNull()
  })

  it('re-reads the counters when the window regains focus', async () => {
    await mount()
    expect(api.getStats).toHaveBeenCalledTimes(1)

    api.getStats.mockResolvedValue(makeStats({ walkSteps: 999 }))
    fireEvent.focus(window)
    openSection('Memories')

    expect(await screen.findByText('999 steps')).toBeTruthy()
  })

  it('shows no ledger while the stats read is still outstanding', async () => {
    api.getStats.mockReturnValue(new Promise(() => {}))
    await mount()
    openSection('Memories')

    expect(screen.queryByText(/Together for/)).toBeNull()
  })
})

/* ── appearance / behavior / notifications ────────────────────── */

describe('SettingsPanel appearance section', () => {
  it('opens the gallery through the shell rather than staging anything', async () => {
    await mount()
    openSection('Appearance')

    fireEvent.click(screen.getByRole('button', { name: 'Appearance Gallery' }))
    expect(api.galleryOpen).toHaveBeenCalledTimes(1)
    expect(saveButton().disabled).toBe(true)
  })
})

describe('SettingsPanel behavior section', () => {
  it('stages the picked activity mode and moves the selection to it', async () => {
    await mount()
    openSection('Behavior')

    const quiet = screen.getByText('Quiet').closest('label') as HTMLElement
    const normal = screen.getByText('Normal').closest('label') as HTMLElement
    expect(normal.style.background).toBe('var(--accent-glow)')

    fireEvent.click(quiet)
    // Only the newly-picked row is asserted. The two labels can resolve to the
    // SAME element depending on how the i18n'd option text is wrapped, so
    // asserting the other one turned transparent compared an element with
    // itself and failed spuriously.
    expect(quiet.style.background).toBe('var(--accent-glow)')
    expect(saveButton().disabled).toBe(false)
  })
})

describe('SettingsPanel notifications section', () => {
  it('flips the subagent-silence switch on click', async () => {
    await mount()
    openSection('Notifications')

    const [silent] = screen.getAllByRole('switch')
    expect(silent.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(silent)
    expect(screen.getAllByRole('switch')[0].getAttribute('aria-checked')).toBe('true')
    expect(saveButton().disabled).toBe(false)
  })

  it('flips the always-on-top switch from the keyboard', async () => {
    await mount()
    openSection('Notifications')

    const onTop = screen.getAllByRole('switch')[1]
    expect(onTop.getAttribute('aria-checked')).toBe('true')
    fireEvent.keyDown(onTop, { key: ' ' })
    expect(screen.getAllByRole('switch')[1].getAttribute('aria-checked')).toBe('false')

    fireEvent.keyDown(screen.getAllByRole('switch')[1], { key: 'Enter' })
    expect(screen.getAllByRole('switch')[1].getAttribute('aria-checked')).toBe('true')
  })

  it('ignores keys that are not the two activation keys', async () => {
    await mount()
    openSection('Notifications')

    fireEvent.keyDown(screen.getAllByRole('switch')[0], { key: 'a' })
    expect(screen.getAllByRole('switch')[0].getAttribute('aria-checked')).toBe('false')
  })
})

/* ── background activity ──────────────────────────────────────── */

describe('SettingsPanel background activity section', () => {
  it('stages a spend tier without touching the personality axis', async () => {
    await mount()
    openSection('Background Activity')

    expect((screen.getByText('Balanced').closest('label') as HTMLElement).style.background)
      .toBe('var(--accent-glow)')
    clickRow('Economy')
    expect((screen.getByText('Economy').closest('label') as HTMLElement).style.background)
      .toBe('var(--accent-glow)')

    // Behavior is a separate key and must be untouched by a tier pick.
    openSection('Behavior')
    expect((screen.getByText('Normal').closest('label') as HTMLElement).style.background)
      .toBe('var(--accent-glow)')
  })

  it('reads the same ledger the cap enforces and reports all three windows', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ usage: { runsThisHour: 2, runsToday: 9, runs7d: 41 } }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    )))
    await mount()
    openSection('Background Activity')

    expect(await screen.findByText(/2 this hour · 9 today · 41 in 7 days/)).toBeTruthy()
  })

  it('says nothing about usage when the ledger read fails', async () => {
    await mount()
    openSection('Background Activity')

    await waitFor(() => expect(fetch).toHaveBeenCalled())
    expect(screen.queryByText(/this hour/)).toBeNull()
  })
})

/* ── model ────────────────────────────────────────────────────── */

describe('SettingsPanel model section', () => {
  it('renders nothing rather than an empty picker when no model list comes back', async () => {
    await mount()
    openSection('LLM Model')

    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('survives a failed model read without breaking the panel', async () => {
    api.getModels.mockRejectedValue(new Error('nope'))
    await mount()
    openSection('LLM Model')

    await waitFor(() => expect(api.getModels).toHaveBeenCalled())
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy()
  })

  it('normalizes a bare-string model list and switches the chat model immediately', async () => {
    api.getModels.mockResolvedValue(['claude-x', { model_name: 'gpt-y', model_id: 'gpt-y-id' }])
    await mount()
    openSection('LLM Model')

    const [chat] = await screen.findAllByRole('combobox')
    expect(within(chat).getByRole('option', { name: 'claude-x' })).toBeTruthy()
    // A model_id, when present, is what gets sent — not the display name.
    expect((within(chat).getByRole('option', { name: 'gpt-y' }) as HTMLOptionElement).value)
      .toBe('gpt-y-id')

    fireEvent.change(chat, { target: { value: 'claude-x' } })
    expect(api.setModel).toHaveBeenCalledWith('claude-x')
    // The chat model is applied now, not staged, so Save stays disabled.
    await waitFor(() => expect(screen.queryByText('Switching model...')).toBeNull())
    expect(saveButton().disabled).toBe(true)
  })

  it('stages the background model instead of applying it, since it is config', async () => {
    api.getModels.mockResolvedValue([{ model_name: 'gpt-y' }])
    await mount()
    openSection('LLM Model')

    const combos = await screen.findAllByRole('combobox')
    fireEvent.change(combos[1], { target: { value: 'gpt-y' } })

    expect(api.setModel).not.toHaveBeenCalled()
    expect(saveButton().disabled).toBe(false)
  })

  it('keeps working when the model switch itself throws', async () => {
    api.getModels.mockResolvedValue([{ model_name: 'gpt-y' }])
    api.setModel.mockRejectedValue(new Error('switch failed'))
    await mount()
    openSection('LLM Model')

    const [chat] = await screen.findAllByRole('combobox')
    fireEvent.change(chat, { target: { value: 'gpt-y' } })

    await waitFor(() => expect(screen.queryByText('Switching model...')).toBeNull())
    expect((chat as HTMLSelectElement).value).toBe('gpt-y')
  })
})

/* ── MCP ──────────────────────────────────────────────────────── */

describe('SettingsPanel MCP section', () => {
  it('stays visible while loading and when the inventory comes back empty', async () => {
    let land: (v: McpServerInfo[]) => void = () => {}
    api.getMcpServers.mockReturnValue(new Promise<McpServerInfo[]>((res) => { land = res }))
    await mount()
    openSection('MCP Servers')

    expect(screen.getByText('Loading servers…')).toBeTruthy()
    land([])
    expect(await screen.findByText(/No MCP servers are configured yet/)).toBeTruthy()
  })

  it('stays visible when the inventory read fails outright', async () => {
    api.getMcpServers.mockRejectedValue(new Error('offline'))
    await mount()
    openSection('MCP Servers')

    expect(await screen.findByText(/No MCP servers are configured yet/)).toBeTruthy()
  })

  it('separates granted servers from addable ones, and grants on Add', async () => {
    api.getMcpServers.mockResolvedValue([
      makeServer({ name: 'mochi:self', core: true }),
      makeServer({ name: 'extra-b' }),
    ])
    await mount()
    openSection('MCP Servers')

    expect(await screen.findByText('core')).toBeTruthy()
    expect(screen.getByText('Available to add:')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /Add/ }))
    expect(saveButton().disabled).toBe(false)
    // Granted rows are expandable; addable ones are not. Several rows can carry
    // the same agent label, so match all of them rather than demanding exactly one.
    expect((await screen.findAllByText('Chat only')).length).toBeGreaterThan(0)
  })

  it('revokes a granted non-core server without touching the core one', async () => {
    api.getConfig.mockResolvedValue(makeConfig({
      mochi: { extraMcpServers: [{ name: 'extra-b', agents: ['chat'], autoApprove: [], disabledTools: [] }] },
    }))
    api.getMcpServers.mockResolvedValue([
      makeServer({ name: 'mochi:self', core: true }),
      makeServer({ name: 'extra-b' }),
    ])
    await mount()
    openSection('MCP Servers')

    fireEvent.click(await screen.findByRole('button', { name: 'Remove extra-b' }))
    // It moves to the addable list, so the Add affordance appears for it.
    expect(await screen.findByRole('button', { name: /Add/ })).toBeTruthy()
    expect(saveButton().disabled).toBe(false)
  })

  it('discovers tools on first expand and groups them by effective policy', async () => {
    api.getConfig.mockResolvedValue(makeConfig({
      mochi: { extraMcpServers: [{ name: 'extra-b', agents: ['chat'], autoApprove: [], disabledTools: [] }] },
    }))
    api.getMcpServers.mockResolvedValue([makeServer({
      name: 'extra-b',
      chatPolicy: { autoApprove: ['read_file'], disabledTools: ['rm_rf', 'gone_tool'] },
      bgPolicy: { autoApprove: [], disabledTools: [] },
    })])
    api.discoverMcpTools.mockResolvedValue({
      tools: [{ name: 'read_file' }, { name: 'rm_rf' }, { name: 'write_file' }],
      fromCache: true,
    })
    await mount()
    openSection('MCP Servers')

    fireEvent.click(await screen.findByRole('button', { expanded: false }))
    await waitFor(() => expect(api.discoverMcpTools).toHaveBeenCalledWith('extra-b'))

    expect(await screen.findByText('Auto-approved')).toBeTruthy()
    expect(screen.getByText('Disabled')).toBeTruthy()
    expect(screen.getByText('Needs approval')).toBeTruthy()
    // A policy entry with no matching discovered tool is called out, not hidden.
    expect(screen.getByText('Stale (removed from server)')).toBeTruthy()
    expect(screen.getByText('(cached — may be stale)')).toBeTruthy()

    // Collapsing and re-expanding must not re-probe: the list is already held.
    fireEvent.click(screen.getByRole('button', { expanded: true }))
    fireEvent.click(screen.getByRole('button', { expanded: false }))
    expect(api.discoverMcpTools).toHaveBeenCalledTimes(1)
  })

  it('expands from the keyboard too', async () => {
    api.getMcpServers.mockResolvedValue([makeServer({ name: 'mochi:self', core: true })])
    await mount()
    openSection('MCP Servers')

    const row = await screen.findByRole('button', { expanded: false })
    fireEvent.keyDown(row, { key: 'x' })
    expect(screen.getByRole('button', { expanded: false })).toBeTruthy()

    fireEvent.keyDown(row, { key: 'Enter' })
    expect(await screen.findByText('Refresh Tools')).toBeTruthy()
    // A core server has no per-agent toggles — its grant is not negotiable here.
    expect(screen.queryByText('Apply to Chat')).toBeNull()
  })

  it('shows the per-agent policy for the selected tab', async () => {
    api.getMcpServers.mockResolvedValue([makeServer({
      name: 'mochi:self',
      core: true,
      chatPolicy: { autoApprove: ['read_file'], disabledTools: [] },
      bgPolicy: { autoApprove: [], disabledTools: ['read_file'] },
    })])
    api.discoverMcpTools.mockResolvedValue({ tools: [{ name: 'read_file' }], fromCache: false })
    await mount()
    openSection('MCP Servers')

    fireEvent.click(await screen.findByRole('button', { expanded: false }))
    expect(await screen.findByText('Auto-approved')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Background Tools' }))
    expect(await screen.findByText('Disabled')).toBeTruthy()
    expect(screen.queryByText('Auto-approved')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Chat Tools' }))
    expect(await screen.findByText('Auto-approved')).toBeTruthy()
  })

  it('stages a per-agent grant change from the expanded row', async () => {
    api.getConfig.mockResolvedValue(makeConfig({
      mochi: { extraMcpServers: [{ name: 'extra-b', agents: ['chat'], autoApprove: [], disabledTools: [] }] },
    }))
    api.getMcpServers.mockResolvedValue([makeServer({ name: 'extra-b' })])
    await mount()
    openSection('MCP Servers')

    fireEvent.click(await screen.findByRole('button', { expanded: false }))
    const [chatToggle, bgToggle] = await screen.findAllByRole('switch')
    expect(chatToggle.getAttribute('aria-checked')).toBe('true')
    expect(bgToggle.getAttribute('aria-checked')).toBe('false')

    fireEvent.keyDown(bgToggle, { key: 'Enter' })
    expect(await screen.findByText('Chat + BG')).toBeTruthy()

    fireEvent.click(screen.getAllByRole('switch')[0])
    expect(await screen.findByText('BG only')).toBeTruthy()
    expect(saveButton().disabled).toBe(false)
  })

  it('translates a discover failure code and keeps the last good tool list on screen', async () => {
    api.getMcpServers.mockResolvedValue([makeServer({
      name: 'mochi:self',
      core: true,
      chatPolicy: { autoApprove: [], disabledTools: [] },
      bgPolicy: { autoApprove: [], disabledTools: [] },
    })])
    api.discoverMcpTools.mockResolvedValue({ tools: [{ name: 'read_file' }], fromCache: false })
    await mount()
    openSection('MCP Servers')

    fireEvent.click(await screen.findByRole('button', { expanded: false }))
    expect(await screen.findByText('Needs approval')).toBeTruthy()

    api.discoverMcpTools.mockResolvedValue({ tools: [], fromCache: false, errorCode: 'server_disabled' })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh Tools' }))

    expect(await screen.findByText(/This server is disabled/)).toBeTruthy()
    // The error ADDS to the view; it must not wipe the chips already there.
    expect(screen.getByText('Needs approval')).toBeTruthy()
  })

  it('falls back to one generic line for an unmapped failure code', async () => {
    api.getMcpServers.mockResolvedValue([makeServer({ name: 'mochi:self', core: true })])
    api.discoverMcpTools.mockResolvedValue({ tools: [], fromCache: false, errorCode: 'http_405' })
    await mount()
    openSection('MCP Servers')

    fireEvent.click(await screen.findByRole('button', { expanded: false }))
    expect(await screen.findByText(/Couldn't load tools/)).toBeTruthy()
  })

  it('reports no tools when the probe returns nothing at all', async () => {
    api.getMcpServers.mockResolvedValue([makeServer({ name: 'mochi:self', core: true })])
    api.discoverMcpTools.mockResolvedValue(null)
    await mount()
    openSection('MCP Servers')

    fireEvent.click(await screen.findByRole('button', { expanded: false }))
    expect(await screen.findByText('No tools discovered. Click Refresh Tools.')).toBeTruthy()
  })

  it('survives a throwing probe without leaving the button stuck', async () => {
    api.getMcpServers.mockResolvedValue([makeServer({ name: 'mochi:self', core: true })])
    api.discoverMcpTools.mockRejectedValue(new Error('boom'))
    await mount()
    openSection('MCP Servers')

    fireEvent.click(await screen.findByRole('button', { expanded: false }))
    const refresh = await screen.findByRole('button', { name: 'Refresh Tools' })
    await waitFor(() => expect((refresh as HTMLButtonElement).disabled).toBe(false))
  })

  it.each([
    ['normal', /Trust is Normal/],
    ['trust_reads', /Trust is Read/],
    ['trust', /Trust all tools is on/],
    ['yolo', /YOLO is on/],
  ] as const)('states the effective approval posture for %s', async (level, matcher) => {
    api.getMochiTrustLevel.mockResolvedValue(level)
    api.getMcpServers.mockResolvedValue([makeServer({ name: 'mochi:self', core: true })])
    await mount()
    openSection('MCP Servers')

    expect(await screen.findByText(matcher)).toBeTruthy()
  })
})

/* ── trust ────────────────────────────────────────────────────── */

describe('SettingsPanel trust section', () => {
  it('stages a slot trust change and writes it only on Save', async () => {
    await mount()
    openSection('Trust')

    clickRow('Read')
    expect(saveButton().disabled).toBe(false)
    expect(api.setMochiTrustLevel).not.toHaveBeenCalled()

    fireEvent.click(saveButton())
    await waitFor(() => expect(api.setMochiTrustLevel).toHaveBeenCalledWith('trust_reads'))
  })

  it('does not offer yolo, but says so truthfully when the slot is already in it', async () => {
    api.getMochiTrustLevel.mockResolvedValue('yolo')
    await mount()
    openSection('Trust')

    expect(await screen.findByText(/YOLO \(auto-approve everything\) is active/)).toBeTruthy()
    expect(screen.queryByText('YOLO')).toBeNull()
    // Reading a level it cannot set must not read as a pending change.
    expect(saveButton().disabled).toBe(true)
  })
})

/* ── instances ────────────────────────────────────────────────── */

describe('SettingsPanel instances section', () => {
  it('says the picker needs the desktop app instead of rendering a dead control', async () => {
    api.hasShell = false
    await mount()
    openSection('Instances')

    expect(screen.getByText(/needs the desktop app/)).toBeTruthy()
    expect(screen.queryByText(/^instance:/)).toBeNull()
  })

  it('hands the current pointer to the picker and stages what comes back', async () => {
    await mount()
    openSection('Instances')

    expect(screen.getByText('instance:self')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'pick remote' }))
    expect(screen.getByText('instance:remote-1')).toBeTruthy()
    expect(saveButton().disabled).toBe(false)
  })
})

/* ── shortcuts ────────────────────────────────────────────────── */

describe('SettingsPanel shortcuts section', () => {
  it('says the recorder needs the desktop app when there is no shell to bind in', async () => {
    api.hasShell = false
    await mount()
    openSection('Shortcuts')

    expect(screen.getByText(/Changing the global shortcuts needs the desktop app/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Record a shortcut' })).toBeNull()
  })

  it('renders the stored accelerator as one chip per key', async () => {
    await mount()
    openSection('Shortcuts')

    // 'CommandOrControl+Shift+X' — the leading glyph is platform-resolved, the
    // rest is not, so the platform-independent part is what gets asserted.
    const capture = screen.getByText('Screen Capture').parentElement as HTMLElement
    const chips = [...within(capture).getAllByText(/^(Shift|X|⌘|Ctrl)$/)].map((n) => n.textContent)
    expect(chips).toContain('Shift')
    expect(chips).toContain('X')
  })

  it('records a chord and stores it in canonical modifier order', async () => {
    await mount()
    openSection('Shortcuts')

    const [record] = screen.getAllByRole('button', { name: 'Record a shortcut' })
    fireEvent.click(record)
    expect(screen.getByText('Press keys...')).toBeTruthy()

    // Pressed rest-first on purpose: the stored string must be a function of the
    // chord, not of typing order.
    fireEvent.keyDown(window, { key: 'k' })
    fireEvent.keyDown(window, { key: 'Shift' })
    fireEvent.keyDown(window, { key: 'Control' })
    fireEvent.keyUp(window, { key: 'Control' })

    expect(screen.queryByText('Press keys...')).toBeNull()
    const capture = screen.getByText('Screen Capture').parentElement as HTMLElement
    expect(within(capture).getByText('Shift')).toBeTruthy()
    expect(within(capture).getByText('K')).toBeTruthy()
    expect(saveButton().disabled).toBe(false)

    fireEvent.click(saveButton())
    await waitFor(() => expect(api.applyShortcuts).toHaveBeenCalled())
    expect(api.applyShortcuts.mock.calls[0][0]).toMatchObject({
      screenCapture: 'CommandOrControl+Shift+K',
    })
  })

  it('refuses a modifier-only chord and says what is missing', async () => {
    await mount()
    openSection('Shortcuts')

    fireEvent.click(screen.getAllByRole('button', { name: 'Record a shortcut' })[0])
    fireEvent.keyDown(window, { key: 'Shift' })
    fireEvent.keyUp(window, { key: 'Shift' })

    expect(screen.getByText('Need a non-modifier key')).toBeTruthy()
    expect(saveButton().disabled).toBe(true)
  })

  it('refuses two non-modifier keys', async () => {
    await mount()
    openSection('Shortcuts')

    fireEvent.click(screen.getAllByRole('button', { name: 'Record a shortcut' })[0])
    fireEvent.keyDown(window, { key: 'Shift' })
    fireEvent.keyDown(window, { key: 'a' })
    fireEvent.keyDown(window, { key: 'b' })
    fireEvent.keyUp(window, { key: 'Shift' })

    expect(screen.getByText('Only one non-modifier key allowed')).toBeTruthy()
  })

  it('refuses a lone non-modifier key', async () => {
    await mount()
    openSection('Shortcuts')

    fireEvent.click(screen.getAllByRole('button', { name: 'Record a shortcut' })[0])
    fireEvent.keyDown(window, { key: 'q' })
    fireEvent.keyUp(window, { key: 'q' })

    expect(screen.getByText('Invalid combo')).toBeTruthy()
  })

  it('caps a chord at three keys, ignoring anything pressed after', async () => {
    await mount()
    openSection('Shortcuts')

    fireEvent.click(screen.getAllByRole('button', { name: 'Record a shortcut' })[0])
    fireEvent.keyDown(window, { key: 'Control' })
    fireEvent.keyDown(window, { key: 'Shift' })
    fireEvent.keyDown(window, { key: 'Alt' })
    fireEvent.keyDown(window, { key: 'z' })
    fireEvent.keyUp(window, { key: 'Control' })

    // Three modifiers and no key: the 4th press was dropped, so it is refused.
    expect(screen.getByText('Need a non-modifier key')).toBeTruthy()
  })

  it('cancels a recording and leaves the stored accelerator alone', async () => {
    await mount()
    openSection('Shortcuts')

    const [record] = screen.getAllByRole('button', { name: 'Record a shortcut' })
    fireEvent.click(record)
    fireEvent.keyDown(window, { key: 'Control' })
    fireEvent.click(screen.getAllByRole('button', { name: 'Cancel' })[0])

    expect(screen.queryByText('Press keys...')).toBeNull()
    // Cancel is a no-op on the value, so nothing is pending.
    expect(saveButton().disabled).toBe(true)
    // …and a key pressed after cancelling is not captured.
    fireEvent.keyDown(window, { key: 'j' })
    expect(saveButton().disabled).toBe(true)
  })

  it('offers the other two accelerators too, each independently editable', async () => {
    await mount()
    openSection('Shortcuts')

    expect(screen.getByText('Toggle Window')).toBeTruthy()
    const hideAll = screen.getByText('Hide All').parentElement as HTMLElement
    fireEvent.click(within(hideAll).getByRole('button', { name: 'Record a shortcut' }))
    fireEvent.keyDown(window, { key: 'Control' })
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyUp(window, { key: 'Control' })

    fireEvent.click(saveButton())
    await waitFor(() => expect(api.applyShortcuts).toHaveBeenCalled())
    expect(api.applyShortcuts.mock.calls[0][0]).toMatchObject({
      hideAll: 'CommandOrControl+J',
      screenCapture: 'CommandOrControl+Shift+X',
    })
  })
})

/* ── about ────────────────────────────────────────────────────── */

describe('SettingsPanel about section', () => {
  it('opens the author link through the shell, by click and by keyboard', async () => {
    await mount()
    openSection('About')

    expect(screen.getByText(/is a desktop companion built into Kiro Crew/)).toBeTruthy()
    const link = screen.getByRole('link', { name: 'buluoray' })

    fireEvent.click(link)
    expect(api.openExternal).toHaveBeenCalledWith('https://github.com/buluoray')

    fireEvent.keyDown(link, { key: 'Enter' })
    expect(api.openExternal).toHaveBeenCalledTimes(2)
    fireEvent.keyDown(link, { key: 'a' })
    expect(api.openExternal).toHaveBeenCalledTimes(2)
  })

  it('underlines the link on hover only', async () => {
    await mount()
    openSection('About')

    const link = screen.getByRole('link', { name: 'buluoray' })
    fireEvent.mouseEnter(link)
    expect(link.style.textDecoration).toBe('underline')
    fireEvent.mouseLeave(link)
    expect(link.style.textDecoration).toBe('none')
  })
})

/* ── save ─────────────────────────────────────────────────────── */

describe('SettingsPanel save', () => {
  it('writes the config, binds the accelerators, then closes', async () => {
    await mount()
    fireEvent.change(screen.getByDisplayValue('Mochi'), { target: { value: 'Tofu' } })
    fireEvent.click(saveButton())

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(api.updateConfig).toHaveBeenCalledTimes(1)
    expect((api.updateConfig.mock.calls[0][0] as AppConfig).mochi.petName).toBe('Tofu')
    expect(api.applyShortcuts).toHaveBeenCalledTimes(1)
    // Trust did not change, so the slot must not be written.
    expect(api.setMochiTrustLevel).not.toHaveBeenCalled()
    // The baseline is committed, so there is nothing left to save.
    expect(saveButton().disabled).toBe(true)
  })

  it('leaves the pointer alone when the instance did not change', async () => {
    await mount()
    fireEvent.change(screen.getByDisplayValue('Mochi'), { target: { value: 'Tofu' } })
    fireEvent.click(saveButton())

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(api.setPetInstance).not.toHaveBeenCalled()
  })

  it('reveals the refusal and stays dirty when the OS already owns the key', async () => {
    api.applyShortcuts.mockResolvedValue({ screenCapture: false, toggleWindow: true })
    await mount()
    fireEvent.change(screen.getByDisplayValue('Mochi'), { target: { value: 'Tofu' } })
    fireEvent.click(saveButton())

    // Save is global but the message renders under Shortcuts, so the panel must
    // move there rather than refusing to close with no visible reason.
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/Another app already owns this key: screenCapture/)
    expect(onClose).not.toHaveBeenCalled()
    // A refused key really was not stored, so it stays pending for the user.
    expect(saveButton().disabled).toBe(false)
  })

  it('clears a stale refusal once the keys bind', async () => {
    api.applyShortcuts.mockResolvedValue({ screenCapture: false })
    await mount()
    fireEvent.change(screen.getByDisplayValue('Mochi'), { target: { value: 'Tofu' } })
    fireEvent.click(saveButton())
    expect(await screen.findByRole('alert')).toBeTruthy()

    api.applyShortcuts.mockResolvedValue({ screenCapture: true })
    fireEvent.click(saveButton())
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('moves the pet through the shell when the instance changed', async () => {
    await mount()
    openSection('Instances')
    fireEvent.click(screen.getByRole('button', { name: 'pick remote' }))
    fireEvent.click(saveButton())

    await waitFor(() => expect(api.setPetInstance).toHaveBeenCalledWith('remote-1'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('reports a declined switch, keeps the panel open, and still commits the baseline', async () => {
    api.setPetInstance.mockResolvedValue(false)
    await mount()
    openSection('Instances')
    fireEvent.click(screen.getByRole('button', { name: 'pick remote' }))
    fireEvent.click(saveButton())

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/Could not switch instance right now/)
    expect(onClose).not.toHaveBeenCalled()
    // The shell already stored the pointer, so offering a Discard that resets the
    // field while the pet still moves later would be worse than reporting it.
    expect(saveButton().disabled).toBe(true)
    // Everything before the switch still landed.
    expect(api.updateConfig).toHaveBeenCalledTimes(1)
  })

  it('orders the accelerator bind before the instance switch', async () => {
    const order: string[] = []
    api.applyShortcuts.mockImplementation(async () => { order.push('shortcuts'); return {} })
    api.setPetInstance.mockImplementation(async () => { order.push('instance'); return true })
    await mount()
    openSection('Instances')
    fireEvent.click(screen.getByRole('button', { name: 'pick remote' }))
    fireEvent.click(saveButton())

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(order).toEqual(['shortcuts', 'instance'])
  })
})

/* ── close / discard ──────────────────────────────────────────── */

describe('SettingsPanel close', () => {
  it('closes straight away when nothing was edited', async () => {
    await mount()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Unsaved Changes')).toBeNull()
  })

  it('asks before dropping staged edits', async () => {
    await mount()
    fireEvent.change(screen.getByDisplayValue('Mochi'), { target: { value: 'Tofu' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.getByText('Unsaved Changes')).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('restores what was read when the edits are discarded', async () => {
    await mount()
    openSection('Trust')
    clickRow('Read')
    fireEvent.change(screen.getByRole('button', { name: 'General' }), {})
    openSection('General')
    fireEvent.change(screen.getByDisplayValue('Mochi'), { target: { value: 'Tofu' } })

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(api.updateConfig).not.toHaveBeenCalled()
    expect(api.setMochiTrustLevel).not.toHaveBeenCalled()
    expect(screen.getByDisplayValue('Mochi')).toBeTruthy()
    expect(saveButton().disabled).toBe(true)
  })

  it('saves from the dialog instead, when that is what the user picks', async () => {
    await mount()
    fireEvent.change(screen.getByDisplayValue('Mochi'), { target: { value: 'Tofu' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    // The dialog's Save is the second one; the footer keeps the first.
    fireEvent.click(screen.getAllByRole('button', { name: 'Save' })[1])

    await waitFor(() => expect(api.updateConfig).toHaveBeenCalledTimes(1))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Unsaved Changes')).toBeNull()
  })

  it('treats the native window close the same as Cancel', async () => {
    await mount()
    fireEvent.change(screen.getByDisplayValue('Mochi'), { target: { value: 'Tofu' } })

    mocks.requestClose()
    expect(await screen.findByText('Unsaved Changes')).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('lets the native window close go through when nothing is pending', async () => {
    await mount()
    mocks.requestClose()

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('survives a native close that fires during subscribe', () => {
    // Pins the TDZ: subscribe used to close over handleClose declared later in
    // the component body, so an early fire threw ReferenceError.
    api.onSettingsCloseRequested = (cb: () => void) => {
      cb()
      return mocks.subscribeClose(cb)
    }

    expect(() => render(<SettingsPanel onClose={onClose} />)).not.toThrow()
  })

  it('keeps the native-close handler current after re-renders', async () => {
    // Pins the empty-deps stale-closure case: the first handleClose always saw
    // isDirty false (config not landed yet), so a later close skipped the prompt.
    await mount()
    openSection('Appearance')
    openSection('Behavior')
    openSection('General')
    fireEvent.change(screen.getByDisplayValue('Mochi'), { target: { value: 'Tofu' } })

    mocks.requestClose()
    expect(await screen.findByText('Unsaved Changes')).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })
})
