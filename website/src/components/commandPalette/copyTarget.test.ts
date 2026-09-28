import { describe, expect, it } from 'vitest'

import { resolveCopyTarget, type CopyableRow } from './copyTarget'

/**
 * The launcher's copy layer.
 *
 * The rules worth pinning are the ones where a wrong answer is invisible: a
 * copied string is not read until it is pasted somewhere else, so a link that
 * leaves this origin, or a `javascript:` value that reaches the clipboard, is
 * found out far from here. The rest of the file pins that a row which can be
 * OPENED can be ADDRESSED -- the property that makes this layer general instead
 * of one entry per use case.
 */

const DEPS = {
  origin: 'https://dash.example',
  sessionLink: (key: string, title?: string) =>
    `https://dash.example/chat${title ? '/' + title : ''}?sid=${key}`,
}

const resolve = (row: CopyableRow) => resolveCopyTarget(row, DEPS)

describe('resolveCopyTarget', () => {
  it('derives a dashboard link from a navigate row, with no per-row wiring', () => {
    expect(resolve({ enter: { kind: 'navigate', route: '/artifacts' } })).toBe(
      'https://dash.example/artifacts',
    )
  })

  it('derives a session deep link from the open-session payload', () => {
    expect(resolve({ enter: { kind: 'open-session', sessionKey: 'chat-7', title: 'oss' } })).toBe(
      'https://dash.example/chat/oss?sid=chat-7',
    )
  })

  it('has nothing to copy for an insert-token row until a surface claims the chord', () => {
    // A token would be a fine thing to copy; the only producer of this kind feeds the
    // palette, which does not claim the chord yet, so the branch would be unreachable.
    expect(
      resolve({ enter: { kind: 'insert-token', token: '$babysit', tokenKind: 'skill' } }),
    ).toBeNull()
  })

  it('has nothing to copy for an invoke row -- a callback has no address', () => {
    expect(resolve({ enter: { kind: 'invoke', run: () => {} } })).toBeNull()
  })

  it('has nothing to copy for a knowledge row -- no route exists to build a link from', () => {
    expect(resolve({ enter: { kind: 'open-knowledge', entryId: 'k1', title: 'Spec' } })).toBeNull()
  })

  it('has nothing to copy for a row with no declared action', () => {
    expect(resolve({})).toBeNull()
  })

  describe('copyUrl override', () => {
    it('wins over the derived route, because only the provider knows the real address', () => {
      // An artifact row navigates to a dashboard route, but the address worth
      // handing to someone else is where it is actually deployed.
      expect(
        resolve({
          enter: { kind: 'navigate', route: '/artifacts?slug=kanban' },
          copyUrl: 'https://d2nzmpzyp0popu.cloudfront.net/kanban/',
        }),
      ).toBe('https://d2nzmpzyp0popu.cloudfront.net/kanban/')
    })

    it('never reaches the clipboard for a javascript: value', () => {
      expect(
        resolve({ enter: { kind: 'navigate', route: '/artifacts' }, copyUrl: 'javascript:alert(1)' }),
      ).toBeNull()
    })
  })

  describe('a route can never address another origin', () => {
    it('refuses a protocol-relative route that would point off this host', () => {
      // `origin + '//evil.test'` is a valid absolute URL to someone else's host,
      // so a reader pasting it would leave the product without noticing.
      expect(resolve({ enter: { kind: 'navigate', route: '//evil.test/steal' } })).toBeNull()
    })

    it('refuses a route that is not rooted, which would concatenate into nonsense', () => {
      expect(resolve({ enter: { kind: 'navigate', route: 'artifacts' } })).toBeNull()
    })
  })
})
