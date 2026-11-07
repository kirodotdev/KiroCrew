// The footer's "Missing: …" hint names every state still lacking a frame as one
// list. That list must join the way the active language joins items (de:
// "x, y und z", zh-CN: "x、y和z"), not with a hard-coded English comma (#32038).
import { render, screen } from '@testing-library/react'
import React from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import { EditorFooter } from '../src/renderer/EditorFooter'
// `/all` registers the de and zh-CN catalogs; `../i18n` registers English only.
import { i18next } from '../../../i18n/all'

const noop = () => {}
const previous = i18next.language
afterEach(async () => { await i18next.changeLanguage(previous) })

describe('EditorFooter — the missing-state list follows the locale', () => {
  // Unknown state ids render as themselves, so the expected text pins only the
  // list joining and the catalog's own "Missing:" wording.
  it.each([
    ['en', 'Missing: x, y, and z'],
    ['de', 'Fehlt: x, y und z'],
    ['zh-CN', '缺少： x、y和z'],
  ])('joins the states the way %s does', async (lng, expected) => {
    await i18next.changeLanguage(lng)
    render(<EditorFooter missingStates={['x', 'y', 'z']} canSave={false} onCancel={noop} onSave={noop} />)
    expect(screen.getByText(expected)).toBeTruthy()
  })
})
