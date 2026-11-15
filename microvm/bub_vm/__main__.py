"""Launcher, run as a script with `python -I`, which doesn't put the script directory on sys.path."""
import os
import sys

if __package__:
    from .cli import run
else:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    from bub_vm.cli import run

run()
