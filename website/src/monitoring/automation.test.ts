import { describe, it, expect } from 'vitest'
import { dashboardAutomationSlotKey, stripDashboardSessionPrefixes } from './automation'

describe('stripDashboardSessionPrefixes', () => {
  it('removes one dashboard: prefix, then any stacked dashboard_ prefixes', () => {
    expect(stripDashboardSessionPrefixes('dashboard:chat-1-2')).toBe('chat-1-2')
    expect(stripDashboardSessionPrefixes('dashboard_dashboard_chat-1-2')).toBe('chat-1-2')
    expect(stripDashboardSessionPrefixes('dashboard:dashboard_chat-1-2')).toBe('chat-1-2')
    expect(stripDashboardSessionPrefixes('slack:123.45')).toBe('slack:123.45')
  })

  it('is the fold dashboardAutomationSlotKey applies before its filename escape', () => {
    expect(dashboardAutomationSlotKey('dashboard:dashboard_chat-1-2')).toBe('chat-1-2')
    expect(dashboardAutomationSlotKey('slack:123.45')).toBe('slack_123.45')
  })
})
