"""The per-project state directory and the identity (CID, MAC) derived from its hash."""
import fcntl
import hashlib
import os
import re
import time
from pathlib import Path

from .util import pid_alive, read_text, try_lock

DIGEST = re.compile(r"[0-9a-f]{12}")


def state_root():
    state_home = os.environ.get("XDG_STATE_HOME") or f"{os.environ['HOME']}/.local/state"
    return Path(state_home) / "bub-vm"


def digest_of(workdir):
    return hashlib.sha256(workdir.encode()).hexdigest()[:12]


class State:
    def __init__(self, root, digest):
        self.root = root
        self.digest = digest
        self.path = root / digest
        self.meta = self.path / "meta"
        self.key = self.path / "id_ed25519"
        self.cid = int(digest[:7], 16) + 3
        self.mac = "02:" + ":".join(digest[i:i + 2] for i in range(0, 10, 2))

    @classmethod
    def for_workdir(cls, workdir):
        return cls(state_root(), digest_of(workdir))

    @classmethod
    def all(cls):
        root = state_root()
        try:
            entries = sorted(root.iterdir())
        except FileNotFoundError:
            return []
        return [cls(root, entry.name) for entry in entries if DIGEST.fullmatch(entry.name) and entry.is_dir()]

    def __truediv__(self, name):
        return self.path / name

    def exists(self):
        return self.path.is_dir()

    def workdir(self):
        """The project path recorded at the last session start, or "" if it was never recorded."""
        return read_text(self / "workdir")

    def last_used(self):
        """Start time of the last session, since every session start rewrites the workdir file."""
        for path in (self / "workdir", self.path):
            try:
                return path.stat().st_mtime
            except OSError:
                pass
        return 0.0

    def recorded_pid(self):
        try:
            return int(read_text(self / "vm.pid"))
        except ValueError:
            return None

    def in_use(self):
        """Whether a VM of this state dir is running or a session is active, as seen from another process."""
        pid = self.recorded_pid()

        if pid is not None and pid_alive(pid):
            return True

        if not (self / "sessions.lock").exists():
            return False

        lock = try_lock(self / "sessions.lock", fcntl.LOCK_EX)

        if lock is None:
            return True

        lock.close()

        return False

    def disk_usage(self):
        """Allocated bytes, which for the sparse root disk is far less than its apparent size."""
        total = 0

        for directory, dirs, files in os.walk(self.path):
            for name in dirs + files:
                try:
                    total += os.lstat(os.path.join(directory, name)).st_blocks * 512
                except OSError:
                    pass
        return total

    def age_days(self):
        return (time.time() - self.last_used()) / 86400
