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
    // A mate's config has no shared scope — it is owned. The pane does not
    // render this label (the descriptor is hidden for a mate), but the map
    // stays total so indexing by the widened scope union is type-safe.
    mate: 'crewCapabilities.scope_mate',
  },
  mode: {
    shared: 'crewCapabilities.mode_shared',
    legacy_snapshot: 'crewCapabilities.mode_legacy_snapshot',
    inherited: 'crewCapabilities.mode_inherited',
    mate: 'crewCapabilities.mode_mate',
  },
  runtime: {
    pending: 'crewCapabilities.runtime_pending',
    applied: 'crewCapabilities.runtime_applied',
    failed: 'crewCapabilities.runtime_failed',
    unverified: 'crewCapabilities.runtime_unverified',
    owned: 'crewCapabilities.runtime_owned',
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
