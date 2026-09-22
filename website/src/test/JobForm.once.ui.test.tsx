import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import { defaultFireLocal } from '../components/ScheduleLaterPopover'

vi.mock('../api/client', () => ({
  api: {
    models: vi.fn().mockResolvedValue([]),
    createCron: vi.fn().mockResolvedValue({ ok: true }),
    updateCron: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

describe('JobForm Run once strict setting', () => {
  it('does not enable Strict schedule when a new job selects Run once', async () => {
    const now = Date.parse('2026-09-17T10:07:30Z')
    vi.spyOn(Date, 'now').mockReturnValue(now)
    renderWithProviders(
      <JobForm agents={[]} defaultAgent="" onSaved={vi.fn()} layout="vertical" />,
    )

    fireEvent.click(screen.getByRole('combobox', { name: 'Schedule' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Run once' }))

    expect(screen.getByRole('switch', { name: 'Strict schedule' })).toHaveAttribute(
      'aria-checked',
      'false',
    )

    const scheduleFields = screen.getByTestId('jobform-schedule-fields')
    const onceRow = screen.getByTestId('jobform-run-once-row')
    const picker = screen.getByLabelText('Run once')
    expect(getComputedStyle(scheduleFields).gridTemplateColumns).toBe('minmax(0, 1fr)')
    expect(getComputedStyle(onceRow).gridColumn).toBe('1 / -1')
    expect(getComputedStyle(onceRow).width).toBe('100%')
    expect(picker).toHaveClass('w-full', 'min-w-0')
    expect(picker).toHaveValue(defaultFireLocal(now))
  })
})
