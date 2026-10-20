"""The owner modules :mod:`kiro_crew.members` is composed from.

``kiro_crew.members`` is the import path and the patch surface of the per-crew-member
space: the dashboard, the chat paths and the tests import its names from it and patch
them there. Each module here owns one responsibility; the facade imports them lowest
layer first, and a module imports only the ones before it, never the facade:

* ``member_space`` -- the space's layout (every file and directory name it holds), member
  display names and crew ids, slugs, the containment-checked member paths, and the
  first-welcome marker a new member's directory carries;
* ``member_sessions`` -- a member's pinned DM session: its key and alias, the backend it
  routes to, the session-level MCP mounts, and the per-turn lifecycle verdict;
* ``dm_bindings`` -- the crew-to-thread binding under ``trust/``, and the identity it
  answers for a session;
* ``self_edit`` -- the fields a crewmate may change on its own record, their cleaning, the
  update, its undo and the rename;
* ``prompt_layers`` -- the two file layers of the member system prompt: the user-owned
  permanent rules and the member-owned briefing;
* ``member_activity`` -- the activity pointers a member's sessions leave, and their read.
"""
