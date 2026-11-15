"""Command line entry point of bub-vm."""
import fcntl
import json
import os
import shutil
import sys
from pathlib import Path

from .manage import gc, list_vms
from .session import run_session
from .util import confirm, die, open_lock
from .vm import Vm

USAGE = """\
Usage: bub-vm [program [args...]]  run a program (default: bash) in the VM of the current directory
       bub-vm --stop               stop the VM of the current directory
       bub-vm --destroy            stop the VM and delete its disk and state
       bub-vm --log                print the console log of the VM
       bub-vm --list               list the VMs of all projects with their disk usage
       bub-vm --gc [--older-than DAYS] [--yes]
                                   delete stopped VMs whose project directory no longer exists,
                                   and with --older-than also those not used for DAYS days

Environment (overrides bub.devShell.enable and bub.keepRunning):
  BUB_DEVSHELL=0|1       don't load / load the devShell of the project flake
  BUB_KEEP_RUNNING=0|1   stop / keep the VM running after the last session ends
"""


def stop(vm):
    if vm.state.exists():
        _lock = open_lock(vm.state / "start.lock", fcntl.LOCK_EX)
        vm.stop()
    return 0


def destroy(vm):
    if not vm.state.exists():
        die(f"there is no VM for {vm.workdir}")
    if sys.stdin.isatty() and not confirm(f"delete the VM disk and state of {vm.workdir}?"):
        return 1
    _lock = open_lock(vm.state / "start.lock", fcntl.LOCK_EX)
    vm.stop()
    shutil.rmtree(vm.state.path)
    return 0


def print_log(vm):
    logs = [vm.state / "vm.log", vm.state / "serial.log"]
    if not any(path.is_file() for path in logs):
        die(f"there is no log for {vm.workdir}")
    for path in logs:
        if path.is_file():
            sys.stdout.buffer.write(path.read_bytes())
    return 0


def main(args):
    first = args[0] if args else ""
    if first in ("-h", "--help"):
        sys.stderr.write(USAGE)
        return 0
    if first == "--list":
        return list_vms()
    if first == "--gc":
        return gc(args[1:])

    config_path = os.environ.get("BUB_VM_CONFIG")
    if not config_path:
        die("BUB_VM_CONFIG is not set")
    vm = Vm(json.loads(Path(config_path).read_text()), os.getcwd())

    commands = {"--stop": stop, "--destroy": destroy, "--log": print_log}
    if first in commands:
        return commands[first](vm)

    if first == "--":
        args = args[1:]
    elif first.startswith("-"):
        sys.stderr.write(USAGE)
        return 1

    return run_session(vm, args)


def run():
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
