"""Running a program in the VM, sharing the VM between sessions and stopping it after the last one."""
import fcntl
import shlex
import signal
import subprocess
import sys

from .devshell import prepare_devshell
from .shares import collect_shares, validate, write_mounts
from .ssh import ssh_command, ssh_env
from .util import env_flag, log, open_lock, read_text

SESSION = "/run/current-system/sw/bin/bub-session"


def exit_on_signal(signum, _frame):
    sys.exit(128 + signum)


def interactive(vm, command):
    remote = SESSION
    if command:
        remote += " " + shlex.join(command)
    tty = "-t" if sys.stdin.isatty() and sys.stdout.isatty() else "-T"

    options = [tty]
    if vm.config["forwardEnv"]:
        options += ["-o", f"SendEnv={' '.join(vm.config['forwardEnv'])}"]

    process = subprocess.Popen(ssh_command(vm, *options, "bub-vm", remote), env=ssh_env())

    def forward(signum, _frame):
        process.send_signal(signum)

    handled = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)
    previous = {signum: signal.signal(signum, forward) for signum in handled}
    try:
        code = process.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    return 128 - code if code < 0 else code


def finish(vm, start_lock, sessions_lock):
    """Stops the VM if this was the last session, i.e. no other session holds a shared lock."""
    fcntl.flock(sessions_lock, fcntl.LOCK_UN)
    if env_flag("BUB_KEEP_RUNNING", vm.config["keepRunning"]):
        return

    fcntl.flock(start_lock, fcntl.LOCK_EX)
    try:
        fcntl.flock(sessions_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        vm.stop()
    fcntl.flock(start_lock, fcntl.LOCK_UN)


def run_session(vm, command):
    validate(vm)
    vm.setup_state()
    shares = collect_shares(vm)
    write_mounts(vm, shares)
    prepare_devshell(vm)

    start_lock = open_lock(vm.state / "start.lock", fcntl.LOCK_EX)
    if vm.vm_pid() is None:
        vm.start(shares)
        log("waiting for the VM to boot")
    elif read_text(vm.state / "vm.runner") != vm.runner:
        log("the running VM uses an older build of bub-vm, run 'bub-vm --stop' to restart it")

    sessions_lock = open_lock(vm.state / "sessions.lock", fcntl.LOCK_SH)
    fcntl.flock(start_lock, fcntl.LOCK_UN)

    signal.signal(signal.SIGHUP, exit_on_signal)
    signal.signal(signal.SIGTERM, exit_on_signal)
    try:
        vm.wait_ssh()
        return interactive(vm, command)
    finally:
        finish(vm, start_lock, sessions_lock)
