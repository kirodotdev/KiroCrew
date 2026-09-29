/**
 * What a crew card says, and which lanes the picker offers.
 *
 * Both modules under test are pure functions over data the panel already holds, which
 * is why they are pure: the wording is the part that has been wrong most often — a
 * Fargate row described as having a dashboard, a cloud row described as "added by you"
 * beside a Remove that leaves an instance billing — and pinning it here costs nothing
 * compared with rendering a panel that owns nineteen mutations.
 */
import { describe, it, expect } from 'vitest'

import type { InstanceView, RemoteProvisioner } from '../../api/client'
import {
  captionWords,
  detailNote,
  detailRows,
  isMate,
  laneHasLiveReading,
  laneLabel,
  laneOf,
  readClock,
  statusDotClass,
  statusWords,
} from './remoteLane'
import { laneChoices } from './DeployMateDialog'

function inst(over: Partial<InstanceView> = {}): InstanceView {
  return {
    id: 'crew-1',
    name: 'worker-1',
    ssh_host: 'dev-dsk.example.com',
    remote_port: 8765,
    local_port: 18765,
    ttl: '8h',
    remote_bin: '',
    connection_method: 'ssh',
    ssm_target: '',
    aws_profile: '',
    aws_region: '',
    ssm_run_as: '',
    was_connected: false,
    status: { instance_id: 'crew-1', state: 'connected' },
    ...over,
  }
}

const fargate = () =>
  inst({
    name: 'tada-fund-manager',
    connection_method: 'fargate',
    ssm_target: 'ecs:kirocrew-prod_8f3a1c2d9e4b4f7a8c1d2e3f4a5b6c7d_a1b2c3d4e5f60718',
    aws_region: 'us-west-2',
  })

const ec2 = (over: Partial<InstanceView> = {}) =>
  inst({
    name: 'kirocrew-lead',
    connection_method: 'ssm',
    ssm_target: 'i-0a91c47f2b3d8e5f1',
    aws_region: 'us-west-2',
    ...over,
  })

describe('crew or mate -- which of the two a row is', () => {
  it('reads a Fargate task as a mate and everything else as a crew', () => {
    // The whole reason the panel has two tabs. A CREW is a gateway: it serves a roster
    // and has a dashboard. A MATE is one agent: a task holding one agent spec, serving a
    // chat API, with neither. They shared one list, which is what made the list
    // unreadable -- the only thing telling them apart was buried in a paragraph.
    expect(isMate(fargate())).toBe(true)
    expect(isMate(ec2())).toBe(false)
    expect(isMate(inst())).toBe(false)
    // An EC2-stamped SSH row is still a gateway, not an agent.
    expect(isMate(inst({ provisioner_id: 'aws_ec2' }))).toBe(false)
  })

})

describe('the lane a crew runs in', () => {
  it('tells an EC2 machine from a Fargate task, which the transport cannot', () => {
    // Both reach the crew over SSM, so the transport is the same for the two and the
    // old card wore both acronyms (EC2, SSM) to compensate. The lane is what the user
    // chose when they deployed, which is the thing they recognise.
    expect(ec2().connection_method).toBe('ssm')
    expect(fargate().connection_method).toBe('fargate')
    expect(laneOf(ec2())).toBe('ec2')
    expect(laneOf(fargate())).toBe('fargate')
    expect(laneOf(inst())).toBe('ssh')
  })

  it('labels each lane with its product name', () => {
    expect(laneLabel('ssh')).toBe('SSH')
    expect(laneLabel('ec2')).toBe('EC2')
    expect(laneLabel('fargate')).toBe('Fargate')
  })

  it('reads an EC2 stamp as the EC2 lane even over plain SSH', () => {
    // The lane is where the crew RUNS, not how the dashboard dials it. This is what the
    // old card said with its EC2 badge on exactly these rows, and dropping it would call
    // a live, billing instance an SSH machine.
    expect(laneOf(inst({ provisioner_id: 'aws_ec2' }))).toBe('ec2')
    expect(laneOf(inst())).toBe('ssh')
  })

  it('offers a read time and a re-read only where the answer comes from AWS', () => {
    // An SSH tunnel is this process's own socket: printing an age for it would invent
    // one, and a "Check again" beside it would promise news only a reconnect produces.
    expect(laneHasLiveReading(inst())).toBe(false)
    expect(laneHasLiveReading(ec2())).toBe(true)
    expect(laneHasLiveReading(fargate())).toBe(true)
    // Asked of the TRANSPORT: this row runs in the EC2 lane and its status is still a
    // local socket, so an age printed for it would be one no remote read produced.
    expect(laneHasLiveReading(inst({ provisioner_id: 'aws_ec2' }))).toBe(false)
  })

  it('describes the connection by how the dashboard reaches it, not by the lane', () => {
    // An EC2-stamped row over plain SSH has an SSH host and no instance id: labelling it
    // "EC2 over Session Manager" beside an empty Instance value would describe a
    // connection it does not have.
    const rows = detailRows(inst({ provisioner_id: 'aws_ec2' }))
    const conn = rows.find(r => r.label === 'Connection')
    expect(conn?.value).toBe('SSH tunnel')
    expect(rows.map(r => r.label)).toContain('Host')
    expect(rows.map(r => r.label)).not.toContain('Instance')
  })
})

describe('the status line', () => {
  it('says Running for a Fargate crew and Connected for the others', () => {
    // Two different claims, and only one is true of a Fargate task: it is running in
    // AWS, and what the user gets from it is a chat, never a dashboard to connect to.
    expect(statusWords(fargate())).toBe('Running')
    expect(statusWords(ec2())).toBe('Connected')
    expect(statusWords(inst())).toBe('Connected')
  })

  it('has plain words for every state the tunnel reports', () => {
    const words = (state: InstanceView['status']['state']) =>
      statusWords(inst({ status: { instance_id: 'crew-1', state } }))
    expect(words('connecting')).toBe('Starting up')
    expect(words('error')).toBe('Not reachable')
    expect(words('stopped')).toBe('Stopped')
    // "Not connected yet" rather than "Not connected": beside the error state's "Not
    // reachable" the two read as one thing said twice, and only one of them is a failure.
    expect(words('disconnected')).toBe('Not connected yet')
    // No state renders a bare wire value: that is what the plain-words mapping is for,
    // and a missed branch would show up here as the raw word.
    for (const state of ['connected', 'connecting', 'error', 'stopped', 'disconnected'] as const) {
      expect(words(state)).not.toContain(state)
    }
  })

  it('paints the dot from the state, through theme tokens only', () => {
    const dot = (state: InstanceView['status']['state']) =>
      statusDotClass(inst({ status: { instance_id: 'crew-1', state } }))
    expect(dot('connected')).toBe('bg-ok')
    expect(dot('connecting')).toBe('bg-warn')
    expect(dot('error')).toBe('bg-danger')
    expect(dot('disconnected')).toBe('bg-muted')
  })

  it('reads a clock only from a real moment', () => {
    // 0 is "never read", and an epoch-zero timestamp printed as a time would be a
    // reading the dashboard cannot vouch for.
    expect(readClock(0)).toBe('')
    expect(readClock(Date.UTC(2026, 8, 29, 16, 32))).toMatch(/\d/)
  })
})

describe('the caption', () => {
  it('says what a Fargate crew can and cannot give you', () => {
    expect(captionWords(fargate(), false)).toContain('Chat only')
  })

  it('never calls an instance in your account "added by you"', () => {
    // That wording invites a Remove that unregisters a live instance and takes away the
    // only place the dashboard could still delete it — so an EC2 row says the one
    // consequence instead, and Details carries the rest.
    const caption = captionWords(ec2(), false)
    expect(caption).not.toContain('Added by you')
    expect(caption).toContain('billing')
  })

  it('credits a correlated cloud crew to the launcher', () => {
    expect(captionWords(ec2(), true)).toContain('Kiro Crew')
  })

  it('does say "added by you" for a hand-added SSH machine', () => {
    expect(captionWords(inst(), false)).toBe('Added by you')
  })

  it('is one short line for every card', () => {
    // The sentences it replaced ran to three lines each. A cap here is what stops the
    // paragraph growing back one card at a time; the long form lives in Details.
    for (const row of [inst(), ec2(), fargate()]) {
      for (const correlated of [true, false]) {
        expect(captionWords(row, correlated).length).toBeLessThanOrEqual(52)
      }
    }
  })
})

describe('Details behind the kebab', () => {
  it('carries the ECS task verbatim, untruncated', () => {
    // The whole reason the identifiers moved here: the card had to shorten the target
    // to fit one line, and two tasks in a cluster differ only in the tail it cut off.
    const rows = detailRows(fargate())
    const task = rows.find(r => r.label === 'Task')
    expect(task?.value).toBe(fargate().ssm_target)
    expect(task?.value).not.toContain('…')
  })

  it('carries the instance id and region for an EC2 crew', () => {
    const rows = detailRows(ec2())
    expect(rows.map(r => r.value)).toContain('i-0a91c47f2b3d8e5f1')
    expect(rows.map(r => r.value)).toContain('us-west-2')
  })

  it('carries the host and port for an SSH crew', () => {
    const host = detailRows(inst()).find(r => r.label === 'Host')
    expect(host?.value).toContain('dev-dsk.example.com')
    expect(host?.value).toContain('8765')
  })

  it('labels a chained crew’s host as reported, and drops its port', () => {
    // This gateway resolves the PARENT's host for a row carrying `via_instance_id` and
    // never dials this one, so rendering it where a verified address goes would let an
    // untrusted string borrow that authority. The port is the crew's own gateway port,
    // which is not what this dashboard forwards to either.
    const rows = detailRows(inst({ via_instance_id: 'parent-1', remote_port: 9999 }))
    const labels = rows.map(r => r.label)
    expect(labels).toContain('Host, as reported')
    expect(labels).not.toContain('Host')
    expect(rows.map(r => r.value).join(' ')).not.toContain('9999')
  })

  it('keeps the long sentence about what Remove does not do', () => {
    // Moved, not rewritten: each of these was arrived at by correcting a specific wrong
    // reading, and shortening them here would undo those corrections.
    expect(detailNote(fargate(), false)).toContain('keeps running')
    expect(detailNote(ec2(), true)).toContain('Kiro Crew')
    // An EC2-STAMPED row says plainly that its instance may still be billing; a row with
    // no stamp hedges instead. That distinction was deliberate on the cards and survives
    // the move: the note must not claim AWS resources exist when nothing proves they do,
    // nor hedge when the stamp says the launcher created them.
    expect(detailNote(ec2({ provisioner_id: 'aws_ec2' }), false)).toContain('billing')
    expect(detailNote(ec2(), false)).toContain('cannot verify')
  })

  it('never puts an identifier on the card itself', () => {
    // The property edit 3 asks for, stated as one assertion over all three lanes: no
    // card string may contain a host, a port, a task target or an instance id.
    const identifiers = ['dev-dsk.example.com', '8765', 'ecs:', 'i-0a91c47f2b3d8e5f1']
    for (const row of [inst(), ec2(), fargate()]) {
      const onCard = [
        row.name,
        laneLabel(laneOf(row)),
        statusWords(row),
        captionWords(row, false),
      ].join(' ')
      for (const id of identifiers) expect(onCard).not.toContain(id)
    }
  })
})

function lane(over: Partial<RemoteProvisioner> = {}): RemoteProvisioner {
  return {
    id: 'aws_ec2',
    kind: 'aws_ec2',
    label: 'AWS EC2 in your own account',
    posix_only: true,
    steps: [],
    ...over,
  }
}

const fargateRow = (over: Partial<RemoteProvisioner> = {}) =>
  lane({
    id: 'aws_fargate',
    kind: 'aws_fargate',
    label: 'AWS Fargate in your own account',
    confirm_before_launch: 'img@sha256:aaa <- arn:secret',
    ...over,
  })

describe('the picker’s lane chips', () => {
  it('offers the Fargate lane at all, which the old kind list hid', () => {
    // `canRenderRemoteProvisionerKind` answered false for `aws_fargate`, so the row the
    // backend offered — and whose launches the API accepted — was filtered out entirely.
    const fargateChip = laneChoices([lane(), fargateRow()], 'demo').find(
      c => c.lane === 'fargate',
    )
    expect(fargateChip?.disabled).toBe(false)
    expect(fargateChip?.provisioner?.id).toBe('aws_fargate')
  })

  it('gives every unusable lane a reason a reader can actually reach', () => {
    // A lane marked unusable MUST carry a sentence saying why. The chip has room for
    // three words, so that sentence is what the picker renders when the lane is
    // selected — and selecting an unusable lane is allowed precisely so the reason is
    // reachable. A reason-less disabled chip is a dead end with no explanation.
    for (const chip of laneChoices([lane()], 'demo')) {
      if (chip.disabled) expect(chip.blockedReason.length).toBeGreaterThan(20)
    }
    const pinned = laneChoices([lane(), fargateRow({ serves_mate: 'demo' })], 'orchard-sde')
    for (const chip of pinned) {
      if (chip.disabled) expect(chip.blockedReason.length).toBeGreaterThan(20)
    }
  })

  it('shows a lane no provisioner backs, disabled, with the reason', () => {
    // Silently absent is indistinguishable from "does not exist". A Fargate lane with no
    // cluster configured is that case: the chip stays, disabled, with what to set up.
    const fargateChip = laneChoices([lane()], 'demo').find(c => c.lane === 'fargate')
    expect(fargateChip?.disabled).toBe(true)
    expect(fargateChip?.blockedReason).toContain('cluster')
  })

  it('disables a Fargate lane pinned to another crew and names both crews', () => {
    const chip = laneChoices([lane(), fargateRow({ serves_mate: 'demo' })], 'orchard-sde').find(
      c => c.lane === 'fargate',
    )
    expect(chip?.disabled).toBe(true)
    expect(chip?.hint).toContain('demo')
    expect(chip?.blockedReason).toContain('demo')
    expect(chip?.blockedReason).toContain('orchard-sde')
  })

  it('enables the same lane for the crew it serves', () => {
    // The complement: without it, disabling every pinned lane would satisfy the test
    // above and the lane would be unreachable for the one crew it can deploy.
    const chip = laneChoices([lane(), fargateRow({ serves_mate: 'demo' })], 'demo').find(
      c => c.lane === 'fargate',
    )
    expect(chip?.disabled).toBe(false)
  })

  it('does not judge a pinned lane before a crew is picked', () => {
    // With no crew chosen there is nothing to compare, and a lane shown as refused
    // before the user has picked reads as a lane that is broken.
    const chip = laneChoices([lane(), fargateRow({ serves_mate: 'demo' })], '').find(
      c => c.lane === 'fargate',
    )
    expect(chip?.disabled).toBe(false)
  })

  it('carries the lane’s own confirmation value through untouched', () => {
    // The client never composes this: the server compares it to what the descriptor
    // published and the engine compares it again to what it is about to launch.
    const chip = laneChoices([fargateRow()], 'demo').find(c => c.lane === 'fargate')
    expect(chip?.provisioner?.confirm_before_launch).toBe('img@sha256:aaa <- arn:secret')
  })
})
