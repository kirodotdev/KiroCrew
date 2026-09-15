/**
 * toolStatusLabel: the purpose-vs-raw-tool-title selection shared by every
 * session-list surface (sidebar rows, command-palette recents) so a row agrees
 * with the inline tool pill in the transcript.
 */
import { describe, it, expect } from 'vitest'
import { toolStatusLabel } from '../utils/toolStatusLabel'

describe('toolStatusLabel', () => {
  const toolDetail = { kind: 'tool', text: 'Looking up the mic permission handler', toolName: 'grep' }

  it('returns the agent purpose when simplified names are on', () => {
    expect(toolStatusLabel(toolDetail, true)).toBe('Looking up the mic permission handler')
  })

  it('returns the raw tool title when simplified names are off', () => {
    expect(toolStatusLabel(toolDetail, false)).toBe('grep')
  })

  it('falls back to the purpose in raw mode when no tool title was recorded', () => {
    // Details restored from before toolName was carried, or a status whose
    // tool title was empty: showing the purpose beats blanking the row.
    expect(toolStatusLabel({ kind: 'tool', text: 'Reading gateway.log' }, false)).toBe('Reading gateway.log')
  })

  it('passes non-tool phases through unchanged in both modes', () => {
    for (const simplified of [true, false]) {
      expect(toolStatusLabel({ kind: 'thinking', text: 'Thinking…' }, simplified)).toBe('Thinking…')
      expect(toolStatusLabel({ kind: 'streaming', text: 'Streaming' }, simplified)).toBe('Streaming')
      // Server-supplied chat_status carries only `text`, never a tool title.
      expect(toolStatusLabel({ kind: 'thinking', text: 'Compacting…' }, simplified)).toBe('Compacting…')
    }
  })

  it('returns empty string when there is nothing to show (caller owns fallback copy)', () => {
    expect(toolStatusLabel(undefined, true)).toBe('')
    expect(toolStatusLabel({}, true)).toBe('')
    expect(toolStatusLabel({ kind: 'tool' }, false)).toBe('')
  })
})
