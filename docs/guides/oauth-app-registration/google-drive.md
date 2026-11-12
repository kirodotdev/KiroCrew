# Google Drive card: Google notes

Set up Google once for every Google card with [google.md](google.md): run the script, three clicks, paste once. This page keeps only what is specific to Google Drive.

| | |
|---|---|
| Redirect URI | `http://127.0.0.1:48103/callback` |
| MCP endpoint | `https://drivemcp.googleapis.com/mcp/v1` |
| APIs the script enables | `drive.googleapis.com` and `drivemcp.googleapis.com` |
| Environment variable prefix | `KIROCREW_CONNECTIONS_GOOGLE_DRIVE_` (`CLIENT_ID`, `CLIENT_SECRET`) |

## Scopes

| Scope | Kiro Crew requests it | What it allows | Tools it serves |
|---|---|---|---|
| `https://www.googleapis.com/auth/drive.readonly` | yes | **Restricted**. View and download all your Drive files | `search_files`, `list_recent_files`, `get_file_metadata`, `get_file_permissions`, `read_file_content`, `download_file_content` |
| `https://www.googleapis.com/auth/drive.file` | no | Non-sensitive. Files the app opened or created | `create_file`, `copy_file` |

Google's page does not map tools to scopes; the mapping follows each scope's description. Scopes on **Data Access** are a ceiling; the consent screen shows what Kiro Crew actually asks for.

## Check it works

After **Connect** shows **Connected**, ask in a chat: "Summarize the file Marketing Plan." (use a real file name). Expect `search_files`, then `read_file_content`.

Tools the server lists: `copy_file`, `create_file`, `download_file_content`, `get_file_metadata`, `get_file_permissions`, `list_recent_files`, `read_file_content`, `search_files`.

## Gotchas

- `create_file` and `copy_file` fail with a permission error. Kiro Crew asks Google only for the read-only scopes above, so adding more on **Data Access** does not change what Connect grants.
- Only files eligible for the Drive MCP server are returned; see Google's Drive MCP file eligibility page.
- A Workspace admin can block third-party access to Drive scopes for the whole organization.

## Sources

- https://developers.google.com/workspace/drive/api/guides/configure-mcp-server
- https://developers.google.com/workspace/guides/configure-mcp-servers
