import { describe, it, expect } from 'vitest'
import type { InstanceView } from '../api/client'
import type { WarmConn } from '../store/instancesSlice'
import { hasDashboardPane, shortenEcsTarget, usesSsmTransport } from '../utils/remoteCrew'
import { visibleInstanceTabs } from '../components/InstanceTabBar'

/** Minimal InstanceView; only the fields the switcher rule reads matter. */
function inst(id: string, method: InstanceView['connection_method'], extra: Partial<InstanceView> = {}): InstanceView {
  return {
    id,
    name: id,
    ssh_host: method === 'ssh' ? id : '',
    remote_port: 0,
    local_port: 0,
    ttl: '',
    remote_bin: '',
    connection_method: method,
    ssm_target: method === 'ssh' ? '' : 'ecs:c_t_r',
    aws_profile: '',
    aws_region: '',
    ssm_run_as: '',
    was_connected: false,
    status: { instance_id: id, state: 'disconnected' },
    ...extra,
  }
}

describe('remote crew transport predicates', () => {
  it('ssm and fargate both address the crew through an SSM forward; ssh does not', () => {
    expect(usesSsmTransport(inst('a', 'ssh'))).toBe(false)
    expect(usesSsmTransport(inst('b', 'ssm'))).toBe(true)
    expect(usesSsmTransport(inst('c', 'fargate'))).toBe(true)
  })

  it('only a fargate crew has no dashboard pane', () => {
    expect(hasDashboardPane(inst('a', 'ssh'))).toBe(true)
    expect(hasDashboardPane(inst('b', 'ssm'))).toBe(true)
    expect(hasDashboardPane(inst('c', 'fargate'))).toBe(false)
    // An older record with no method at all is an ssh crew and keeps its pane.
    expect(hasDashboardPane({})).toBe(true)
  })
})

describe('shortenEcsTarget', () => {
  it.each([
    // An ECS target keeps the cluster, the head of the task id and the tail of
    // the runtime id, so two tasks in one cluster stay distinguishable once
    // the row truncates from the right.
    [
      'ecs:crew_0123456789abcdef0123456789abcdef_fedcba9876543210fedcba9876543210-2653819172',
      'ecs:crew_01234567\u2026-2653819172',
    ],
    // Anything that is not an ECS target passes through unchanged.
    ['i-0abc12345678', 'i-0abc12345678'],
    ['', ''],
  ])('%s -> %s', (target, shown) => {
    expect(shortenEcsTarget(target)).toBe(shown)
  })
})

describe('visibleInstanceTabs and the fargate method', () => {
  const warm: Record<string, WarmConn> = { f: { port: 7790, token: '' } }

  it('never gives a fargate crew a switcher tab, whatever its connect state', () => {
    // Every signal that earns an ssm crew a tab is present on the fargate one:
    // sticky intent, a connected status, and a warm entry. The method alone
    // removes it, because there is no pane to switch to.
    const fargate = inst('f', 'fargate', {
      was_connected: true,
      status: { instance_id: 'f', state: 'connected', local_port: 7790, turn_url: 'http://127.0.0.1:7790/v1/chat/completions' },
    })
    const ssm = inst('s', 'ssm', { was_connected: true, status: { instance_id: 's', state: 'connected' } })
    expect(visibleInstanceTabs([fargate, ssm], warm).map(i => i.id)).toEqual(['s'])
    expect(visibleInstanceTabs([fargate], warm)).toEqual([])
  })

  it('leaves the existing tab rule intact for the other methods', () => {
    const idle = inst('idle', 'ssh')
    const sticky = inst('sticky', 'ssh', { was_connected: true })
    const live = inst('live', 'ssm', { status: { instance_id: 'live', state: 'connected' } })
    expect(visibleInstanceTabs([idle, sticky, live], {}).map(i => i.id)).toEqual(['sticky', 'live'])
  })
})
