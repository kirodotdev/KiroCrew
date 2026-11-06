/** Sharing this Crew over AWS Systems Manager (`/api/shared-crew/ssm`).
 *
 *  Owner-only. A teammate forwards a port with `aws ssm start-session` and
 *  opens the local link; the invite in that link is the whole credential, so
 *  the gateway returns it once, when the invite is made. */
import type { PeopleRow } from './sharedCrewUi'

export type SsmState = 'active' | 'off' | 'unavailable'

export interface SsmInvite {
  id: string
  alias: string
  created_at: number
  /** True once anyone has opened the Crew with the link. */
  joined: boolean
  /** The id the joined person's People row carries. */
  principal_id: string
}

export interface SsmStatus {
  state: SsmState
  /** Why the listener is not active; empty while it is. */
  reason: string
  port: number
  target: { instance_id: string; region: string } | null
  /** The command a teammate runs. Empty unless `state` is `active`. */
  command: string
  /** Where the browser opens once the forward is up, without the invite. */
  local_url: string
  /** Whether the AWS CLI and the Session Manager plugin are on this machine. */
  invites: SsmInvite[]
}

export interface SsmCreated {
  invite: SsmInvite
  /** The local link with the invite in it. Shown once. */
  url: string
  command: string
  local_url: string
}

export const ssmStatusKey = ['shared-crew', 'ssm'] as const

/** The guide a teammate or owner reads for the tools and the IAM setup. */
export const SSM_GUIDE_URL = 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/remote-and-mobile.md#share-one-crew-over-aws-ssm'

/** Invites to show as Waiting: made, and no person row stands for them yet.
 *  A joined invite leaves the list once its person is a row of their own (that
 *  row carries the Revoke), but stays while the gateway has not seen them since
 *  it started, so it can always be revoked. */
export function waitingSsmInvites(people: readonly PeopleRow[], invites: readonly SsmInvite[]): SsmInvite[] {
  const seen = new Set(people.map(row => row.principal.id))
  return invites.filter(invite => !(invite.joined && seen.has(invite.principal_id)))
}

/** The SSM invite a People row stands for, when it joined through one. */
export function ssmInviteFor(row: PeopleRow, invites: readonly SsmInvite[]): SsmInvite | undefined {
  return invites.find(invite => invite.principal_id === row.principal.id)
}
