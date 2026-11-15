{
  config,
  lib,
  pkgs,
  hostPkgs,
  ...
}:

let
  cfg = config.bub;
  home = "/home/${cfg.user}";
  isDarwinHost = hostPkgs.stdenv.hostPlatform.isDarwin;

  # vfkit puts stdin into raw mode for `virtio-serial,stdio` and fails when it is not a tty,
  # which is the case once the VM runs in the background. Log the console to a file instead.
  vfkitWrapper = hostPkgs.writeShellScriptBin "vfkit" ''
    args=()
    for arg in "$@"; do
      if [ "$arg" = "virtio-serial,stdio" ]; then
        arg="virtio-serial,logFilePath=$PWD/serial.log"
      fi
      args+=("$arg")
    done
    exec ${lib.getExe hostPkgs.vfkit} "''${args[@]}"
  '';

  bubSession = pkgs.writeShellScriptBin "bub-session" ''
    for _ in $(seq 300); do
      [ -e /run/bub/ready ] && break
      sleep 0.1
    done
    if [ ! -e /run/bub/ready ]; then
      echo "bub-session: project directory is not mounted, see 'journalctl -u bub-workdir'" >&2
      exit 1
    fi

    cd "$(cat /run/bub/meta/workdir)" || exit 1

    if [ -z "''${__NIXOS_SET_ENVIRONMENT_DONE:-}" ]; then
      . /etc/profile
    fi
    if [ -r /run/bub/meta/devenv.sh ]; then
      . /run/bub/meta/devenv.sh
    fi

    if [ $# -eq 0 ]; then
      exec bash -i
    fi
    exec "$@"
  '';
in
{
  options.bub = {
    user = lib.mkOption {
      type = lib.types.str;
      default = "bub";
      description = "Name of the user inside the VM.";
    };

    uid = lib.mkOption {
      type = lib.types.int;
      default = if isDarwinHost then 501 else 1000;
      description = "UID of the VM user. Must match the host user so shared files keep their ownership.";
    };

    diskSize = lib.mkOption {
      type = lib.types.int;
      default = 32 * 1024;
      description = "Size of the sparse per-project root disk in MiB.";
    };

    homeShares = lib.mkOption {
      description = "Host directories mounted into the home directory of the VM user.";
      type = lib.types.attrsOf (
        lib.types.submodule (
          { name, ... }:
          {
            options = {
              source = lib.mkOption {
                type = lib.types.str;
                description = "Host directory. A leading `~` expands to the home directory of the host user.";
              };
              target = lib.mkOption {
                type = lib.types.str;
                description = "Mount point relative to the home directory of the VM user.";
              };
              tag = lib.mkOption {
                type = lib.types.str;
                default = "h-${name}";
                internal = true;
              };
            };
          }
        )
      );
    };

    packages = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = with pkgs; [
        claude-code
        curl
        fd
        file
        git
        gnumake
        jq
        less
        procps
        python3
        ripgrep
        unzip
        wget
      ];
      description = "Packages available inside the VM.";
    };
  };

  config = {
    system.stateVersion = "26.05";
    networking.hostName = "bub";

    nixpkgs.config.allowUnfreePredicate = pkg: lib.getName pkg == "claude-code";

    bub.homeShares.claude = lib.mkDefault {
      source = "~/.local/share/bub-vm/claude";
      target = ".claude";
    };

    microvm = {
      hypervisor = if isDarwinHost then "vfkit" else "qemu";
      vmHostPackages = hostPkgs;
      vcpu = lib.mkDefault 4;
      mem = lib.mkDefault 4096;
      socket = null;
      machineId = "6f1c4b7e-2d1a-4c55-9a3e-8b0d2f7e4a10";

      shares = [
        {
          tag = "ro-store";
          source = "/nix/store";
          mountPoint = "/nix/.ro-store";
          proto = "virtiofs";
          socket = "virtiofs-ro-store.sock";
          readOnly = true;
        }
      ];

      volumes = [
        {
          image = "root.img";
          label = "bub-root";
          mountPoint = "/";
          size = cfg.diskSize;
        }
      ];

      extraArgsScript = "${hostPkgs.coreutils}/bin/printenv BUB_HYPERVISOR_ARGS";
      vfkit.package = vfkitWrapper;
    };

    boot.kernelModules = [ "vmw_vsock_virtio_transport" ];
    boot.kernelParams = [ "systemd.ssh_auto=no" ];
    boot.tmp.useTmpfs = true;

    nix.enable = false;
    documentation.enable = false;

    networking.useNetworkd = true;
    networking.firewall.enable = true;
    systemd.network.networks."10-uplink" = {
      matchConfig.Type = "ether";
      networkConfig.DHCP = "yes";
    };

    users.mutableUsers = false;
    users.allowNoPasswordLogin = true;
    users.users.${cfg.user} = {
      # NixOS only allows uids >= 1000 for normal users, but macOS hosts start at 501.
      isNormalUser = cfg.uid >= 1000;
      isSystemUser = cfg.uid < 1000;
      inherit (cfg) uid;
      inherit home;
      group = "users";
      createHome = true;
      useDefaultShell = true;
      extraGroups = [ "wheel" ];
      subUidRanges = [
        {
          startUid = 100000;
          count = 65536;
        }
      ];
      subGidRanges = [
        {
          startGid = 100000;
          count = 65536;
        }
      ];
    };
    security.sudo.wheelNeedsPassword = false;

    services.openssh = {
      enable = true;
      startWhenNeeded = true;
      openFirewall = false;
      authorizedKeysFiles = [ "/run/bub/meta/authorized_keys" ];
      settings = {
        PasswordAuthentication = false;
        KbdInteractiveAuthentication = false;
        PermitRootLogin = "no";
        AllowAgentForwarding = false;
        AllowStreamLocalForwarding = false;
        AllowTcpForwarding = "local";
        X11Forwarding = false;
        AcceptEnv = [ "COLORTERM" ];
      };
    };
    systemd.sockets.sshd.socketConfig.ListenStream = lib.mkForce [ "vsock::22" ];

    fileSystems = {
      "/run/bub/meta" = {
        device = "meta";
        fsType = "virtiofs";
        options = [ "ro" ];
      };
    }
    // lib.mapAttrs' (
      _: share:
      lib.nameValuePair "${home}/${share.target}" {
        device = share.tag;
        fsType = "virtiofs";
      }
    ) cfg.homeShares;

    systemd.services.bub-workdir = {
      description = "Mount the project directory at its host path";
      wantedBy = [ "multi-user.target" ];
      unitConfig.RequiresMountsFor = [ "/run/bub/meta" ];
      path = [
        pkgs.coreutils
        pkgs.util-linux
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        wd=$(cat /run/bub/meta/workdir)
        case "$wd" in
          /*) ;;
          *) echo "invalid project path: $wd" >&2; exit 1 ;;
        esac
        case "$wd" in
          / | /bin* | /boot* | /dev* | /etc* | /lib* | /nix* | /proc* | /root* | /run* | /sbin* | /sys* | /tmp | /usr* | /var | /var/lib* | /var/log* | ${home} | ${home}/*)
            echo "refusing to mount the project at $wd" >&2
            exit 1
            ;;
        esac
        mkdir -p "$wd"
        mountpoint -q "$wd" || mount -t virtiofs work "$wd"
        touch /run/bub/ready
      '';
    };

    virtualisation.containers.enable = true;
    virtualisation.podman = {
      enable = true;
      dockerCompat = true;
      defaultNetwork.settings.dns_enabled = true;
    };

    programs.git = {
      enable = true;
      config.include.path = "/run/bub/meta/gitconfig";
    };

    environment.enableAllTerminfo = true;
    environment.systemPackages = cfg.packages ++ [ bubSession ];
    environment.variables = {
      DISABLE_AUTOUPDATER = "1";
      DISABLE_TELEMETRY = "1";
      DISABLE_ERROR_REPORTING = "1";
    }
    // lib.optionalAttrs (cfg.homeShares ? claude) {
      CLAUDE_CONFIG_DIR = "${home}/${cfg.homeShares.claude.target}";
    };
    environment.extraInit = ''
      if [ -n "''${XDG_RUNTIME_DIR:-}" ]; then
        export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"
      fi
    '';
  };
}
