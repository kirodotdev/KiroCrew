/**
 * The import preview's origin line: one translated sentence with the archive's
 * creation time formatted for the reader's locale, not two glued fragments
 * around the raw UTC timestamp the manifest stores.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { i18next } from '../../i18n/index'
import '../../i18n/all'
import { fmtDateTime } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import PortabilityTab from './PortabilityTab'

const STAMP = '2026-07-30T15:04:00Z'

function stubPreview() {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(
    { ok: true, manifest: { version: 1, created_at: STAMP, hostname: 'laptop', user: 'ana', contents: {} } },
  ), { status: 200 })))
}

async function choose() {
  render(<PortabilityTab />)
  const input = screen.getByLabelText(i18nT('pages.overview.portabilityTab.choose_import_file')) as HTMLInputElement
  fireEvent.change(input, { target: { files: [new File(['PK'], 'e.zip')] } })
  return screen.findByText((_t, el) => el?.tagName === 'DIV' && (el.textContent ?? '').includes('ana@laptop') && el.children.length === 0)
}

afterEach(async () => {
  cleanup()
  vi.unstubAllGlobals()
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('PortabilityTab import preview origin line', () => {
  it('shows the creation time formatted, not the raw manifest timestamp', async () => {
    stubPreview()
    const line = await choose()
    expect(line.textContent).toBe(`Created: ${fmtDateTime(STAMP)} | From: ana@laptop`)
    expect(line.textContent).not.toContain(STAMP)
  })

  it('reads as one sentence in Japanese, with the date in the Japanese format', async () => {
    await i18next.changeLanguage('ja')
    stubPreview()
    const line = await choose()
    expect(line.textContent).toBe(`作成: ${fmtDateTime(STAMP)} | 送信元: ana@laptop`)
    expect(line.textContent).not.toContain(STAMP)
  })
})
