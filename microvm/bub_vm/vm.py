"""Lifecycle of the VM of one project: state setup, starting the hypervisor, waiting for SSH and stopping."""
import os
import shutil
import signal
import subprocess
import sys
import time

from .ssh import ssh_quiet
from .state import State
from .util import die, kill, log, pid_alive, read_text, tail, wait_until

POWEROFF = "/run/wrappers/bin/sudo /run/current-system/sw/bin/systemctl poweroff"
# Primary group of the guest user ("users"). Unprivileged virtiofsd can't create files as another gid.
GUEST_GID = 100


class Vm:
    def __init__(self, config, workdir):
        self.config = config
        self.hypervisor = config["hypervisor"]
        self.runner = config["runner"]
        self.home = os.environ["HOME"]
        self.workdir = workdir
        self.state = State.for_workdir(workdir)

        # Processes started by this invocation. Once they exit they stay zombies until reaped,
        # so a plain kill(pid, 0) would still report them as running.
        self.children = {}

    def setup_state(self):
        state = self.state
        state.meta.mkdir(parents=True, exist_ok=True)
        state.path.chmod(0o700)
        state.meta.chmod(0o700)
        (state / "workdir").write_text(f"{self.workdir}\n")
        (state.meta / "workdir").write_text(f"{self.workdir}\n")

        if not state.key.exists():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "bub-vm", "-f", str(state.key)],
                           check=True)

        authorized_keys = state.meta / "authorized_keys"
        shutil.copyfile(f"{state.key}.pub", authorized_keys)
        authorized_keys.chmod(0o600)

    def detach(self, logname, command, env=None):
        """Starts a command in a new session, so terminal signals (Ctrl-C, hangup) don't reach it.

        The command runs in the state dir, which microvm.nix expects for relative paths.
        """
        with open(self.state / logname, "ab") as logfile:
            try:
                process = subprocess.Popen(command, cwd=self.state.path, env=env, stdin=subprocess.DEVNULL,
                                           stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as error:
                die(f"failed to start {command[0]}: {error}")
        self.children[process.pid] = process
        return process.pid

    def alive(self, pid):
        child = self.children.get(pid)

        if child is not None:
            return child.poll() is None

        return pid_alive(pid)

    def vm_pid(self):
        pid = self.state.recorded_pid()

        return pid if pid is not None and self.alive(pid) else None

    def start_virtiofsd(self, tag, source, readonly=False):
        command = ["virtiofsd", f"--socket-path=virtiofs-{tag}.sock", f"--shared-dir={source}",
                   "--sandbox=none", "--cache=auto", f"--translate-gid=map:{GUEST_GID}:{os.getgid()}:1"]
        if readonly:
            command.append("--readonly")

        pid = self.detach("virtiofsd.log", command)

        with open(self.state / "virtiofsd.pids", "a") as pids:
            pids.write(f"{pid}\n")

    def qemu_args(self, shares):
        self.start_virtiofsd("ro-store", "/nix/store", readonly=True)

        sockets = [self.state / "virtiofs-ro-store.sock"]

        args = ["-device", f"vhost-vsock-pci,guest-cid={self.state.cid}"]

        if self.config["network"]:
            args += [
                "-netdev", "user,id=bubnet",
                "-device", f"virtio-net-pci,netdev=bubnet,mac={self.state.mac}",
            ]

        for tag, source, readonly, _target in shares:
            self.start_virtiofsd(tag, source, readonly)

            sockets.append(self.state / f"virtiofs-{tag}.sock")

            args += [
                "-chardev", f"socket,id=bub-{tag},path=virtiofs-{tag}.sock",
                "-device", f"vhost-user-fs-pci,chardev=bub-{tag},tag={tag}",
            ]

        if not wait_until(lambda: all(socket.is_socket() for socket in sockets), 100, 0.05):
            die(f"virtiofsd did not start, see {self.state.path}/virtiofsd.log")

        return args

    def vfkit_args(self, shares):
        args = ["--device", f"virtio-vsock,port=22,socketURL={self.state.path}/ssh.sock,connect"]

        if self.config["network"]:
            args += ["--device", f"virtio-net,nat,mac={self.state.mac}"]

        for tag, source, _readonly, _target in shares:
            args += ["--device", f"virtio-fs,sharedDir={source},mountTag={tag}"]

        return args

    def remove_sockets(self):
        for socket in self.state.path.glob("*.sock"):
            socket.unlink(missing_ok=True)

    def start(self, shares):
        log(f"starting VM for {self.workdir}")

        self.remove_sockets()

        (self.state / "serial.log").unlink(missing_ok=True)
        (self.state / "virtiofsd.pids").unlink(missing_ok=True)
        (self.state / "vm.log").write_bytes(b"")

        if self.hypervisor == "qemu":
            args = self.qemu_args(shares)
        elif self.hypervisor == "vfkit":
            args = self.vfkit_args(shares)
        else:
            die(f"unsupported hypervisor {self.hypervisor}")

        # microvm.nix word-splits the output of extraArgsScript, which prints this variable.
        env = dict(os.environ, BUB_HYPERVISOR_ARGS=" ".join(args))
        pid = self.detach("vm.log", [f"{self.runner}/bin/microvm-run"], env=env)
        (self.state / "vm.pid").write_text(f"{pid}\n")
        (self.state / "vm.runner").write_text(f"{self.runner}\n")

    def wait_ssh(self):
        for _ in range(300):
            if ssh_quiet(self, "-o", "ConnectTimeout=2", "bub-vm", "true") == 0:
                return

            if self.vm_pid() is None:
                log("the VM exited, last console output:")
                tail(self.state / "vm.log", 40)
                tail(self.state / "serial.log", 40)
                sys.exit(1)

            time.sleep(0.3)

        die("the VM did not become reachable, see 'bub-vm --log'")

    def stop(self):
        pid = self.vm_pid()

        if pid is None:
            return

        log("stopping VM")
        ssh_quiet(self, "-o", "ConnectTimeout=5", "bub-vm", POWEROFF)

        if not wait_until(lambda: not self.alive(pid), 100, 0.3):
            log("the VM did not shut down, terminating it")
            kill(pid, signal.SIGTERM)
            time.sleep(2)

            if self.alive(pid):
                kill(pid, signal.SIGKILL)

        for line in read_text(self.state / "virtiofsd.pids").split():
            if line.isdigit():
                kill(int(line), signal.SIGTERM)

        for name in ("vm.pid", "virtiofsd.pids"):
            (self.state / name).unlink(missing_ok=True)

        self.remove_sockets()
