import shutil
import subprocess
import sys

from .workspace import PROJECT, current


def writable(cwd):
    """Paths a command may write: the project, or only a subagent's worktree.

    A worktree keeps its index under the main repository's .git, so that one
    directory stays writable; the rest of the main tree is read-only to it.
    """
    return [PROJECT] if cwd == PROJECT else [cwd, PROJECT / ".git"]


def profile(cwd):
    """macOS seatbelt profile."""
    allowed = " ".join(f'(subpath "{path}")' for path in writable(cwd))
    return f"""(version 1)
(deny default)
(allow process-exec process-fork signal)
(allow file-read*)
(allow sysctl-read)
(deny network*)
(allow file-write* {allowed} (literal "/dev/null"))
"""


def wrap(command, cwd):
    """Wrap a shell command in an OS sandbox. None means we have no sandbox."""
    if sys.platform == "darwin":
        return ["sandbox-exec", "-p", profile(cwd), "/bin/sh", "-c", command]

    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        binds = [arg for path in writable(cwd) for arg in ("--bind", str(path), str(path))]
        return [
            "bwrap",
            "--ro-bind", "/", "/",
            *binds,
            "--dev", "/dev", "--proc", "/proc",
            "--unshare-net", "--die-with-parent",
            "--chdir", str(cwd),
            "/bin/sh", "-c", command,
        ]

    return None  # Windows, or Linux without bubblewrap


def name():
    if sys.platform == "darwin":
        return "seatbelt"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bubblewrap"
    return "none"


def run(command, timeout=60):
    """Run a command in the caller's workspace, sandboxed when the OS lets us."""
    cwd = current()
    sandboxed = wrap(command, cwd)
    return subprocess.run(
        sandboxed or command,
        shell=sandboxed is None,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
