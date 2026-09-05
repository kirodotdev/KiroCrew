import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MigratedBadge } from './MigratedBadge'

// Requirement 7.3: the tombstone must be discoverable from the surface that
// listed the unit before the move. Without this the Schedule page renders a
// migrated job as an ordinary paused one -- cron persists enabled=false (so the
// double-fire guard holds) but derives user_paused from it on reload, leaving
// nothing that says the work now lives on another crew.

describe('MigratedBadge', () => {
  it('names the crew and the remote id', () => {
    render(
      <MigratedBadge
        migratedTo={{
          crew_id: 'remote-ec2',
          label: 'EC2 box',
          remote_unit_id: 'cron-77',
          migrated_ts: 5,
        }}
      />,
    )
    expect(screen.getByText(/migrated to/i)).toBeInTheDocument()
    expect(screen.getByText(/EC2 box/)).toBeInTheDocument()
    expect(screen.getByText(/cron-77/)).toBeInTheDocument()
  })

  it('falls back to the crew id when there is no label', () => {
    render(
      <MigratedBadge
        migratedTo={{ crew_id: 'remote-ec2', remote_unit_id: 'cron-77', migrated_ts: 5 }}
      />,
    )
    expect(screen.getByText(/remote-ec2/)).toBeInTheDocument()
  })

  it('renders nothing for a job that did not move', () => {
    const { container } = render(<MigratedBadge migratedTo={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when the field is absent', () => {
    const { container } = render(<MigratedBadge migratedTo={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('is announced to assistive tech, not colour-only', () => {
    render(
      <MigratedBadge
        migratedTo={{ crew_id: 'remote-ec2', remote_unit_id: 'cron-77', migrated_ts: 5 }}
      />,
    )
    // the redirect is real information, so it must carry a text label a screen
    // reader reaches -- an icon or a colour alone would hide it
    expect(screen.getByRole('note')).toHaveAccessibleName(/migrated to remote-ec2/i)
  })
})
