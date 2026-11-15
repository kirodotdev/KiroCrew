// The tool-group toggle is named by one catalog sentence with the group's
// label in it, so a verb-final language can put the label before the verb.
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'
import { i18next } from '../i18n/all'

describe('CollapsibleToolGroup toggle name', () => {
  afterEach(async () => {
    await act(async () => { await i18next.changeLanguage('en') })
  })

  it('reads as before in English', () => {
    render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    expect(screen.getByRole('button', { name: 'Expand 2 tool calls' })).toBeInTheDocument()
  })

  it('puts the label before the verb in German', async () => {
    await act(async () => { await i18next.changeLanguage('de') })
    render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    expect(screen.getByRole('button', { name: '2 Werkzeugaufrufe ausklappen' })).toBeInTheDocument()
  })

  it('puts the label before the verb in Japanese', async () => {
    await act(async () => { await i18next.changeLanguage('ja') })
    render(<CollapsibleToolGroup count={2} autoExpand><div>row</div></CollapsibleToolGroup>)
    expect(screen.getByRole('button', { name: '「ツール呼び出し 2 件」を折りたたむ' })).toBeInTheDocument()
  })
})
