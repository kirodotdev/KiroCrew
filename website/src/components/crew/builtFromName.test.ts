import { describe, expect, it } from 'vitest'
import { kitTemplateName, templateDisplayName } from './builtFromName'

describe('templateDisplayName', () => {
  it('labels the default template and the source-defined agents a crewmate runs on as the default', () => {
    for (const id of ['kirocrew', 'kirocrew-mate-conductor', 'kirocrew-mate-worker', '']) {
      expect(templateDisplayName(id)).toBe('kirocrew (default)')
    }
  })

  it('keeps a template the user picked under its own name', () => {
    expect(templateDisplayName('release-captain')).toBe('release-captain')
  })
})

describe('kitTemplateName', () => {
  it('names a private copy by the template it was forked from, never by its id', () => {
    expect(kitTemplateName({ template: 'crew-5eed01', origin: 'release-helper', private_copy: true })).toBe('release-helper')
    expect(kitTemplateName({ template: 'crew-5eed01', origin: '', private_copy: true })).toBe('Its own copy')
  })

  it('names any other kit by its template, and no kit as none', () => {
    expect(kitTemplateName({ template: 'release-helper', origin: 'release-helper', private_copy: false })).toBe('release-helper')
    expect(kitTemplateName({ template: '', origin: '', private_copy: false })).toBeNull()
    expect(kitTemplateName(undefined)).toBeNull()
  })
})
