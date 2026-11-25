/**
 * Settings → Voice → Model for the OpenAI-compatible provider: what a blur saves.
 *
 * A changed id is saved trimmed, an unchanged one saves nothing, and a cleared field
 * saves "" so the stored model is removed rather than silently restored.
 */
import { describe, it, expect, vi, beforeAll, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { initI18n } from '../i18n'
import SttOpenAiCompatibleSettings from '../pages/settings/SttOpenAiCompatibleSettings'

beforeAll(async () => {
  await initI18n('en')
})

afterEach(cleanup)

function edit(value: string, model = 'whisper-1') {
  const onSave = vi.fn()
  render(<SttOpenAiCompatibleSettings model={model} onSave={onSave} />)
  const field = screen.getByRole('textbox')
  fireEvent.change(field, { target: { value } })
  fireEvent.blur(field)
  return { onSave, field }
}

describe('SttOpenAiCompatibleSettings', () => {
  it('saves a changed model id trimmed', () => {
    expect(edit('  gpt-4o-transcribe ').onSave).toHaveBeenCalledWith('gpt-4o-transcribe')
  })

  it('saves nothing when the id is unchanged', () => {
    expect(edit('whisper-1 ').onSave).not.toHaveBeenCalled()
  })

  it('clears the stored model when the field is emptied', () => {
    const { onSave, field } = edit('   ')
    expect(onSave).toHaveBeenCalledWith('')
    expect((field as HTMLInputElement).value).toBe('')
  })

  it('saves nothing when an empty field stays empty', () => {
    expect(edit('', '').onSave).not.toHaveBeenCalled()
  })
})
