export const capabilityLabels = {
  state: {
    inherited: 'crewCapabilities.inherited',
    local: 'crewCapabilities.local',
    removed: 'crewCapabilities.removed',
  },
  source: {
    builtin: 'crewCapabilities.source_builtin',
    package: 'crewCapabilities.source_package',
    custom: 'crewCapabilities.source_custom',
    project: 'crewCapabilities.source_project',
    unknown: 'crewCapabilities.source_unknown',
  },
  scope: {
    global: 'crewCapabilities.scope_global',
    project: 'crewCapabilities.scope_project',
  },
  mode: {
    shared: 'crewCapabilities.mode_shared',
    legacy_snapshot: 'crewCapabilities.mode_legacy_snapshot',
    inherited: 'crewCapabilities.mode_inherited',
  },
  runtime: {
    pending: 'crewCapabilities.runtime_pending',
    applied: 'crewCapabilities.runtime_applied',
    failed: 'crewCapabilities.runtime_failed',
    unverified: 'crewCapabilities.runtime_unverified',
  },
  parentChange: {
    added: 'crewCapabilities.parentChange_added',
    removed: 'crewCapabilities.parentChange_removed',
    changed: 'crewCapabilities.parentChange_changed',
  },
  change: {
    added: 'crewCapabilities.change_added',
    removed: 'crewCapabilities.change_removed',
    changed: 'crewCapabilities.change_changed',
  },
} as const
