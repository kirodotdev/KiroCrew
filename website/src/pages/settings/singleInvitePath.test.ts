/**
 * People has one way to invite someone: the `Invite…` button (`InviteEntry`)
 * opening one dialog holding one form, which makes invites through one API call.
 * These scans read the production sources so a second entry point, a second form
 * or a second create call fails here instead of shipping as a parallel flow.
 */
import { describe, it, expect } from 'vitest'

const all = import.meta.glob('/src/**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
const production = Object.entries(all).filter(([file]) => !/\.test\.tsx?$/.test(file) && !file.includes('/test/'))

/** The production files whose source matches *pattern*, with how many times. */
function uses(pattern: RegExp): Record<string, number> {
  const out: Record<string, number> = {}
  for (const [file, source] of production) {
    const count = source.match(pattern)?.length ?? 0
    if (count > 0) out[file.replace('/src/', '')] = count
  }
  return out
}

describe('one invite path', () => {
  it('makes invites through one API call, from one place', () => {
    expect(uses(/\.tailscaleCreateInvites\(/g)).toEqual({ 'pages/settings/TailscaleInvites.tsx': 1 })
  })

  it('renders the invite form once, inside the dialog', () => {
    expect(uses(/<InviteForm\b/g)).toEqual({ 'pages/settings/TailscaleInvites.tsx': 1 })
    expect(uses(/<InviteDialog\b/g)).toEqual({ 'pages/settings/TailscaleInvites.tsx': 1 })
  })

  it('labels one Invite… button, owned by the entry component', () => {
    expect(uses(/sharedCrew\.hero_invite\b/g)).toEqual({ 'pages/settings/TailscaleInvites.tsx': 1 })
  })

  it('mounts the entry once per mutually exclusive People layout', () => {
    expect(uses(/<InviteEntry\b/g)).toEqual({
      'pages/settings/PeopleCrew.tsx': 1,
      'pages/settings/PeoplePanel.tsx': 1,
    })
  })

  it('lists not-yet-accepted email invites in People (or under the entry in personal mode) and held links in the dialog, nowhere else', () => {
    expect(uses(/<InviteRow\b/g)).toEqual({ 'pages/settings/PeopleCrew.tsx': 1, 'pages/settings/TailscaleInvites.tsx': 1 })
  })

  it('revokes through one control, whoever the row is for', () => {
    expect(uses(/\.tailscaleRevokeInvite\(/g)).toEqual({ 'pages/settings/TailscaleInvites.tsx': 1 })
    // An AWS SSM invite revokes through the same control, in the same words.
    expect(uses(/\.sharedCrewSsmRevokeInvite\(/g)).toEqual({ 'pages/settings/TailscaleInvites.tsx': 1 })
    expect(uses(/<RevokeControl\b/g)).toEqual({
      'pages/settings/PeopleCrew.tsx': 2,
      'pages/settings/SsmInvite.tsx': 1,
      'pages/settings/TailscaleInvites.tsx': 1,
    })
  })
})
