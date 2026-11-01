import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

import { ResourcePostureChip } from '../shell/topbar/resourcePostureChip'
// `/all` for every catalog: `../i18n` registers English only.
import { i18next } from '../i18n/all'

/**
 * The capsule's resource posture chip ("Tight · cap: 32"), rendered for real.
 *
 * It used to be a plain span with only a `title`: a keyboard could not reach it,
 * a touch screen could not open the title, and nothing said what "cap" meant. It is now a button whose tip opens on hover, keyboard focus and a
 * tap, and whose description says what the posture and the cap mean.
 */

const SEG = 'seg'

function chip(props: Partial<Parameters<typeof ResourcePostureChip>[0]> = {}) {
  return render(
    <ResourcePostureChip posture="tight" availableGb={2.1} subagentCap={8} seg={SEG} {...props} />,
  )
}

afterEach(async () => {
  await i18next.changeLanguage('en')
})

describe('ResourcePostureChip', () => {
  it('is a button a keyboard can reach', () => {
    chip()
    const button = screen.getByRole('button', { name: 'Tight' })
    button.focus()
    expect(document.activeElement).toBe(button)
    expect(button.getAttribute('title')).toBeNull()
  })

  it('says what the posture and the cap mean, in its description', () => {
    chip()
    expect(screen.getByRole('button', { name: 'Tight' })).toHaveAccessibleDescription(
      'Memory is low on this computer (2.1 GB free). Heavy work may be slower. '
        + 'Up to 8 subagents can run at once right now. Others wait their turn.',
    )
  })

  it('uses the singular for a cap of one', () => {
    chip({ posture: 'critical', availableGb: 0.5, subagentCap: 1 })
    expect(screen.getByRole('button', { name: 'Critical' })).toHaveAccessibleDescription(
      'Memory is very low on this computer (0.5 GB free). Avoid heavy parallel work. '
        + 'Up to 1 subagent can run at once right now. Others wait their turn.',
    )
  })

  it('leaves the cap out when the gateway reports none', () => {
    chip({ subagentCap: null })
    expect(screen.getByRole('button', { name: 'Tight' })).toHaveAccessibleDescription(
      'Memory is low on this computer (2.1 GB free). Heavy work may be slower.',
    )
    expect(screen.queryByText(/cap:/)).toBeNull()
  })

  it('shows the label and the cap', () => {
    chip()
    const button = screen.getByRole('button', { name: 'Tight' })
    expect(button.textContent).toContain('Tight')
    expect(button.textContent).toContain('· cap: 8')
  })

  it('opens its tip on a touch tap', () => {
    chip()
    const button = screen.getByRole('button', { name: 'Tight' })
    // A touch tap's own pointer events, then the replayed mouse events
    // (the `tap()` order in InstantTip.test.tsx).
    act(() => {
      fireEvent.pointerEnter(button, { pointerType: 'touch' })
      fireEvent.pointerDown(button, { pointerType: 'touch' })
      fireEvent.pointerUp(button, { pointerType: 'touch' })
      fireEvent.mouseEnter(button)
      fireEvent.mouseDown(button)
      fireEvent.focus(button)
      fireEvent.mouseUp(button)
      fireEvent.click(button)
    })
    expect(screen.getByRole('tooltip').textContent).toContain('Up to 8 subagents can run at once right now.')
  })

  it('opens its tip on keyboard focus', () => {
    chip()
    act(() => { fireEvent.focus(screen.getByRole('button', { name: 'Tight' })) })
    const tip = screen.getByRole('tooltip')
    expect(tip.textContent).toContain('Memory is low on this computer (2.1 GB free).')
    expect(tip.textContent).toContain('Up to 8 subagents can run at once right now.')
  })

  it('keeps warn styling for tight and danger styling for critical', () => {
    const { unmount } = chip()
    expect(screen.getByTestId('resource-health').className).toContain('text-warn')
    expect(screen.getByTestId('resource-health-dot').className).toContain('bg-warn')
    unmount()
    chip({ posture: 'critical' })
    expect(screen.getByTestId('resource-health').className).toContain('text-danger')
    expect(screen.getByTestId('resource-health-dot').className).toContain('bg-danger')
  })

  it('reads its copy from the catalog', async () => {
    await i18next.changeLanguage('de')
    chip({ subagentCap: 3 })
    const button = screen.getByRole('button', { name: 'Wenig Speicher' })
    expect(button.textContent).toContain('Limit: 3')
    expect(button).toHaveAccessibleDescription(/Gerade können höchstens 3 Subagenten gleichzeitig laufen\./)
  })
})
