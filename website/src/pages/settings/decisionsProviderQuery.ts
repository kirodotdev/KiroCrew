/**
 * Query key for `GET /api/decisions/provider`, shared by the Decisions card and its
 * lazily loaded model picker. It lives outside the picker module so the card can
 * read the provider without pulling the picker into its own chunk.
 */
export const DECISIONS_PROVIDER_QUERY_KEY = ['decisionsProvider'] as const
