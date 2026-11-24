import { describe, expect, it } from 'vitest'
import { xrView } from './xrView'

describe('the view that an /xr address opens', () => {
  it('is the workspace of pages for plain /xr, for view=pages, and for the other words with no view', () => {
    for (const search of ['', '?view=pages', '?desk=1', '?source=dom', '?fps=1', '?desk=1&source=dom&fps=1', '?view=nothing', '?page=%2Fchat&probe=1']) expect(xrView(search), search).toBe('pages')
  })
  it('is the land only with view=land, or view=aura, the land framed as the glasses show it', () => {
    for (const search of ['?view=land', '?view=land&fixture=1', '?fixture=1&view=land', '?view=aura']) expect(xrView(search), search).toBe('land')
    expect(xrView(new URLSearchParams('view=land'))).toBe('land')
  })
  it('is one Bot\'s dashboard alone only with view=dash', () => {
    for (const search of ['?view=dash', '?view=dash&slug=atlas&camera=taken', '?slug=atlas&view=dash']) expect(xrView(search), search).toBe('dash')
    expect(xrView('?slug=atlas')).toBe('pages')
  })
})
