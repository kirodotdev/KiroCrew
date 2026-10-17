/** A disabled crew's sidebar group says so and offers Enable */
import { describe, it, expect, vi } from 'vitest'
import { useState } from 'react'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { CrewGroupSection, useCrewEnable } from '../pages/chat-sidebar/CrewGroups'
import type { CrewGroup } from '../hooks/useInstanceSessions'

const group = (over: Partial<CrewGroup> = {}): CrewGroup => ({
  id: 'astro', name: 'astro', badge: 'offline', offline: true, disabled: false, ...over,
})

function renderGroup(g: CrewGroup, onEnable = vi.fn()) {
  renderWithProviders(
    <CrewGroupSection group={g} rows={[]} collapsed={false} onToggle={() => {}}
      hideWhenEmpty={false} chevron={null} renderRows={() => null} onEnable={onEnable} />,
  )
  return onEnable
}

describe('CrewGroupSection disabled crew', () => {
  it('shows the Disabled badge and an Enable action that names the crew', async () => {
    const onEnable = renderGroup(group({ badge: 'disabled', disabled: true }))
    expect(screen.getByTestId('crew-group-badge-astro')).toHaveTextContent(/^Off$/)
    await userEvent.setup().click(screen.getByRole('button', { name: 'Enable astro' }))
    expect(onEnable).toHaveBeenCalledWith('astro')
  })

  it('offers no Enable action to a crew that is only offline', () => {
    renderGroup(group())
    expect(screen.queryByTestId('crew-group-enable-astro')).toBeNull()
    expect(screen.getByTestId('crew-group-badge-astro')).toHaveTextContent('Offline')
  })

})

/** The sidebar's shape: the notice sits outside the groups, and the group
 *  vanishes once the flag is cleared. */
function Sidebar({ enable }: { enable: (id: string) => Promise<unknown> }) {
  const [gone, setGone] = useState(false)
  const { onEnable, notice } = useCrewEnable(
    id => enable(id).finally(() => setGone(true)),
    () => 'astro',
  )
  return (
    <>
      {notice}
      {!gone && (
        <CrewGroupSection group={group({ badge: 'disabled', disabled: true })} rows={[]} collapsed={false}
          onToggle={() => {}} hideWhenEmpty={false} chevron={null} renderRows={() => null} onEnable={onEnable} />
      )}
    </>
  )
}

describe('useCrewEnable', () => {
  it('keeps a failed enable on screen after the crew group drops out', async () => {
    renderWithProviders(<Sidebar enable={vi.fn().mockRejectedValue(new Error('host unreachable'))} />)
    await userEvent.setup().click(screen.getByRole('button', { name: 'Enable astro' }))
    const notice = await screen.findByTestId('crew-enable-error')
    expect(notice).toHaveTextContent('host unreachable')
    expect(notice).toHaveTextContent('Could not enable astro')
    expect(screen.queryByTestId('crew-group-astro')).toBeNull()
  })

  it('lets the owner dismiss a failed enable', async () => {
    renderWithProviders(<Sidebar enable={vi.fn().mockRejectedValue(new Error('host unreachable'))} />)
    const u = userEvent.setup()
    await u.click(screen.getByRole('button', { name: 'Enable astro' }))
    const notice = await screen.findByTestId('crew-enable-error')
    await u.click(within(notice).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('crew-enable-error')).toBeNull()
  })
})
