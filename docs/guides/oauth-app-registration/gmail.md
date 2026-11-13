# Gmail card: Google notes

Set up Google once for every Google card with [google.md](google.md): run the script, three clicks, paste once. This page keeps only what is specific to Gmail.

| | |
|---|---|
| Redirect URI | `http://127.0.0.1:48104/callback` |
| MCP endpoint | `https://gmailmcp.googleapis.com/mcp/v1` |
| APIs the script enables | `gmail.googleapis.com` and `gmailmcp.googleapis.com` |
| Environment variable prefix | `KIROCREW_CONNECTIONS_GMAIL_` (`CLIENT_ID`, `CLIENT_SECRET`) |

## Scopes

| Scope | Kiro Crew requests it | What it allows | Tools it serves |
|---|---|---|---|
| `https://www.googleapis.com/auth/gmail.readonly` | yes | **Restricted**. View your email messages and settings | `search_threads`, `get_thread`, `get_message`, `list_labels`, `list_drafts` |
| `https://www.googleapis.com/auth/gmail.compose` | no | **Restricted**. Manage drafts and send emails | `create_draft`; likely the label tools (`label_message`, `label_thread`, `unlabel_message`, `unlabel_thread`, `create_label`), which Google does not map |

Google's page does not map tools to scopes; the mapping follows each scope's description. Scopes on **Data Access** are a ceiling; the consent screen shows what Kiro Crew actually asks for.

## Check it works

After **Connect** shows **Connected**, ask in a chat: "What did Ariel say in her last email about our marketing plan?" (use a real sender). Expect `search_threads`, then `get_thread`.

Tools the server lists: `create_draft`, `create_label`, `get_message`, `get_thread`, `label_message`, `label_thread`, `list_drafts`, `list_labels`, `search_threads`, `unlabel_message`, `unlabel_thread`.

## Gotchas

- Draft and label tools fail with a permission error. Kiro Crew asks Google only for the read-only scopes above, so adding more on **Data Access** does not change what Connect grants.
- There is no Gmail scope that reads mail without being **restricted**. That only matters if you publish (see the appendix of [google.md](google.md#appendix-publishing-to-other-people)).
- Mail is untrusted input. Google asks clients to screen prompts and responses for prompt injection, so review what agents do with it.

## Sources

- https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server
- https://developers.google.com/workspace/guides/configure-mcp-servers
