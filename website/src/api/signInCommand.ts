import { i18nT } from '../i18n/t'

/**
 * The sign-in sentences name the catalog's `api.client.reauth_command`
 * (`kirocrew token`), which a gateway in a container does not have on its host.
 * Such a gateway sends its own command in the status payload's
 * `sign_in_command`; this holds the last one this tab heard. '' until a status
 * arrives, so a tab that never loaded one keeps the catalog's command.
 *
 * Its own module, importing nothing but the translator: suites that mock
 * `./client` wholesale still render the gate's sentences through it.
 */
let gatewayCommand = ''

/** Called with each status the tab receives from the gateway. */
export function noteSignInCommand(command: string | undefined): void {
  gatewayCommand = command ?? ''
}

/** The command the sign-in sentences name on this gateway. */
export function signInCommand(): string {
  return gatewayCommand || i18nT('api.client.reauth_command')
}

/**
 * A translated sign-in sentence with this gateway's command in place of the
 * catalog's. Every shipping catalog carries its own `reauth_command` verbatim in
 * each of these sentences (`ApiClient.coverage.test.tsx` pins it), so the
 * command is replaced as a whole value, the way a `{{placeholder}}` value would
 * be. Callers pass `i18nT('<key>')`, so the key checker still sees each key.
 */
export function signInSentence(sentence: string): string {
  const catalogCommand = i18nT('api.client.reauth_command')
  if (!gatewayCommand || !catalogCommand) return sentence
  return sentence.split(catalogCommand).join(gatewayCommand)
}
