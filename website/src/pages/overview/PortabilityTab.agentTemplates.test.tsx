import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import PortabilityTab, { bundledTemplateRows, skippedTemplateRows } from './PortabilityTab'

function stubDownload(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = () => 'blob:x'
    static revokeObjectURL = () => {}
  })
}

const zipResponse = () => new Response(new Blob(['PK']), { status: 200 })
const manifest = { version: 2, created_at: 't', hostname: 'h', user: 'u', contents: {} }

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('bundledTemplateRows', () => {
  it('keeps named rows and drops anything malformed', () => {
    expect(bundledTemplateRows([{ name: 'a', exists: true }, { name: 'b' }, { name: 3 }, null, 'c']))
      .toEqual([{ name: 'a', exists: true }, { name: 'b', exists: false }])
    expect(bundledTemplateRows(undefined)).toEqual([])
  })
})

describe('skippedTemplateRows', () => {
  it('keeps each named row, reads an unknown reason as invalid, and drops the rest', () => {
    expect(skippedTemplateRows([
      { name: 'a', reason: 'exists' },
      { name: 'b', reason: 'mystery' },
      { reason: 'x' },
      7,
    ])).toEqual([{ name: 'a', reason: 'exists' }, { name: 'b', reason: 'invalid' }])
    expect(skippedTemplateRows('a')).toEqual([])
  })
})

describe('PortabilityTab agent templates on export', () => {
  it('is off by default, so the export request is unchanged', async () => {
    const fetchMock = vi.fn(async () => zipResponse())
    stubDownload(fetchMock)
    render(<PortabilityTab />)
    const box = screen.getByTestId('portability-include-agent-templates') as HTMLInputElement
    expect(box.checked).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: /download export/i }))
    await screen.findByText('Download started.')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/portability/export')
  })

  it('asks for the templates when ticked', async () => {
    const fetchMock = vi.fn(async () => zipResponse())
    stubDownload(fetchMock)
    render(<PortabilityTab />)
    fireEvent.click(screen.getByRole('checkbox', { name: /include agent templates/i }))
    fireEvent.click(screen.getByRole('button', { name: /download export/i }))
    await screen.findByText('Download started.')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/portability/export?include_agent_templates=true')
  })

  it('warns about templates left out even when only a count is known', async () => {
    stubDownload(vi.fn(async () => new Response(new Blob(['PK']), {
      status: 200,
      headers: { 'X-Kirocrew-Unbundled-Templates': '["+2"]' },
    })))
    render(<PortabilityTab />)
    fireEvent.click(screen.getByRole('checkbox', { name: /include agent templates/i }))
    fireEvent.click(screen.getByRole('button', { name: /download export/i }))
    const warning = await screen.findByTestId('portability-export-warning')
    expect(warning.textContent).toContain('2 more')
  })

  it('is unavailable for a memory export', () => {
    stubDownload(vi.fn(async () => zipResponse()))
    render(<PortabilityTab />)
    fireEvent.click(screen.getByTestId('portability-include-agent-templates'))
    fireEvent.click(screen.getByRole('checkbox', { name: /export memory only/i }))
    const box = screen.getByTestId('portability-include-agent-templates') as HTMLInputElement
    expect(box.disabled).toBe(true)
    expect(box.checked).toBe(false)
    expect(document.getElementById('portability-include-templates-hint')?.textContent)
      .toBe('A memory export never carries agent templates.')
  })
})

describe('PortabilityTab agent templates on import', () => {
  function stubImport(summary: object) {
    const fetchMock = vi.fn(async (url: string) => new Response(JSON.stringify(
      url.includes('preview')
        ? {
          ok: true,
          manifest,
          agent_templates: [{ name: 'helper', exists: true }, { name: 'researcher', exists: false }],
        }
        : { ok: true, summary: { items: ['config (merged)'], ...summary } },
    ), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    return fetchMock
  }

  async function chooseArchive() {
    render(<PortabilityTab />)
    const input = screen.getByLabelText(/choose import file/i) as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['PK'], 'e.zip')] } })
    return screen.findByTestId('portability-preview-templates')
  }

  it('lists the bundled templates unticked, and one already here cannot be ticked', async () => {
    stubImport({})
    const list = await chooseArchive()
    const helper = within(list).getByRole('checkbox', { name: /helper/ }) as HTMLInputElement
    const researcher = within(list).getByRole('checkbox', { name: /researcher/ }) as HTMLInputElement
    expect(helper.disabled).toBe(true)
    expect(list.textContent).toContain('Already installed')
    expect(researcher.disabled).toBe(false)
    expect(researcher.checked).toBe(false)
  })

  it('installs none unless picked', async () => {
    const fetchMock = stubImport({})
    await chooseArchive()
    const importButton = screen.getByRole('button', { name: /^import$/i })
    await waitFor(() => expect((importButton as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(importButton)
    await screen.findByText(/Import complete/)
    const importUrl = String(fetchMock.mock.calls.at(-1)?.[0])
    expect(importUrl).not.toContain('agent_templates')
  })

  it('sends only the picked templates and reports what was installed and skipped', async () => {
    const fetchMock = stubImport({
      agent_templates_installed: ['researcher'],
      agent_templates_skipped: [{ name: 'helper', reason: 'exists' }],
    })
    const list = await chooseArchive()
    fireEvent.click(within(list).getByRole('checkbox', { name: /researcher/ }))
    const importButton = screen.getByRole('button', { name: /^import$/i })
    await waitFor(() => expect((importButton as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(importButton)
    await screen.findByText(/Import complete/)
    const importUrl = String(fetchMock.mock.calls.at(-1)?.[0])
    expect(new URL(importUrl, 'http://x').searchParams.get('agent_templates')).toBe('researcher')
    const status = (await screen.findAllByTestId('portability-import-chats')).map(w => w.textContent)
    expect(status.join('\n')).toContain('Installed agent templates: researcher')
    const warnings = (await screen.findAllByTestId('portability-import-warning')).map(w => w.textContent)
    expect(warnings.join('\n')).not.toContain('Installed agent templates')
    expect(warnings.join('\n')).toContain('helper (already installed)')
  })

  it('shows a template that could not be written as an error, not a warning', async () => {
    stubImport({
      agent_templates_installed: [],
      agent_templates_skipped: [{ name: 'researcher', reason: 'unwritable' }, { name: 'helper', reason: 'exists' }],
    })
    const list = await chooseArchive()
    fireEvent.click(within(list).getByRole('checkbox', { name: /researcher/ }))
    const importButton = screen.getByRole('button', { name: /^import$/i })
    await waitFor(() => expect((importButton as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(importButton)
    await screen.findByText(/Import complete/)
    const errors = await screen.findAllByTestId('portability-import-refused')
    expect(errors.map(e => e.textContent).join('\n')).toContain('researcher (could not be written)')
    const warnings = screen.getAllByTestId('portability-import-warning').map(w => w.textContent).join('\n')
    expect(warnings).toContain('helper (already installed)')
    expect(warnings).not.toContain('researcher')
  })

  it('says how many templates sit past the archive limit', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      ok: true,
      manifest,
      agent_templates: [{ name: 'a', exists: false }],
      agent_templates_more: 3,
    }), { status: 200 })))
    const list = await chooseArchive()
    expect(within(list).getByTestId('portability-preview-templates-more').textContent).toContain('3')
  })
})
