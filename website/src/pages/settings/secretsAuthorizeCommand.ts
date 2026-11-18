// The literal command the owner types on a Custom secret's pane. It stays a
// code constant, never a catalog value, so a translation cannot change it:
// `kirocrew secrets authorize` binds one secret NAME to one exact https origin.
// This module holds only this literal; it renders nothing.
export const SECRETS_AUTHORIZE_COMMAND = 'kirocrew secrets authorize NAME https://api.example.com'
