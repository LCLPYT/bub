{
  description = "bub-vm: run programs sandboxed in a NixOS microVM";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    microvm = {
      url = "github:microvm-nix/microvm.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      microvm,
    }:
    let
      inherit (nixpkgs) lib;

      hostSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      guestSystemFor = lib.replaceStrings [ "-darwin" ] [ "-linux" ];

      mkVm =
        {
          system,
          modules ? [ ],
        }:
        lib.nixosSystem {
          specialArgs.hostPkgs = nixpkgs.legacyPackages.${system};
          modules = [
            microvm.nixosModules.microvm
            self.nixosModules.default
            { nixpkgs.hostPlatform = guestSystemFor system; }
          ]
          ++ modules;
        };

      mkBub =
        args:
        nixpkgs.legacyPackages.${args.system}.callPackage ./wrapper.nix {
          vm = mkVm args;
        };
    in
    {
      lib = { inherit mkVm mkBub; };

      nixosModules.default = ./guest.nix;

      packages = lib.genAttrs hostSystems (system: {
        default = mkBub { inherit system; };
      });

      apps = lib.genAttrs hostSystems (system: {
        default = {
          type = "app";
          program = lib.getExe self.packages.${system}.default;
        };
      });
    };
}
