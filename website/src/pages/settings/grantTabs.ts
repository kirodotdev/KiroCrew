/**
 * The Settings tabs on which no control gives an agent more than it had, or widens who may reach the agent or the
 * gateway: keyboard shortcuts, the telemetry switches, and the product's own version and updates. Every other
 * tab's content is a grant surface (`AgentGrantRegion`), so on a surface where a press can be a slip (the XR
 * page's live panels) a plain press there is not passed on. The list is of the tabs that are let through, not of
 * the ones that are held back: a tab that is added later is guarded until it is put here.
 */
export const SETTINGS_TABS_THAT_GRANT_NOTHING: ReadonlySet<string> = new Set(['shortcuts', 'privacy', 'about'])
