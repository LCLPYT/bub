"""Managing the VMs of all projects: listing them and deleting the ones that are no longer used."""
import fcntl
import os
import shutil
import sys

from .state import State
from .util import confirm, die, log, try_lock


def human_size(size):
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def human_age(days):
    if days < 1 / 1440:
        return "now"
    if days < 1 / 24:
        return f"{days * 1440:.0f}m ago"
    if days < 1:
        return f"{days * 24:.0f}h ago"
    return f"{days:.0f}d ago"


def project_missing(state):
    workdir = state.workdir()
    return not workdir or not os.path.isdir(workdir)


def describe(state):
    workdir = state.workdir() or "?"
    return f"{workdir} (missing)" if project_missing(state) else workdir


def list_vms():
    states = State.all()
    if not states:
        log("there are no VMs")
        return 0

    rows = []
    total = 0
    for state in sorted(states, key=State.last_used, reverse=True):
        size = state.disk_usage()
        total += size
        status = "running" if state.in_use() else "stopped"
        rows.append((state.digest, status, human_size(size), human_age(state.age_days()), describe(state)))

    header = ("VM", "STATUS", "SIZE", "LAST USED", "PROJECT")
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(4)]
    for row in [header, *rows]:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)) + "  " + row[4])
    print(f"\n{len(rows)} VMs, {human_size(total)} on disk in {states[0].root}")
    return 0


def parse_days(value):
    try:
        days = float(value.removesuffix("d"))
    except ValueError:
        days = -1
    if days < 0:
        die(f"invalid number of days: {value}")
    return days


def unused(state, older_than):
    if state.in_use():
        return False
    return project_missing(state) or (older_than is not None and state.age_days() > older_than)


def delete(state, older_than):
    """Deletes the state dir unless a session started in the meantime."""
    lock = try_lock(state / "start.lock", fcntl.LOCK_EX)
    if lock is None:
        log(f"skipping {state.digest}, it is starting or stopping")
        return False
    with lock:
        if not unused(state, older_than):
            log(f"skipping {state.digest}, it was used in the meantime")
            return False
        shutil.rmtree(state.path)
    return True


def gc(args):
    older_than = None
    assume_yes = False
    while args:
        arg = args.pop(0)
        if arg in ("-y", "--yes"):
            assume_yes = True
        elif arg == "--older-than" and args:
            older_than = parse_days(args.pop(0))
        elif arg.startswith("--older-than="):
            older_than = parse_days(arg.split("=", 1)[1])
        else:
            die(f"unknown argument for --gc: {arg}")

    candidates = [(state, state.disk_usage()) for state in State.all() if unused(state, older_than)]
    if not candidates:
        log("no unused VMs found")
        return 0

    total = sum(size for _, size in candidates)
    for state, size in candidates:
        print(f"{state.digest}  {human_size(size):>7}  {human_age(state.age_days()):>8}  {describe(state)}")

    if not assume_yes:
        if not sys.stdin.isatty():
            die("refusing to delete VMs without a tty, pass --yes")
        if not confirm(f"delete these {len(candidates)} VMs and free {human_size(total)}?"):
            return 1

    freed = sum(size for state, size in candidates if delete(state, older_than))
    log(f"freed {human_size(freed)}")
    return 0
