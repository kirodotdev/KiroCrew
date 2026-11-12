// The folder toggle is named by one catalog sentence with the folder name in
// it, so a verb-final language can put the name before the verb.
import { render, screen, act } from '@testing-library/react'
import CronFolderHeader from './CronFolderHeader'
import { Table, TableBody } from './ui/table'
import { i18next } from '../i18n/all'

const folder = { id: 'f1', name: 'Nightly', order: 1 }

function setup(collapsed: boolean) {
  render(
    <Table>
      <TableBody>
        <CronFolderHeader
          folder={folder}
          jobCount={2}
          collapsed={collapsed}
          colSpan={4}
          onToggleCollapse={vi.fn()}
          onRename={vi.fn()}
          onDelete={vi.fn()}
        />
      </TableBody>
    </Table>,
  )
}

describe('CronFolderHeader toggle name', () => {
  afterEach(async () => {
    await act(async () => { await i18next.changeLanguage('en') })
  })

  it('reads as before in English', () => {
    setup(true)
    expect(screen.getByRole('button', { name: 'Expand folder Nightly' })).toBeInTheDocument()
  })

  it('puts the name before the verb in German', async () => {
    await act(async () => { await i18next.changeLanguage('de') })
    setup(false)
    expect(screen.getByRole('button', { name: 'Ordner Nightly einklappen' })).toBeInTheDocument()
  })

  it('puts the name before the verb in Japanese', async () => {
    await act(async () => { await i18next.changeLanguage('ja') })
    setup(true)
    expect(screen.getByRole('button', { name: 'フォルダー「Nightly」を展開' })).toBeInTheDocument()
  })
})
