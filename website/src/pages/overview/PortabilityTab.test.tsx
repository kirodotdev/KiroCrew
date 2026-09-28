import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import PortabilityTab, { refusalText, unbundledTemplates } from './PortabilityTab'

describe('refusalText', () => {
  const fallback = 'Import failed.'

  it('prefers the localized fallback over a coded 5xx boilerplate message', () => {
    // What these handlers actually answer with: opaque English produced in
    // Python, which says no more than the catalog string already says.
    expect(refusalText(500, { error: 'Import failed', code: 'import_failed' }, fallback))
      .toBe(fallback)
    expect(refusalText(500, { error: 'Preview failed', code: 'preview_failed' }, fallback))
      .toBe(fallback)
  })

  it('keeps the validator detail a coded 4xx carries', () => {
    // `import_archive_invalid` reports the archive validator's own finding.
    // That prose is the whole value of the message, so it must survive.
    expect(refusalText(
      400,
      { error: 'manifest.json is missing', code: 'import_archive_invalid' },
      fallback,
    )).toBe('manifest.json is missing')
  })

  it('keeps the prose of an uncoded refusal at any status', () => {
    // No machine-readable identity means the refusal may not be from these
    // handlers at all — a proxy, an edge, a gateway — and there the message can
    // be the only detail there is.
    expect(refusalText(500, { error: 'Bad gateway' }, fallback)).toBe('Bad gateway')
    expect(refusalText(400, { error: 'nope' }, fallback)).toBe('nope')
  })

  it('falls back when the body carries no message at all', () => {
    expect(refusalText(500, {}, fallback)).toBe(fallback)
    expect(refusalText(400, { error: '' }, fallback)).toBe(fallback)
  })
})

describe('unbundledTemplates', () => {
  it('reads the header list and drops anything that is not a name', () => {
    expect(unbundledTemplates('["a", 3, "b"]')).toEqual({ names: ['a', 'b'], more: 0 })
    expect(unbundledTemplates('["a", "+12"]')).toEqual({ names: ['a'], more: 12 })
    expect(unbundledTemplates(null)).toEqual({ names: [], more: 0 })
    expect(unbundledTemplates('{not json')).toEqual({ names: [], more: 0 })
    expect(unbundledTemplates('{"a": 1}')).toEqual({ names: [], more: 0 })
  })
})

describe('PortabilityTab template warnings', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('names the templates an export leaves out, and still downloads', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(new Blob(['PK']), {
      status: 200,
      headers: { 'X-Kirocrew-Unbundled-Templates': '["reviewer", "writer", "+3"]' },
    })))
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = () => 'blob:x'
      static revokeObjectURL = () => {}
    })
    render(<PortabilityTab />)
    fireEvent.click(screen.getByRole('button', { name: /download export/i }))
    const warning = await screen.findByTestId('portability-export-warning')
    expect(warning.textContent).toContain('reviewer, writer, 3 more')
    expect(warning.textContent).not.toContain('+3')
    expect(screen.getByText('Download started.')).toBeTruthy()
  })

  it('names each imported crew whose template is missing, beside the success line', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(
      url.includes('preview')
        ? { ok: true, manifest: { version: 1, created_at: 't', hostname: 'h', user: 'u', contents: {} } }
        : { ok: true, summary: { items: ['config (restored)'], missing_agent_templates: [{ crew: 'triage', kiro_agent: 'local-only' }] } },
    ), { status: 200 })))
    render(<PortabilityTab />)
    const input = screen.getByLabelText(/choose import file/i) as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['PK'], 'e.zip')] } })
    const importButton = screen.getByRole('button', { name: /^import$/i })
    await waitFor(() => expect((importButton as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(importButton)
    const warning = await screen.findByTestId('portability-import-warning')
    expect(warning.textContent).toContain('triage \u2192 local-only')
    expect(screen.getByText(/Import complete/)).toBeTruthy()
  })
})
