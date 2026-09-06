import { describe, expect, it } from 'vitest'

describe('NotificationsBellButton module boundary', () => {
  it('exports the topbar notification bell component', async () => {
    const module = await import('../components/NotificationsBell')
    expect(module.NotificationsBellButton).toBeTypeOf('function')
  })
})
