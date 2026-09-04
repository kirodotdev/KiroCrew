/**
 * Mochi - Approval dialog for tool execution confirmation
 */
import React from 'react'
import { AlertTriangle } from 'lucide-react'
import type { ApprovalRequest } from '../shared/types'

import { i18nT } from '../../../../i18n/t'
interface Props {
  request: ApprovalRequest
  onRespond: (approved: boolean) => void
}

/**
 * Localized risk words. The raw discriminant used to render straight through, so nine
 * locales read "medium risque" — an English value beside a translated noun. Keyed here
 * as full literals so `check-i18n-keys.mjs` can verify all three exist.
 */
const RISK_LEVEL_KEY = {
  low: 'apps.mochi.approval.risk_low',
  medium: 'apps.mochi.approval.risk_medium',
  high: 'apps.mochi.approval.risk_high',
} as const

const riskColors = { low: '#81c784', medium: '#ffb74d', high: '#ef5350' }

export const ApprovalDialog: React.FC<Props> = ({ request, onRespond }) => (
  <div style={{
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
    display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
  }}>
    <div style={{
      background: '#2a2a2a', borderRadius: 12, padding: 20, maxWidth: 280,
      boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <span style={{ display: 'inline-flex', alignItems: 'center', color: riskColors.medium }}><AlertTriangle size={18} /></span>
        <span style={{ fontWeight: 600, fontSize: 14 }}>{i18nT('apps.mochi.approval.title')}</span>
      </div>

      <div style={{ marginBottom: 12 }}>
        <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>{i18nT('apps.mochi.approval.tool')}</div>
        <div style={{ fontSize: 13, fontFamily: 'monospace' }}>{request.toolName}</div>
      </div>

      <div style={{ marginBottom: 12 }}>
        <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>{i18nT('apps.mochi.approval.params')}</div>
        <div style={{ fontSize: 12, background: 'rgba(255,255,255,0.05)', padding: 8, borderRadius: 6, whiteSpace: 'pre-wrap' }}>
          {request.paramsSummary}
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <span style={{
          fontSize: 11, padding: '2px 8px', borderRadius: 4,
          background: riskColors[request.riskLevel] + '22',
          color: riskColors[request.riskLevel],
        }}>
          {i18nT(RISK_LEVEL_KEY[request.riskLevel])} {i18nT('apps.mochi.approval.risk')}
        </span>
      </div>

      <div style={{ display: 'flex', gap: 8 }}>
        <button onClick={() => onRespond(false)} style={{
          flex: 1, padding: '8px 0', borderRadius: 8, border: '1px solid rgba(255,255,255,0.2)',
          background: 'transparent', color: '#e0e0e0', cursor: 'pointer', fontSize: 13,
        }}>{i18nT('apps.mochi.approval.reject')}</button>
        <button onClick={() => onRespond(true)} style={{
          flex: 1, padding: '8px 0', borderRadius: 8, border: 'none',
          background: '#4fc3f7', color: '#1a1a1a', cursor: 'pointer', fontSize: 13, fontWeight: 600,
        }}>{i18nT('apps.mochi.approval.approve')}</button>
      </div>
    </div>
  </div>
)
