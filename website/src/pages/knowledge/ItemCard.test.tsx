/**
 * The Knowledge file-group header (`FileSubGroup`) shows the grouped file's
 * name. The group key is the file's gateway path, which on Windows is a native
 * `C:\…` path — a `/`-only split would show it whole — so the header takes the
 * name with `pathBasename` (via `utils/pathBasename`). The `__ungrouped__`
 * sentinel keeps its own "other" label.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { FileSubGroup } from './ItemCard'
import { i18nT } from '../../i18n/t'
import type { KnowledgeItem } from './types'

const item: KnowledgeItem = {
  id: 'i1', title: 'An item', item_type: 'note', status: 'ready',
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
}

function renderGroup(filePath: string) {
  return render(
    <FileSubGroup filePath={filePath} items={[item]} onItemClick={() => {}} selectedItems={new Set()} onSelect={() => {}} />,
  )
}

describe('FileSubGroup header', () => {
  afterEach(() => cleanup())

  it('heads a Windows-path group by the file name, not the whole C:\\ path', () => {
    renderGroup('C:\\notes\\x.md')
    expect(screen.getByText('x.md')).toBeTruthy()
    expect(screen.queryByText('C:\\notes\\x.md')).toBeNull()
  })

  it('heads a POSIX-path group by the file name (existing behaviour unchanged)', () => {
    renderGroup('/home/u/notes/x.md')
    expect(screen.getByText('x.md')).toBeTruthy()
  })

  it('keeps a POSIX name that contains a backslash whole', () => {
    renderGroup('/tmp/we\\ird.md')
    expect(screen.getByText('we\\ird.md')).toBeTruthy()
  })

  it('keeps the ungrouped sentinel label', () => {
    renderGroup('__ungrouped__')
    expect(screen.getByText(i18nT('pages.knowledge.itemCard.other'))).toBeTruthy()
  })
})

describe('FileSubGroup toggle', () => {
  afterEach(() => cleanup())

  it('says whether its group is open and hides its icons from the name', () => {
    renderGroup('/home/u/notes/x.md')
    const toggle = screen.getByRole('button', { name: /x\.md/ })

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    for (const svg of toggle.querySelectorAll('svg')) expect(svg).toHaveAttribute('aria-hidden', 'true')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('An item')).toBeNull()
  })
})
