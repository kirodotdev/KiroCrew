Kiro BYO configuration samples
==============================

providers.example.json
  Provider connection examples. Real API keys are stored by the configured
  credential backend, not in provider JSON.

native_tools.example.json
  Optional hard execution policy for Kiro BYO native tools. Copy it to
  <Kiro BYO app data>/config/native_tools.json to customize it.

  read_only
    Disables filesystem mutation and shell execution. Read/search and web tools
    can remain available because Crew still owns per-call approval.

  allowed_roots
    Additional canonical filesystem roots that native file tools may access.

  web_enabled
    Hides and disables web_fetch/web_search when false. Web requests reject
    localhost/private/link-local destinations and re-check redirects.

  shell_timeout_seconds / shell_max_output_bytes
    Hard limits for one-shot shell execution.

  process_max_concurrent / process_max_history / process_buffer_bytes
    Hard limits for session-owned long-running processes, retained completed
    process handles, and each stdout/stderr ring buffer.

  web_timeout_seconds / web_max_output_bytes
    Hard limits for outbound native web tools.

Per-tool trust/approval is intentionally NOT configured here. KiroCrew is the
host authority for permission prompts, Side Chat restrictions, hooks and saved
Crewmate permission policy.
