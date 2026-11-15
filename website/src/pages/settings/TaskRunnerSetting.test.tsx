/**
 * Settings > Display > View: Task Runner's switch, which replaced turning the
 * built-in app `projects` on and off in Library.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import { APP_NAV_HIDDEN_KEY, readAppNavHidden } from '../../lib/appNavHidden'

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn((_path: string, _value: unknown) => Promise.resolve({})),
  kirocrewConfigMock: vi.fn((): Promise<unknown> => Promise.resolve({})),
}))
vi.mock('../../api/client', () => ({
  api: { kirocrewConfig: kirocrewConfigMock, patchConfig: patchConfigMock },
}))

import { TaskRunnerSetting } from './TaskRunnerSetting'

const toggle = () => screen.findByRole('switch', { name: 'Show Task Runner' })

describe('TaskRunnerSetting', () => {
  beforeEach(() => {
    localStorage.clear()
    patchConfigMock.mockClear()
    kirocrewConfigMock.mockReset()
  })

  it('shows the setting as the gateway holds it and turns Task Runner off', async () => {
    kirocrewConfigMock.mockResolvedValue({ taskrunner: { enabled: true } })
    renderWithProviders(<TaskRunnerSetting />)
    const row = await toggle()
    expect(row).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(row)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('taskrunner.enabled', false))
  })

  it('a user who turned the app off sees it off, and can turn it back on', async () => {
    kirocrewConfigMock.mockResolvedValue({ taskrunner: { enabled: false } })
    renderWithProviders(<TaskRunnerSetting />)
    const row = await toggle()
    expect(row).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(row)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('taskrunner.enabled', true))
  })

  it('a row unpinned from the sidebar in Library still shows the setting, and turning it on pins it back', async () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(['projects', 'mochi']))
    kirocrewConfigMock.mockResolvedValue({ taskrunner: { enabled: false } })
    renderWithProviders(<TaskRunnerSetting />)
    const row = await toggle()
    expect(row).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(row)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('taskrunner.enabled', true))
    expect([...readAppNavHidden()]).toEqual(['mochi'])
  })

  it('an unpinned row does not make a Task Runner that is on read as off', async () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, JSON.stringify(['projects']))
    kirocrewConfigMock.mockResolvedValue({ taskrunner: { enabled: true } })
    renderWithProviders(<TaskRunnerSetting />)
    expect(await toggle()).toHaveAttribute('aria-checked', 'true')
  })

  it('draws nothing for a gateway whose config has no such key', async () => {
    kirocrewConfigMock.mockResolvedValue({ taskrunner: {} })
    const { container } = renderWithProviders(<TaskRunnerSetting />)
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    expect(container.querySelector('[role="switch"]')).toBeNull()
  })
})
