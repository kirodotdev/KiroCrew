import { describe, it, expect } from 'vitest'
import type { PeopleRow } from '../../api/sharedCrewUi'
import { buildInviteMessage, ownerDisplayName, splitDirectUrls } from './inviteMessage'

const V4 = 'http://100.64.0.1:5476/'
const V6 = 'http://[fd7a:115c::1]:5476/'
const NAME = 'http://crew.tail1.ts.net:5476/'

const person = (id: string, name: string, over: Partial<PeopleRow> = {}): PeopleRow => ({
  principal: { id, kind: 'human', display: { name, login: '', captured_at: null } },
  sources: ['observed'], ...over,
})

describe('splitDirectUrls', () => {
  it('leads with the IPv4 address whatever the order, and keeps the MagicDNS name as the fallback', () => {
    expect(splitDirectUrls([NAME, V6, V4])).toEqual({ ipUrl: V4, nameUrl: NAME })
    expect(splitDirectUrls([V4, NAME, V6])).toEqual({ ipUrl: V4, nameUrl: NAME })
  })

  it('falls back to an IPv6 address, then to the name alone with no duplicate fallback', () => {
    expect(splitDirectUrls([NAME, V6])).toEqual({ ipUrl: V6, nameUrl: NAME })
    expect(splitDirectUrls([NAME])).toEqual({ ipUrl: NAME, nameUrl: '' })
  })

  it('has nothing to say without a URL', () => {
    expect(splitDirectUrls([])).toEqual({ ipUrl: '', nameUrl: '' })
  })
})

describe('buildInviteMessage', () => {
  it('a link invite carries its URL, then the IPv4 link, then MagicDNS', () => {
    expect(buildInviteMessage({ owner: 'Mac Studio', urls: [V4, NAME, V6], inviteUrl: 'https://login.tailscale.com/uinv/x' }).split('\n')).toEqual([
      'Mac Studio invited you to Kiro Crew.',
      '1. Accept the Tailscale share: https://login.tailscale.com/uinv/x',
      `2. Open Crew: ${V4}`,
      '',
      `Can't open it? Turn on MagicDNS at https://login.tailscale.com/admin/dns, then open ${NAME}`,
    ])
  })

  it('an email invite names the email Tailscale sent', () => {
    const lines = buildInviteMessage({ owner: 'Mac Studio', urls: [V4, NAME], inviteUrl: '' }).split('\n')
    expect(lines[1]).toBe('1. Accept the invitation Tailscale emailed you')
  })

  it('leaves out the MagicDNS block when the Crew has no name URL', () => {
    const text = buildInviteMessage({ owner: 'Pat', urls: [V4], inviteUrl: '' })
    expect(text.split('\n')).toHaveLength(3)
    expect(text).not.toContain('MagicDNS')
  })
})

describe('ownerDisplayName', () => {
  it('prefers the owner row, then the viewer, then a generic label', () => {
    expect(ownerDisplayName([person('a', 'Pat', { is_viewer: true }), person('b', 'Mac Studio', { is_owner: true })])).toBe('Mac Studio')
    expect(ownerDisplayName([person('a', 'Pat', { is_viewer: true })])).toBe('Pat')
    expect(ownerDisplayName([])).toBe('Crew owner')
  })

  it('uses the display alias a person set', () => {
    expect(ownerDisplayName([person('a', 'mac', { is_owner: true, member: { alias: 'Pat’s Mac', avatar_id: '', profile_pic_url: '' } })])).toBe('Pat’s Mac')
  })
})
