// The agent pill bar — ported from the upstream app's own test file.
//
// What it pins: a pill reflects and toggles its agent's enabled state, the live
// dot only appears while the meeting is running, the preset picker round-trips,
// and the attachment menu's icon-only controls carry accessible labels (the
// blocking `icon-buttons-need-labels` rule).

import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

import AgentPillBar from '../apps/meetings/components/AgentPillBar'
import type { AgentDef, Attachment, MeetingStatus, Preset } from '../apps/meetings/api'

const AGENTS: AgentDef[] = [
  { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown' },
  { id: 'sketch-artist', name: 'Sketch Artist', widget_type: 'html' },
]

const PRESETS: Record<string, Preset> = {
  standup: { enabled_agents: ['note-taker'] },
  design: { enabled_agents: ['note-taker', 'sketch-artist'] },
}

function mount(overrides: Partial<React.ComponentProps<typeof AgentPillBar>> = {}) {
  const props: React.ComponentProps<typeof AgentPillBar> = {
    agents: AGENTS,
    enabledIds: ['note-taker'],
    mutedAgents: [],
    presets: PRESETS,
    defaultPreset: 'standup',
    selectedPreset: 'standup',
    status: 'idle' as MeetingStatus,
    attachments: [],
    attachMenuOpen: false,
    onPresetChange: vi.fn(),
    onToggleAgent: vi.fn(),
    onOpenSettings: vi.fn(),
    onToggleAttachMenu: vi.fn(),
    onAddAttachment: vi.fn(),
    onRemoveAttachment: vi.fn(),
    ...overrides,
  }
  return { props, ...render(<AgentPillBar {...props} />) }
}

afterEach(cleanup)

describe('AgentPillBar', () => {
  it('renders a pill per agent', () => {
    mount()
    expect(screen.getByText('Note Taker')).toBeTruthy()
    expect(screen.getByText('Sketch Artist')).toBeTruthy()
  })

  it('toggles an agent ON when a disabled pill is clicked', () => {
    const onToggleAgent = vi.fn()
    mount({ onToggleAgent })
    fireEvent.click(screen.getByText('Sketch Artist').closest('button')!)
    // The second argument is the DESIRED state, so an off pill must ask for true.
    expect(onToggleAgent).toHaveBeenCalledWith('sketch-artist', true)
  })

  it('toggles an agent OFF when an enabled pill is clicked', () => {
    const onToggleAgent = vi.fn()
    mount({ onToggleAgent })
    fireEvent.click(screen.getByText('Note Taker').closest('button')!)
    expect(onToggleAgent).toHaveBeenCalledWith('note-taker', false)
  })

  it('shows a live dot for an enabled, unmuted agent during an active meeting', () => {
    mount({ status: 'active' })
    const pill = screen.getByText('Note Taker').closest('button')!
    expect(pill.querySelector('.animate-pulse')).toBeTruthy()
  })

  it('shows no live dot while the meeting is idle', () => {
    mount({ status: 'idle' })
    expect(
      screen.getByText('Note Taker').closest('button')!.querySelector('.animate-pulse'),
    ).toBeNull()
  })

  it('shows no live dot for a muted agent even during an active meeting', () => {
    mount({ status: 'active', mutedAgents: ['note-taker'] })
    expect(
      screen.getByText('Note Taker').closest('button')!.querySelector('.animate-pulse'),
    ).toBeNull()
  })

  it('renders the preset picker with the active preset selected', () => {
    mount()
    // The picker is a Radix Select now, so the selection lives in the trigger's
    // text, not a `.value` — and 'standup' is the default preset, so it renders
    // decorated.
    expect(screen.getByRole('combobox', { name: 'Agent preset' })).toHaveTextContent(
      'standup (default)',
    )
  })

  it('reports a preset change', async () => {
    const onPresetChange = vi.fn()
    mount({ onPresetChange })
    // A `change` event on the trigger does nothing — open it, then click the option.
    fireEvent.click(screen.getByRole('combobox', { name: 'Agent preset' }))
    fireEvent.click(await screen.findByRole('option', { name: 'design' }))
    expect(onPresetChange).toHaveBeenCalledWith('design')
  })

  it('clears the preset back to empty from the "no preset" row', async () => {
    // The old `<option value="">` is now SimpleSelect's `clearLabel`, which routes
    // through an internal sentinel. This pins that '' still reaches the callback
    // rather than the sentinel leaking out.
    const onPresetChange = vi.fn()
    mount({ onPresetChange })
    fireEvent.click(screen.getByRole('combobox', { name: 'Agent preset' }))
    fireEvent.click(await screen.findByRole('option', { name: 'No preset' }))
    expect(onPresetChange).toHaveBeenCalledWith('')
  })

  it('offers to create one when there are no presets', () => {
    mount({ presets: {} })
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('shows the attachment count', () => {
    const attachments: Attachment[] = [
      { type: 'url', url: 'https://example.test/a', label: 'A' },
      { type: 'url', url: 'https://example.test/b', label: 'B' },
    ]
    mount({ attachments })
    expect(screen.getByText('2')).toBeTruthy()
  })

  it('lists attachments and reports a removal by index', () => {
    const onRemoveAttachment = vi.fn()
    mount({
      attachMenuOpen: true,
      onRemoveAttachment,
      attachments: [{ type: 'url', url: 'https://example.test/doc', label: 'Design Doc' }],
    })
    expect(screen.getByText('Design Doc')).toBeTruthy()
    // Icon-only control: it must be reachable by its accessible name.
    fireEvent.click(screen.getByLabelText('Remove Design Doc'))
    expect(onRemoveAttachment).toHaveBeenCalledWith(0)
  })

  it('every icon-only control has an accessible name', () => {
    mount({ attachMenuOpen: true })
    for (const button of screen.getAllByRole('button')) {
      const named = button.textContent?.trim() || button.getAttribute('aria-label')
      expect(named, `unlabelled control: ${button.outerHTML.slice(0, 80)}`).toBeTruthy()
    }
  })
})
