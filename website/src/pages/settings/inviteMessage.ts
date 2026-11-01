/** The message the owner sends to someone they share this Crew with.
 *
 *  Built from the owner's display name, the Crew's tailnet links and, for a
 *  link invite, the invite URL. The tailnet IPv4 link leads because it opens
 *  without MagicDNS; the MagicDNS name follows as the fallback for a device
 *  that has MagicDNS on. Every word comes from the catalog. */
import type { PeopleRow } from '../../api/sharedCrewUi'
import { principalName } from '../../api/sharedCrewUi'
import { i18nT } from '../../i18n/t'

/** Where a device turns MagicDNS on. */
export const TAILSCALE_DNS_URL = 'https://login.tailscale.com/admin/dns'

const IPV4_HOST = /^\d{1,3}(\.\d{1,3}){3}$/

type HostKind = 'ipv4' | 'ipv6' | 'name'

function hostKind(url: string): HostKind {
  let host = ''
  try { host = new URL(url).hostname } catch { return 'name' }
  if (IPV4_HOST.test(host)) return 'ipv4'
  return host.startsWith('[') ? 'ipv6' : 'name'
}

/** The two links the message carries: an address (IPv4 first) and, when there
 *  is one, the MagicDNS name. */
export function splitDirectUrls(urls: readonly string[]): { ipUrl: string; nameUrl: string } {
  const ip = urls.find(u => hostKind(u) === 'ipv4') ?? urls.find(u => hostKind(u) === 'ipv6') ?? ''
  const name = urls.find(u => hostKind(u) === 'name') ?? ''
  return { ipUrl: ip || name, nameUrl: ip ? name : '' }
}

/** The owner's name as the People list shows it. */
export function ownerDisplayName(people: readonly PeopleRow[]): string {
  const row = people.find(r => r.is_owner) ?? people.find(r => r.is_viewer)
  if (!row) return i18nT('sharedCrew.crew_owner')
  return row.member?.alias || principalName(row.principal) || i18nT('sharedCrew.crew_owner')
}

export interface InviteMessageInput {
  owner: string
  urls: readonly string[]
  /** A link invite's URL; empty for an email invite. */
  inviteUrl: string
}

export function buildInviteMessage({ owner, urls, inviteUrl }: InviteMessageInput): string {
  const { ipUrl, nameUrl } = splitDirectUrls(urls)
  const lines = [
    i18nT('sharedCrew.invite_msg_header', { owner }),
    inviteUrl ? i18nT('sharedCrew.invite_msg_accept_link', { url: inviteUrl }) : i18nT('sharedCrew.invite_msg_accept_email'),
    i18nT('sharedCrew.invite_msg_open', { url: ipUrl }),
  ]
  if (nameUrl) lines.push('', i18nT('sharedCrew.invite_msg_fallback', { dns: TAILSCALE_DNS_URL, url: nameUrl }))
  return lines.join('\n')
}
