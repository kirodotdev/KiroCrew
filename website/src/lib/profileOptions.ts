// Canonical user-profile option slugs, shared by the onboarding flow (step 2)
// and Settings > Chat so the two UIs cannot drift. These slugs are the enum
// values in the dashboard.user_role / dashboard.user_technical_level config
// PATCH allowlist (backend `dashboard/handlers/core.py`) and are mapped to
// prompt descriptions in `context.py` — keep all in sync. Visible labels stay
// per-surface (each screen resolves them through its own i18n keys). (#557)
export const ROLE_SLUGS = [
  'developer',
  'designer',
  'product-manager',
  'data-ml',
  'it-ops',
  'other',
] as const

export const TECH_SLUGS = [
  'codes',
  'somewhat-technical',
  'non-technical',
] as const
