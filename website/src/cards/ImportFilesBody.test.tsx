import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { Card, CrewmateImportFiles } from '../api/cards'

/* A `crewmate.import` card shows every carried file in full (rfc-crewmate-share.md
 * §5.4), read from the local backend's staged copy; text stays text. */

const importFiles = vi.fn<(sha: string) => Promise<CrewmateImportFiles>>()
vi.mock('../api/cards', async (orig) => {
  const real = await orig<typeof import('../api/cards')>()
  return { ...real, cardsApi: { ...real.cardsApi, importFiles: (s: string) => importFiles(s) } }
})

import { ImportFilesBody } from './ImportFilesBody'

const card = (over: Partial<Card> = {}): Card => ({
  id: 'cc_1', slot_key: 'owner:crewmate-import', kind: 'crewmate.import', revision: 1, status: 'pending',
  risk: 'code_exec', title: 'Import', changes: [], editable: ['name'],
  params: { bundle_sha256: 'a'.repeat(64), carried: { scripts: [] } }, plan: { apply: [], undo: null },
  created_at: 0, expires_at: 0, ...over,
})

function mount(c: Card) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><ImportFilesBody card={c} disabled={false} /></QueryClientProvider>)
}

describe('ImportFilesBody', () => {
  it('shows each skill and script file in full, as text', async () => {
    importFiles.mockResolvedValue({
      skills: { notes: { 'SKILL.md': '# notes', 'tool.py': '<script>alert(1)</script>' } },
      scripts: { 'run.py': 'def main():\n    return 1\n' },
      avatar: 'data:image/png;base64,AAAA',
    })
    mount(card())
    expect((await screen.findAllByTestId('import-file')).length).toBe(3)
    expect(importFiles).toHaveBeenCalledWith('a'.repeat(64))
    expect(screen.getByText('<script>alert(1)</script>')).toBeTruthy()
    expect(document.querySelector('script')).toBeNull()
    expect(screen.getByText(/def main\(\):/).textContent).toBe('def main():\n    return 1\n')
    expect(screen.getByTestId('import-avatar').getAttribute('src')).toBe('data:image/png;base64,AAAA')
  })

  it('reads nothing for a finished card or one that carries no files', () => {
    importFiles.mockClear()
    mount(card({ status: 'applied' }))
    mount(card({ params: { bundle_sha256: 'a'.repeat(64) } }))
    expect(importFiles).not.toHaveBeenCalled()
  })
})
