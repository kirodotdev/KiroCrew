import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen } from '@testing-library/react'
import React from 'react'
import { renderWithProviders } from '../test/helpers'
import HookSkillsDropdown from './HookSkillsDropdown'
import { i18next } from '../i18n/all'
import zh from '../i18n/locales/zh-CN.json'

// The hook skills dropdown's filter box has an accessible name. A placeholder
// is not one: it vanishes once text is typed, and assistive tech may skip it.

vi.mock('react-dom', async (importOriginal) => {
  const mod = await importOriginal<typeof import('react-dom')>()
  return { ...mod, createPortal: (children: React.ReactNode) => children }
})

function anchorRef() {
  const el = document.createElement('button')
  el.getBoundingClientRect = () => ({ top: 0, left: 0, bottom: 32, right: 100, width: 100, height: 32, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  document.body.appendChild(el)
  return { current: el }
}

function renderDropdown() {
  renderWithProviders(
    <HookSkillsDropdown
      anchorRef={anchorRef()}
      dropdownRef={{ current: null } as React.RefObject<HTMLDivElement | null>}
      inputRef={{ current: null } as React.RefObject<HTMLInputElement | null>}
      filter=""
      setFilter={vi.fn()}
      onClose={vi.fn()}
      selected={[]}
      filtered={[{ key: 'dev/pod-e2e', name: 'pod-e2e' }]}
      byKey={new Map()}
      onAdd={vi.fn()}
      onRemove={vi.fn()}
    />,
  )
}

describe('HookSkillsDropdown filter name', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('names the filter box', () => {
    renderDropdown()
    expect(screen.getByRole('textbox', { name: 'Filter skills' })).toBeInTheDocument()
  })

  it('takes the name from the catalog in another language', async () => {
    await i18next.changeLanguage('zh-CN')
    renderDropdown()
    expect(screen.getByRole('textbox', { name: zh.components.agentSkillsEditor.filter_skills })).toBeInTheDocument()
  })
})
