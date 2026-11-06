/**
 * The Mochi chat composer has a steady accessible name. Its placeholder rotates
 * through tips ("Drink water", a shortcut, ...), so without a label a screen
 * reader would announce whichever tip is showing as the field's name.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'

vi.mock('../apps/mochi/src/mochiApi', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'getMochiConfig') return async () => ({ petName: 'Mochi', theme: 'mocha' })
      if (p === 'getConfig') return async () => ({ shortcuts: { toggleWindow: 'A', screenCapture: 'B', hideAll: 'C' } })
      if (p === 'getPetStateInfo') return async () => ({ state: 'idle', mood: 'happy' })
      if (p === 'getChatHistory') return async () => []
      if (p === 'getBackendStatus') return async () => true
      if (p.startsWith('on')) return () => () => {}
      return async () => undefined
    },
  }),
}))

const { ChatPanel } = await import('../apps/mochi/src/renderer/ChatPanel')
const { i18next } = await import('../i18n/all')
const zh = (await import('../i18n/locales/zh-CN.json')).default

describe('Mochi chat composer name', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('names the composer after the pet, not the rotating tip', async () => {
    render(<ChatPanel />)
    await screen.findByText(/Idle/)
    const box = screen.getByRole('textbox', { name: 'Message Mochi' })
    expect(box.tagName).toBe('TEXTAREA')
  })

  it('takes the name from the catalog in another language', async () => {
    await i18next.changeLanguage('zh-CN')
    render(<ChatPanel />)
    const name = zh.apps.mochi.chat.message_label.replace('{{name}}', 'Mochi')
    expect(await screen.findByRole('textbox', { name })).toBeInTheDocument()
  })
})
