/**
 * The /api/** answers the Settings > Security rail reads on mount, for the
 * screenshot harnesses that render one of its sections. The default fixture
 * router has no security routes (unmatched paths fall through to `[]`, which
 * renders every pane in its empty state), so a harness spreads this table in
 * and adds the section's own endpoints on top.
 */
export const SECURITY_RAIL_FIXTURES = Object.freeze({
  '/api/security/posture': { controls: [], counts: {} },
  '/api/security/denied-commands': {
    builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
  },
  '/api/governance/policy': {
    version: null, has_policy: false, profile: null, unavailable: false, scopes: [],
  },
  '/api/config/kirocrew': { agent: { yolo_duration: '6h', apps_allow_third_party: false } },
  '/api/tailnet/status': {
    enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off',
  },
})
