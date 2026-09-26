"""Putting bytes at a path that must not already hold them.

Both directions of the durability pair write a file this way, and both write it for
the same reason: the bytes came from the bucket, something else may be writing the
same name, and an existing file is always the copy to keep. So the mechanism lives
here once.

Only the MECHANISM is shared. Each caller keeps its own refusal message and its own
exception type, because what an existing file or a redirected parent MEANS differs:
for the front it decides whether a customer's turn may be served, and for the restore
step it decides whether the task may boot. A shared helper that also decided how to
complain would have to be told, which is the same thing as leaving it to the caller.

The three properties, all load-bearing:

* **Atomic.** The bytes land in a temporary file in the same directory, are flushed and
  fsynced, and then appear at the target under one name. A crash cannot leave a
  truncated file for something else to read or append to.
* **Never an overwrite.** ``os.link`` refuses an existing target, so "do not clobber"
  is a filesystem guarantee rather than a check with a window after it. It also refuses
  a symlink at the target without following it, so a link planted there cannot receive
  the bytes.
* **The published inode is the one we wrote.** The link names the temporary's still-open
  DESCRIPTOR rather than its pathname, and the destination directory is a descriptor too.
  Closing the temporary first and linking it by name would publish whatever that name
  pointed at by then: these directories are ones the agent writes in, so a concurrent
  turn could replace the temporary between the close and the link and have its own inode
  published under the target name, receiving every later write.
* **One directory throughout.** The temporary is CREATED through the same descriptor the
  link publishes into, so both halves address one directory inode. Creating it by path
  instead left a window: a concurrent rename of that directory between opening the
  descriptor and creating the temporary put the bytes in the replacement directory while
  the link published into the detached old one, so the backend would start from an empty
  history and the backup would then overwrite the bucket with it.
* **No temporary left behind.** The temporary is unlinked on every path out, including
  the one where the link succeeded and it has served its purpose.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

__all__ = ["link_new"]

#: Random bytes in a temporary's name. Long enough that an attacker cannot pre-create the
#: name to make ``O_EXCL`` fail, which would be a denial of service on the write.
_TEMP_NAME_BYTES = 8


def link_new(path: Path, data: bytes, *, prefix: str) -> bool:
    """Create *path* holding *data*. ``True`` if it landed, ``False`` if it existed.

    ``False`` is not an error and is deliberately not raised: an existing file is the
    newer copy in both callers, so the answer they need is "was it already there", not
    a failure to handle. Any other problem -- an unwritable directory, a full disk --
    raises ``OSError``, for the caller to translate into its own refusal.

    *prefix* names the temporary, so a caller's own leftover is recognisable as its
    own. It is required rather than defaulted, because the two callers write into
    different directories and a shared temp name is a shared thing to clean up.
    """
    parent_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        tmp_name = f"{prefix}{secrets.token_hex(_TEMP_NAME_BYTES)}.tmp"
        # O_EXCL through the pinned descriptor: the file is created in the directory this
        # function already holds open, not in whatever that path resolves to now.
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            with os.fdopen(os.dup(fd), "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                # The source is this descriptor's inode, reached through procfs and
                # followed deliberately, so the bytes published are the bytes written.
                # The destination is resolved inside the directory we opened, so the
                # parent cannot be swapped underneath the link either.
                os.link(
                    f"/proc/self/fd/{fd}",
                    path.name,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=True,
                )
            except FileExistsError:
                return False
            return True
        finally:
            os.close(fd)
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent_fd)
