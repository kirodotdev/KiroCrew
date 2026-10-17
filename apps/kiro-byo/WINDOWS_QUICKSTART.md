# Kiro BYO 0.1.0 — Windows / KiroCrew 0.9 quick start

Public source-audit targets: **KiroCrew `v0.9.0-insider.6`** and
**`release/0.9.0` / project metadata 0.9.0-rc.1**.

Kiro BYO 0.1.0 is the stable Kiro BYO release line. A local `0.9.0-insider.*` KiroCrew package is accepted only if
the semantic adapter recognizes its source shape and the real-machine verifier /
install / runtime / repair / rollback sequence below succeeds.

## 1. Install/update as a KiroCrew App

Install the folder/zip containing `app.json` as the **Kiro BYO** App. Open:

```text
Kiro BYO → Integration
```

The detected adapter should report the Crew 0.9 adaptive/shared-runtime profile.
Choose **Install / Update integration**, then completely restart KiroCrew.

The adapter is semantic and fail-closed. It inspects every expected Crew seam
before writing and refuses an unknown changed boundary. Startup repair uses the
same `adaptive09` transform.

## 2. Configure provider/model

Use the embedded App UI:

```text
Kiro BYO → Providers
  → provider preset/custom endpoint
  → API Key
  → Save & discover models
  → 模式：Tiga（默认 Multi Type）
```

API keys stay in Windows Credential Manager; provider JSON stores a credential
reference rather than the live secret. Reasoning controls and context-window
metadata appear only when actually known/discovered for the selected model.

## 3. Switch the Agent Harness

After restart:

```text
Settings → Agent Harness → Kiro BYO
```

Create a **new chat/session** after switching. Already-open sessions intentionally
stay on the harness they started with.

After successful integration into a compatible Crew 0.9 package, the expected legacy Kiro BYO capability card is
**9 / 10**:

```text
✓ crew tools in chat
✓ member chat attaches session tools
✗ member fully loads its saved agent
✓ Side Chat read-only tools
✓ subagent continuation
✓ mid-turn steer
✓ manual compact
✓ reasoning effort
✓ model switch
✓ Markdown-defined agents
```

Kiro BYO deliberately does not over-claim Crew's stronger `member_saved_agent`
contract. Markdown-defined agents are now genuine: Crew parses the user-level spec and
projects it into Kiro BYO using the same format-ownership seam as KAS. Its 9/10 result
therefore includes both Crew-owned member-thread dispatch and Markdown-agent loading;
the compact score is still a capability-card summary, not a parity percentage against
native Kiro CLI.

## 4. Native tools expected

```text
read, glob, grep, read_files, list_directory, write, edit, delete_file,
shell, execute_shell_process, control_shell_process, code_symbols,
web_fetch, web_search

`code_query` is additionally advertised when the complete optional Tree-sitter
runtime/grammar set is installed in the Crew backend Python environment.
```

Policy file:

```text
<KIRO_BYO_HOME>\config\native_tools.json
```

Example:

```json
{
  "read_only": false,
  "allowed_roots": ["D:\\APP\\KiroCrewData"],
  "web_enabled": true,
  "shell_timeout_seconds": 120,
  "shell_max_output_bytes": 1048576,
  "process_max_concurrent": 8,
  "process_max_history": 32,
  "process_buffer_bytes": 1048576,
  "web_timeout_seconds": 15,
  "web_max_output_bytes": 2097152
}
```

Kiro BYO has **no per-tool approval override**. Crew's Side Chat policy,
Crewmate Permission setting, hooks and trust rules remain the approval authority.
`read_only`, `allowed_roots`, `web_enabled` and the timeout/size limits are hard
execution boundaries, not replacement approval policy. Old `tool_approval`
configuration is ignored.

## 5. Verify installed Crew 0.9 seams without writing them

From the Kiro BYO source directory:

```powershell
$crew="C:\Users\<you>\AppData\Local\Programs\KiroCrew\resources\backend-dist\kirocrew-backend\Lib\site-packages\kiro_crew"
$env:PYTHONPATH="src"
python scripts\verify_crew09_integration.py --crew-package $crew
```

Expected ending:

```text
Crew-derived score: native Kiro 8/10; Kiro BYO 9/10
PASS: semantic seams compile; source-shape-appropriate Crew selectability contract is active;
derived capabilities and Kiro BYO's evidence-backed contract are consistent.
```

## 6. First real smoke test

Use a **new Kiro BYO session** and verify:

1. read a workspace text file;
2. create a file with `write`;
3. patch it with `edit`;
4. run a harmless PowerShell command with `shell`;
5. access an intended path under an allowed root outside the workspace;
6. try a path outside allowed roots and confirm fail-closed rejection;
7. cancel a long shell or streaming operation;
8. test `/compact`, model switch and reasoning effort;
9. reopen/load a Kiro BYO session and verify projected agent/session state;
10. exercise a configured MCP server and verify tool permission still goes
    through Crew;
11. confirm diagnostics show a real turn/TTFT and real usage/context values when
    the provider supplies them.

If ordinary file writing falls back to Notepad/Computer Use while `write` and
`shell` are available, treat it as a regression and inspect the actual tool and
permission trace.

## Shell security boundary

Filesystem tools are canonical-root/junction/symlink checked. PowerShell itself
runs as the current Windows user after Crew approval. cwd, timeout, cancellation
and process-tree termination are bounded, but this is not an OS-level filesystem
sandbox. Strong shell confinement requires a Windows process-isolation design.
