/**
 * The message the backend commits under when the commit message is blank. Must
 * match `DEFAULT_COMMIT_MESSAGE` in `kiro_crew/apps/builtins/papyrus/backend/gitops.py`.
 * Not a catalog string: the backend commits this English text in every locale,
 * so the hint shows it verbatim.
 */
export const PAPYRUS_DEFAULT_COMMIT_MESSAGE = 'Update from Papyrus'
