import { describe, it, expect, afterEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import type { PostureControl } from '../../api/client'
import { i18next } from '../../i18n/all'
import { renderWithProviders } from '../../test/helpers'
import { PostureDisclosureRow } from './PostureDisclosure'

/* The row's name, accessible name and count badge are built in the user's
 * language. The server sends `label` and `unit` in English, so the wording comes
 * from the catalog by the control's stable `key`, with the server's text kept
 * only as the fallback for a key this build does not know yet. */

function control(over: Partial<PostureControl> = {}): PostureControl {
  return {
    key: 'denied_commands',
    label: 'Denied commands',
    unit: 'built-in rules',
    summary: '',
    source: '',
    count: 2,
    items: [{ label: 'rm -rf /', detail: '' }, { label: 'curl | bash', detail: '' }],
    unavailable: false,
    ...over,
  }
}

function header() {
  return screen.getByRole('button', { expanded: false })
}

afterEach(async () => {
  await act(async () => { await i18next.changeLanguage('en') })
})

describe('PostureDisclosureRow — language of the row', () => {
  it('names the row, its control and its count in the UI language, not English', async () => {
    await act(async () => { await i18next.changeLanguage('ja') })
    renderWithProviders(<PostureDisclosureRow control={control()} icon={null} />)

    expect(header()).toHaveAttribute('aria-label', '拒否されるコマンド の詳細を表示 — 2 件の組み込みルール')
    expect(screen.getByText('拒否されるコマンド')).toBeInTheDocument()
    expect(screen.getByText('2 件の組み込みルール')).toBeInTheDocument()
    expect(screen.queryByText(/built-in rules|Denied commands/)).not.toBeInTheDocument()
  })

  it('translates the unavailable wording too', async () => {
    await act(async () => { await i18next.changeLanguage('de') })
    renderWithProviders(<PostureDisclosureRow control={control()} icon={null} countOverride={null} />)

    expect(header()).toHaveAttribute(
      'aria-label',
      'Details anzeigen: Verbotene Befehle — derzeit nicht verfügbar',
    )
  })

  it('uses the singular for one item in English', () => {
    renderWithProviders(<PostureDisclosureRow control={control()} icon={null} countOverride={1} />)

    expect(header()).toHaveAttribute('aria-label', 'Show Denied commands details — 1 built-in rule')
    expect(screen.getByText('1 built-in rule')).toBeInTheDocument()
  })

  it('falls back to the server label and unit for a control this build has no wording for', () => {
    renderWithProviders(
      <PostureDisclosureRow control={control({ key: 'brand_new', label: 'Brand new', unit: 'widgets' })} icon={null} />,
    )

    expect(header()).toHaveAttribute('aria-label', 'Show Brand new details — 2 widgets')
    expect(screen.getByText('Brand new')).toBeInTheDocument()
    expect(screen.getByText('2 widgets')).toBeInTheDocument()
  })
})
