/**
 * The message the backend commits under when the commit message is blank. Must
 * match `DEFAULT_COMMIT_MESSAGE` in `kiro_crew/apps/builtins/papyrus/backend/gitops.py`.
 * Not a catalog string: the backend writes this English literal into the commit
 * whatever the dashboard language, so the hint must show it verbatim. This module
 * holds only backend wire values, which is why the i18n lint skips it.
 */
export const PAPYRUS_DEFAULT_COMMIT_MESSAGE = 'Update from Papyrus'
