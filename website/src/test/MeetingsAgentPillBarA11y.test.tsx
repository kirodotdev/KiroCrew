// The agent pills are toggles, so a screen reader must hear their on/off state,
// and the live dot inside an enabled pill must be a named image, not a bare
// span whose aria-label assistive tech may skip.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'

import AgentPillBar from '../apps/meetings/components/AgentPillBar'
import type { AgentDef, MeetingStatus, Preset } from '../apps/meetings/api'
import EN_CATALOG from '../i18n/locales/en.json'

const PB = EN_CATALOG.apps.meetings.pillBar

const AGENTS: AgentDef[] = [
  { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown' },
  { id: 'sketch-artist', name: 'Sketch Artist', widget_type: 'html' },
]

const PRESETS: Record<string, Preset> = { standup: { enabled_agents: ['note-taker'] } }

function mount(status: MeetingStatus) {
  render(
    <AgentPillBar
      agents={AGENTS}
      enabledIds={['note-taker']}
      mutedAgents={[]}
      presets={PRESETS}
      defaultPreset="standup"
      selectedPreset="standup"
      status={status}
      attachments={[]}
      attachMenuOpen={false}
      onPresetChange={vi.fn()}
      onToggleAgent={vi.fn()}
      onOpenSettings={vi.fn()}
      onToggleAttachMenu={vi.fn()}
      onAddAttachment={vi.fn()}
      onRemoveAttachment={vi.fn()}
    />,
  )
}

const pill = (name: string) => screen.getByText(name).closest('button')!

afterEach(cleanup)

describe('AgentPillBar accessibility', () => {
  it('marks an enabled pill pressed and a disabled one not pressed', () => {
    mount('idle')
    expect(pill('Note Taker').getAttribute('aria-pressed')).toBe('true')
    expect(pill('Sketch Artist').getAttribute('aria-pressed')).toBe('false')
  })

  it('exposes the live dot as a named image while the meeting runs', () => {
    mount('active')
    expect(within(pill('Note Taker')).getByRole('img', { name: PB.listening })).toBeTruthy()
  })
})
