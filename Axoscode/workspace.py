"""Where a tool call works: the project, or a subagent's private git worktree.

Every file and shell tool resolves paths through here, so a writing subagent
edits its own copy of the repository and can never touch the main tree. Its
changes come back to the main agent as a patch file.
"""

import subprocess
import tempfile
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

PROJECT = Path.cwd().resolve()

_root = ContextVar("workspace_root", default=PROJECT)
_worktree_lock = threading.Lock()  # git serialises worktree bookkeeping badly


def current():
    """The directory the calling agent works in."""
    return _root.get()


@contextmanager
def use(root):
    """Run the enclosed tool calls against `root` (this thread only)."""
    token = _root.set(root)
    try:
        yield
    finally:
        _root.reset(token)


def resolve(path):
    """Map a path into the caller's working tree.

    Relative paths land under the current root. Absolute paths that point
    into the main project are re-rooted too, so a path a subagent picked up
    from the parent's conversation still lands in its own worktree.
    """
    root = current()
    target = Path(path)
    if not target.is_absolute():
        return root / target
    try:
        return root / target.resolve().relative_to(PROJECT)
    except ValueError:
        return target


def git(*args, cwd=PROJECT):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def snapshot():
    """A commit id for the project as it is now, uncommitted edits included.

    None when there is nothing to base a worktree on (no repository, or no
    commits yet). Untracked files are not part of the snapshot.
    """
    if git("rev-parse", "--verify", "HEAD").returncode:
        return None
    stash = git("stash", "create").stdout.strip()
    return stash or git("rev-parse", "HEAD").stdout.strip()


def create(base):
    """Check `base` out into a fresh worktree outside the project."""
    path = Path(tempfile.mkdtemp(prefix="axos-worktree-")).resolve()
    with _worktree_lock:
        added = git("worktree", "add", "--detach", str(path), base)
    if added.returncode:
        raise RuntimeError(f"could not create a worktree: {added.stderr.strip()}")
    return path


def collect(tree, base):
    """Everything the subagent changed: (changed files, patch file path)."""
    git("add", "-A", cwd=tree)
    patch = git("diff", "--cached", "--binary", base, cwd=tree).stdout
    if not patch:
        return [], ""

    files = git("diff", "--cached", "--name-status", base, cwd=tree).stdout
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix="axos-", suffix=".patch", delete=False
    )
    handle.write(patch)
    handle.close()
    return [line.replace("\t", " ") for line in files.splitlines()], handle.name


def remove(tree):
    with _worktree_lock:
        git("worktree", "remove", "--force", str(tree))
