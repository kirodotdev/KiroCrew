// Which url the report pins its findings onto. The critic is handed the images
// as `![screen](path)` and echoes a `screens[].path` back; that echo is NOT a
// trustworthy file path — it can be the chat placeholder `[image: <id>_name.png]`
// or an invented string, and /api/file-raw then serves nothing, so the preview
// image fails to load and the numbered pins collapse into a pile. `resolveScreens`
// therefore pins onto the url the app already built for each uploaded/rendered
// screen and uses the model only for the (nicer) label.
import { describe, expect, it } from 'vitest'

import { resolveScreens } from '../apps/design-critique/utils'
import type { Report, Screen } from '../apps/design-critique/types'

const url = '/api/file-raw?path=%2FUsers%2Fme%2F.kiro%2Fcrew%2Fuploads%2Fabc_shot.png'
const uploaded: Screen[] = [{ step: 1, label: 'Screen 1', url }]

describe('resolveScreens', () => {
  it('pins onto the uploaded url even when the model echoes a chat placeholder path', () => {
    const rep: Report = { screens: [{ step: 1, label: 'Home', path: '[image: abc_Screenshot_2026-09-11.png' }] }
    const [screen] = resolveScreens(rep, uploaded)
    expect(screen.url).toBe(url)
    expect(screen.url).not.toContain('image%3A')
    // the model's readable label is still used
    expect(screen.label).toBe('Home')
  })

  it('keeps the uploaded label when the model omits one', () => {
    const [screen] = resolveScreens({}, uploaded)
    expect(screen.url).toBe(url)
    expect(screen.label).toBe('Screen 1')
  })

  it('never invents a url from the model when the app has none of its own', () => {
    const rep: Report = { screens: [{ step: 1, label: 'Real', path: '/tmp/render/1.png' }] }
    expect(resolveScreens(rep, [])).toEqual([])
  })
})
