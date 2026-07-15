"""Filesystem helpers that survive Windows.

Every Null store is a git repo, and git writes its loose objects READ-ONLY
(mode 444). On Windows, DeleteFile refuses a read-only file (WinError 5), so
``shutil.rmtree`` cannot remove any directory containing a ``.git/objects``
tree. POSIX doesn't care — unlink is governed by the *parent* directory's
write bit, not the file's — so this never shows up on Linux/macOS.

That asymmetry bit us twice, and both were silent:

  * ``selftest`` cleaned its throwaway store with ``ignore_errors=True``, so
    on Windows the store was never deleted and the failure was swallowed —
    every ``null selftest`` run leaked a store dir into TEMP (13 had piled
    up on this seat before anyone noticed).
  * ``multiverse.delete(remove_files=True)`` called bare ``shutil.rmtree``,
    so deleting a personality raised PermissionError on Windows.

Use :func:`force_rmtree` for ANY tree that might contain a git repo.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import time


def _chmod_and_retry(func, path, exc) -> None:
    """rmtree error hook: clear the read-only bit, then redo the delete.

    Signature is shared by rmtree's ``onerror`` (3.11: gets an exc_info tuple)
    and ``onexc`` (3.12+: gets the exception) — both pass three positionals.

    Two things this must NOT do, both of which crashed the naive version:
      * Re-raise on a file that already vanished — git background maintenance
        creates/removes lock files under .git/objects and the tree walk races
        them. FileNotFoundError = already gone = success.
      * Blindly ``func(path)``. ``func`` is whatever op failed; the read-only
        removal case is os.unlink/os.rmdir (a bare path), but rmtree's
        fd-based walk can fail in ``os.open``/``os.scandir``, which need extra
        args — re-invoking those with just ``path`` raises TypeError (seen on
        a PermissionError on .git/objects). Only retry the removal ops; for
        the rest the chmod is the fix and force_rmtree's outer retry re-walks.
    """
    exc_obj = exc[1] if isinstance(exc, tuple) else exc
    if isinstance(exc_obj, FileNotFoundError):
        return
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        return
    if func in (os.unlink, os.remove, os.rmdir):
        try:
            func(path)
        except OSError:
            pass


def force_rmtree(
    path: str, *, missing_ok: bool = True, retries: int = 10, delay: float = 0.1
) -> bool:
    """Remove a directory tree, clearing read-only bits as needed.

    Two Windows-only obstacles, both of which POSIX shrugs off:

      * READ-ONLY FILES (git's loose objects) -- handled by the chmod hook.
      * OPEN HANDLES. Windows refuses to delete a file any process still has
        open, and handle release is ASYNCHRONOUS: a child that has just been
        killed can keep the file locked for a short window after it dies. So
        a single rmtree can lose a race it would win a moment later. We retry
        with a short backoff rather than giving up.

    Retrying is a backstop, not a licence to leak handles -- the owner should
    still close/reap what it opened (see selftest._shutdown). But a caller
    cannot control the OS's release latency, and silently leaving the tree
    behind is worse than waiting 1s for it.

    Returns True if the tree is gone afterwards. With ``missing_ok`` a
    non-existent path is a no-op success. Never raises: callers run this in
    ``finally`` blocks, where raising would mask the real result.
    """
    if not os.path.exists(path):
        return bool(missing_ok)

    for attempt in range(retries):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_chmod_and_retry)
            else:
                shutil.rmtree(path, onerror=_chmod_and_retry)
            return not os.path.exists(path)
        except OSError:
            if attempt == retries - 1:
                break
            time.sleep(delay)  # a dying process is still letting go

    # Last resort: don't let cleanup crash the caller. Returns False, so a
    # caller that cares (a test, a leak check) can still see it failed.
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.exists(path)
