"""A real-kill restart harness for the task store.

``harness.py`` starts a child process (``test/fixtures/crash_child.py``) that
drives the store to a named failpoint, then ends it with ``SIGKILL``;
``reopen.py`` opens the file again as the next incarnation. The tests that use
them are in ``test/test_taskq_real_crash.py``.
"""
