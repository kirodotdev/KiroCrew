/**
 * The signal-cli command lines the Signal settings panel quotes. Shell
 * commands, never prose: translating any of it would hand the user a command
 * that does not run, so this module holds ONLY command text and is named in
 * `eslint.i18n.config.js`'s ignores. The sentences around them are catalog keys
 * (`pages.settings.signalPanel.connection_failed_check` and `aci_hint`), and
 * the panel renders each command in a `<code>` element.
 */
export function signalReceiveCommand(account: string): string {
  return `signal-cli -a ${account} receive`
}

/**
 * Lists every identity signal-cli knows, each with its ACI, including a sender
 * who is not a saved contact. `-a` is left out while no account is set:
 * signal-cli then uses its only local account.
 */
export function signalListRecipientsCommand(account: string): string {
  return account
    ? `signal-cli -a ${account} listContacts --all-recipients`
    : 'signal-cli listContacts --all-recipients'
}
