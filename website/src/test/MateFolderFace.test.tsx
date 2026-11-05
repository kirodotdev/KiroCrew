/**
 * A mate's home folder row wears the mate's face (#25935).
 *
 *   - A plain folder, and a folder marked home without a member owner: the glyph.
 *   - A home folder whose store the roster lists: the mate's avatar.
 *   - A home folder the roster does not list: the glyph.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ChatFolder } from '../types'

const ROWS = [{ name: 'Radar', memory_store: 'member-radar-0123', avatar: undefined, running: true }]
vi.mock('../api/membersQuery', () => ({
  membersRosterQuery: { queryKey: ['members-roster-test'], queryFn: () => Promise.resolve(ROWS) },
}))

import { MateFolderFace, mateStoreOfFolder } from '../pages/chat-sidebar/MateFolderFace'

function draw(folder: ChatFolder) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MateFolderFace folder={folder} open={false} testId="face" />
    </QueryClientProvider>,
  )
  return qc
}

const home = (store: string): ChatFolder => ({
  id: 'h', name: 'Radar', order: 0, mate_home: true, owner_app: `member:${store}`,
})

describe('MateFolderFace', () => {
  it('reads the store only from a home folder a member owns', () => {
    expect(mateStoreOfFolder(home('member-radar-0123'))).toBe('member-radar-0123')
    expect(mateStoreOfFolder({ id: 'p', name: 'Ops', order: 0 })).toBe('')
    expect(mateStoreOfFolder({ id: 'a', name: 'App', order: 0, mate_home: true, owner_app: 'some-app' })).toBe('')
  })

  it('draws the plain glyph for a person folder', () => {
    draw({ id: 'p', name: 'Ops', order: 0 })
    expect(screen.getByTestId('face').getAttribute('data-mate-folder')).toBeNull()
  })

  it('draws the mate face once the roster lists its store', async () => {
    draw(home('member-radar-0123'))
    await waitFor(() => expect(screen.getByTestId('face').getAttribute('data-mate-folder')).toBe('Radar'))
  })

  it('keeps the plain glyph for a store the roster does not list', async () => {
    const qc = draw(home('member-gone-9999'))
    await waitFor(() => expect(qc.getQueryData(['members-roster-test'])).toBe(ROWS))
    expect(screen.getByTestId('face').getAttribute('data-mate-folder')).toBeNull()
  })
})
