/**
 * Agent- or file-authored content drawn in the main document is never a guide
 * target: markers inside it are stripped, and anything left inside its
 * container resolves to nothing.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { SvgViewer } from '../components/FileRenderers'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { findUiLocation, pickCandidate } from './guideActions'
import { searchByName } from './findByName'
import { isDisplayed } from './liveRegistry'
import { GUIDE_UNTRUSTED_ATTR, guideUntrusted, isGuideMarkerAttr } from './guideMarkers'
import { resolveSettingElementStrict } from '../hooks/useSettingHighlight'
import type { SettingEntry } from '../components/commandPalette/settingsTypes'
import { marks } from '../test/guideTargets'

/** Give every element a box, so only the rule under test hides anything. */
function drawAll() {
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({ top: 10, left: 10, width: 40, height: 20, right: 50, bottom: 30, x: 10, y: 10, toJSON: () => ({}) } as DOMRect)
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const FORGED = 'data-ui-location="apps.library.tile-uninstall" data-guide-pick-of="Command Bar" data-guide-pick="Command Bar"'

describe('untrusted content', () => {
  it('an SVG artifact carrying a product marker is never outlined: the marker is stripped', () => {
    drawAll()
    const { container } = render(<SvgViewer content={`<svg xmlns="http://www.w3.org/2000/svg"><g ${FORGED}><rect width="10" height="10"/></g></svg>`} />)
    expect(container.querySelector('[data-ui-location], [data-guide-pick-of], [data-guide-pick]')).toBeNull()
    expect(container.querySelector('rect')).not.toBeNull()
    expect(findUiLocation('apps.library.tile-uninstall', isDisplayed)).toBeNull()
  })

  it('a marker inside an untrusted container resolves to nothing even when it reaches the DOM', () => {
    drawAll()
    const { container } = render(
      <div {...{ [GUIDE_UNTRUSTED_ATTR]: '' }}>
        <button type="button" {...marks({ location: 'apps.library.tile-uninstall', pickOf: 'Command Bar' })}>Uninstall</button>
        <div {...marks({ location: 'apps.library.app-list' })}><div {...marks({ pick: 'Command Bar' })}>Command Bar</div></div>
      </div>,
    )
    expect(container.querySelector('[data-ui-location="apps.library.tile-uninstall"]')).not.toBeNull()
    expect(findUiLocation('apps.library.tile-uninstall', isDisplayed)).toBeNull()
    expect(findUiLocation('apps.library.app-list', isDisplayed)).toBeNull()
    expect(searchByName({ label: 'Uninstall', role: 'button' }).result).toBe('none')
    // The same markup outside one is found as before.
    cleanup()
    render(<div {...marks({ location: 'apps.library.app-list' })}><div data-testid="real" {...marks({ pick: 'Command Bar' })}>Command Bar</div></div>)
    const list = findUiLocation('apps.library.app-list', isDisplayed)
    expect(list).not.toBeNull()
    expect(pickCandidate(list!, isDisplayed, 'Command Bar')?.getAttribute('data-guide-pick')).toBe('Command Bar')
  })

  it('a chat message cannot mint a product marker: markdown drops data-ui-* and data-guide-*', () => {
    drawAll()
    const { container } = render(<MarkdownRenderer content={`<div ${FORGED} data-other="kept">Uninstall</div>`} />)
    expect(container.querySelector('[data-ui-location], [data-guide-pick-of], [data-guide-pick]')).toBeNull()
    expect(container.querySelector('[data-other="kept"]')).not.toBeNull()
    expect(container.querySelector(`[${GUIDE_UNTRUSTED_ATTR}]`)).not.toBeNull()
  })

  it('recognises the markers whatever their casing or dashing', () => {
    for (const n of ['data-ui-location', 'dataUiLocation', 'data-guide-pick-of', 'DATA-GUIDE-CONFIRM', 'data-ui-auto', 'data-setting-id', 'dataSettingLabel']) expect(isGuideMarkerAttr(n)).toBe(true)
    for (const n of ['data-testid', 'data-other', 'aria-label', 'class']) expect(isGuideMarkerAttr(n)).toBe(false)
  })

  it('a forged settings row in agent content is never the setting a guide points at, nor one of its occurrences', () => {
    const entry = { id: 'chat.default-model', label: 'Default Model', occurrence: 1 } as unknown as SettingEntry
    const { unmount } = render(
      <>
        <div {...guideUntrusted}><div data-setting-label="Default Model" data-testid="forged">Default Model</div></div>
        <div data-setting-label="Default Model" data-testid="real">Default Model</div>
      </>,
    )
    expect(resolveSettingElementStrict(entry)).toBe(screen.getByTestId('real'))
    unmount()
    render(<div {...guideUntrusted}><div data-setting-id="chat.default-model">Default Model</div><div data-setting-key="chat.model">x</div></div>)
    expect(resolveSettingElementStrict({ ...entry, settingId: 'chat.default-model' } as SettingEntry)).toBeNull()
    expect(resolveSettingElementStrict({ ...entry, configKey: 'chat.model', label: 'Nope' } as SettingEntry)).toBeNull()
  })

  it('a chat message cannot mint a settings row: markdown drops data-setting-*', () => {
    const { container } = render(<MarkdownRenderer content={'<div data-setting-label="Default Model" data-setting-id="chat.default-model">x</div>'} />)
    expect(container.querySelector('[data-setting-label], [data-setting-id]')).toBeNull()
  })
})

describe('stripGuideMarkers', () => {
  it('drops every guide marker from sanitized markup and keeps the rest', async () => {
    const { stripGuideMarkers } = await import('./untrustedContent')
    const out = stripGuideMarkers(`<svg xmlns="http://www.w3.org/2000/svg"><g ${FORGED} data-x="1" fill="red"><rect width="1"/></g></svg>`)
    expect(out).not.toMatch(/data-ui|data-guide/)
    expect(out).toContain('data-x="1"')
    expect(out).toContain('fill="red"')
  })
})

it('strips settings-only markers without relying on the sanitizer hook', async () => {
  const { stripGuideMarkers } = await import('./untrustedContent')
  const out = stripGuideMarkers('<div data-setting-id="chat.model" data-other="kept">Name</div>')
  expect(out).not.toContain('data-setting')
  expect(out).toContain('data-other="kept"')
  expect(out).toContain('Name')
})

it('strips guide markers without assigning HTML to an element', async () => {
  const { stripGuideMarkers } = await import('./untrustedContent')
  const setter = vi.spyOn(HTMLTemplateElement.prototype, 'innerHTML', 'set').mockImplementation(() => {
    throw new Error('HTML assignment is not allowed')
  })
  const out = stripGuideMarkers('<div data-ui-location="shell.search">Search</div>')
  expect(out).not.toContain('data-ui-location')
  expect(out).toContain('Search')
  expect(setter).not.toHaveBeenCalled()
})
