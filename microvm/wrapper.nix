{
  lib,
  stdenv,
  writeShellApplication,
  coreutils,
  findutils,
  flock,
  gitMinimal,
  openssh,
  perl,
  socat,
  virtiofsd,
  vm,
}:

let
  cfg = vm.config.bub;
  mcfg = vm.config.microvm;
  homeShares = lib.concatMapStrings (share: "${share.tag}\t${share.source}\n") (
    lib.attrValues cfg.homeShares
  );
in
writeShellApplication {
  name = "bub-vm";
  runtimeInputs = [
    coreutils
    findutils
    flock
    gitMinimal
    openssh
    perl
    socat
  ]
  ++ lib.optional stdenv.hostPlatform.isLinux virtiofsd;
  text = ''
    BUB_RUNNER=${mcfg.declaredRunner}
    BUB_HYPERVISOR=${mcfg.hypervisor}
    BUB_GUEST_SYSTEM=${vm.pkgs.stdenv.hostPlatform.system}
    BUB_GUEST_USER=${cfg.user}
    BUB_GUEST_UID=${toString cfg.uid}
    BUB_HOME_SHARES=${lib.escapeShellArg homeShares}
  ''
  + builtins.readFile ./bub-vm.sh;
}
