// Literal tokens the owner types on the Custom secrets card. They stay code
// constants, never catalog values, so a translation cannot change them: the
// secrets MCP server is opt-in per agent under this exact name, and
// `kirocrew secrets authorize` binds one secret NAME to one exact https origin.
// The sentence around them is translated (`settings.secrets.custom_authorize`).
// This module holds only these literals; it renders nothing.
export const SECRETS_SERVER_GRANT = '@kirocrew-secrets'
export const SECRETS_AUTHORIZE_COMMAND = 'kirocrew secrets authorize NAME https://api.example.com'
