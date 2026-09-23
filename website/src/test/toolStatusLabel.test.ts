/**
 * toolStatusLabel: the purpose-vs-raw-tool-title selection shared by every
 * session-list surface (sidebar rows, command-palette recents) so a row agrees
 * with the inline tool pill in the transcript.
 *
 * The detail it reads is split by `kind`: a `tool` phase carries the
 * agent-written `purpose` (never a display label); the fixed phases carry no
 * copy and resolve from `kind` at render time; a server-supplied status is the
 * one non-tool phase with a `label`. This file pins that each branch reads ONLY
 * its own field, so the resolver stays the single place the meanings meet.
 */
import { describe, it, expect, afterEach } from 'vitest'
// Through `/all` so the German catalog the locale case asserts on is registered.
import { i18next } from '../i18n/all'
import { toolStatusLabel, type ToolStatusDetail } from '../utils/toolStatusLabel'

describe('toolStatusLabel', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  const toolDetail: ToolStatusDetail = { kind: 'tool', purpose: 'Looking up the mic permission handler', toolName: 'grep' }

  it('returns the agent purpose when simplified names are on', () => {
    expect(toolStatusLabel(toolDetail, true)).toBe('Looking up the mic permission handler')
  })

  it('returns the raw tool title when simplified names are off', () => {
    expect(toolStatusLabel(toolDetail, false)).toBe('grep')
  })

  it('falls back to the purpose in raw mode when no tool title was recorded', () => {
    // Details restored from before toolName was carried, or a status whose
    // tool title was empty: showing the purpose beats blanking the row.
    expect(toolStatusLabel({ kind: 'tool', purpose: 'Reading gateway.log' }, false)).toBe('Reading gateway.log')
  })

  it('renders the fixed phases from their kind in both modes', () => {
    for (const simplified of [true, false]) {
      expect(toolStatusLabel({ kind: 'thinking' }, simplified)).toBe('Thinking…')
      expect(toolStatusLabel({ kind: 'streaming' }, simplified)).toBe('Streaming')
    }
  })

  it('passes a server-supplied status through unchanged in both modes', () => {
    // chat_status arrives as a `thinking` phase carrying its own `label`, never
    // a tool title; the label wins over the fixed-phase copy.
    for (const simplified of [true, false]) {
      expect(toolStatusLabel({ kind: 'thinking', label: 'Compacting…' }, simplified)).toBe('Compacting…')
    }
  })

  it('renders the fixed phases in the current UI language', async () => {
    // The store holds only `kind`, so the copy is looked up when the row paints:
    // switching language re-renders the row rather than freezing the phrase
    // that was active at dispatch.
    await i18next.changeLanguage('de')
    const thinking = i18next.t('pages.chatSidebar.thinking')
    expect(thinking).not.toBe('Thinking…')
    expect(toolStatusLabel({ kind: 'thinking' }, true)).toBe(thinking)
    expect(toolStatusLabel({ kind: 'streaming' }, true)).toBe(i18next.t('pages.chatSidebar.streaming'))
    // A server-supplied status is not catalog copy and stays as sent.
    expect(toolStatusLabel({ kind: 'thinking', label: 'Compacting…' }, true)).toBe('Compacting…')
  })

  it('returns empty string when there is nothing to show (caller owns fallback copy)', () => {
    expect(toolStatusLabel(undefined, true)).toBe('')
    expect(toolStatusLabel({ kind: 'idle' }, true)).toBe('')
    expect(toolStatusLabel({ kind: 'tool' }, false)).toBe('')
    expect(toolStatusLabel({ kind: 'tool', purpose: '' }, true)).toBe('')
  })

  it('reads a tool phase from `purpose` alone, never from a label field', () => {
    // A stray `label` on a tool detail must not be painted: the tool branch
    // resolves purpose, derived title and raw title, and nothing else. Cast
    // because the type forbids the shape on purpose — the runtime guard is what
    // this pins.
    const stray = { kind: 'tool', label: 'Compacting…', toolName: 'grep' } as unknown as ToolStatusDetail
    expect(toolStatusLabel(stray, true)).toBe('grep')
    expect(toolStatusLabel(stray, false)).toBe('grep')
    const strayNoTitle = { kind: 'tool', label: 'Compacting…' } as unknown as ToolStatusDetail
    expect(toolStatusLabel(strayNoTitle, true)).toBe('')
  })

  it('reads a non-tool phase from `label` alone, never from a purpose field', () => {
    // The mirror image: a purpose smuggled onto a phase detail is not a label,
    // so the phase renders its own catalog copy (or nothing, for idle).
    const stray = { kind: 'thinking', purpose: 'Reading gateway.log' } as unknown as ToolStatusDetail
    expect(toolStatusLabel(stray, true)).toBe('Thinking…')
    expect(toolStatusLabel(stray, false)).toBe('Thinking…')
    const strayIdle = { kind: 'idle', purpose: 'Reading gateway.log' } as unknown as ToolStatusDetail
    expect(toolStatusLabel(strayIdle, true)).toBe('')
  })
})
