/**
 * The voice settings answer "Is my audio kept?" for the selected provider.
 *
 * Transcribe users look in their AWS console for job history that streaming
 * never creates, so the answer must say nothing is kept in the account, name
 * what AWS itself may keep, and link the control that governs it. The detail
 * sits behind the info tip so the page carries one line. `off` gets nothing.
 */
import { describe, it, expect, beforeAll, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { initI18n } from '../i18n'
import { i18nT } from '../i18n/t'
import SttRetentionNote, { AI_OPT_OUT_POLICY_URL } from '../pages/settings/SttRetentionNote'

beforeAll(async () => {
  await initI18n()
})

afterEach(cleanup)

describe('SttRetentionNote', () => {
  it.each(['local', 'apple'])('answers that %s audio stays on this machine', provider => {
    render(<SttRetentionNote provider={provider} />)
    const note = screen.getByTestId('stt-retention-note')
    expect(note.textContent).toContain(i18nT('pages.settings.sttSettings.retention_title'))
    expect(note.textContent).toContain(i18nT('pages.settings.sttSettings.retention_on_device'))
    expect(screen.queryByRole('link')).toBeNull()
    expect(note.querySelector(`[title="${i18nT('pages.settings.sttSettings.retention_on_device_detail')}"]`)).toBeTruthy()
  })

  it('answers that Transcribe keeps nothing in the account and links the AWS opt-out', () => {
    render(<SttRetentionNote provider="transcribe" />)
    const note = screen.getByTestId('stt-retention-note')
    expect(note.textContent).toContain('Not by')
    expect(note.textContent).toContain('AWS may keep a copy')
    const link = screen.getByRole('link', { name: i18nT('pages.settings.sttSettings.retention_transcribe_opt_out') })
    expect(link.getAttribute('href')).toBe(AI_OPT_OUT_POLICY_URL)
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    const detail = i18nT('pages.settings.sttSettings.retention_transcribe_detail')
    expect(detail).toContain("no transcription job or file is created in your account")
    expect(note.querySelector(`[title="${detail}"]`)).toBeTruthy()
  })

  it.each(['off', 'unknown'])('renders nothing for %s', provider => {
    const { container } = render(<SttRetentionNote provider={provider} />)
    expect(container.firstChild).toBeNull()
  })

  it('discloses hosted Codex audio and account-dependent access without promising free use', () => {
    render(<SttRetentionNote provider="codex" />)
    const note = screen.getByTestId('stt-retention-note')
    expect(note.textContent).toContain(i18nT('pages.settings.sttSettings.retention_codex'))
    const detail = i18nT('pages.settings.sttSettings.retention_codex_detail')
    expect(note.querySelector(`[title="${detail}"]`)).toBeTruthy()
    expect(screen.queryByRole('link')).toBeNull()
  })
})
