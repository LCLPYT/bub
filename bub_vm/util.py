"""Small helpers shared by all modules: logging, path checks, files, processes and locks."""
import fcntl
import os
import re
import sys
import time

UNSAFE_CHARS = re.compile(r"[\s,='\"\\$`*?\[\]]")


def log(message):
    print(f"[bub-vm] {message}", file=sys.stderr, flush=True)


def die(message):
    log(f"error: {message}")
    sys.exit(1)


def confirm(question):
    sys.stderr.write(f"[bub-vm] {question} [y/N] ")
    sys.stderr.flush()

    return sys.stdin.readline().strip() in ("y", "Y", "yes")


def matches(path, patterns):
    """Matches like a shell `case`, where a trailing `*` matches any suffix."""
    for pattern in patterns:
        if pattern.endswith("*") and path.startswith(pattern[:-1]):
            return True
        if path == pattern:
            return True
    return False


def check_path(what, path):
    if UNSAFE_CHARS.search(path):
        die(f"{what} contains characters that can't be passed to the hypervisor: {path}")


def env_flag(name, default):
    value = os.environ.get(name, "")
    return default if value == "" else value == "1"


def wait_until(predicate, attempts, delay):
    for _ in range(attempts):
        if predicate():
            return True
        time.sleep(delay)
    return predicate()


def read_text(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def tail(path, count):
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return

    print(f"==> {path} <==", file=sys.stderr)

    for line in lines[-count:]:
        print(line, file=sys.stderr)


def pid_alive(pid):
    """Only valid for processes that are not our own children, which stay zombies until reaped."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def kill(pid, signum):
    try:
        os.kill(pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def open_lock(path, operation):
    lock = open(path, "a")
    fcntl.flock(lock, operation)
    return lock


def try_lock(path, operation):
    """Like open_lock, but returns None instead of blocking if the lock is held elsewhere."""
    lock = open(path, "a")

    try:
        fcntl.flock(lock, operation | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock
