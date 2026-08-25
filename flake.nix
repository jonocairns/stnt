{
  description = "Stnt Phase 1C development and checks";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "aarch64-darwin" "x86_64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
      developmentPackages = pkgs: [ pkgs.bash pkgs.git pkgs.jq pkgs.python3 pkgs.util-linux ];
    in {
      devShells = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            packages = developmentPackages pkgs;
          };
        });

      checks = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          phase1c = pkgs.runCommand "stnt-phase1c-check" {
            nativeBuildInputs = developmentPackages pkgs;
          } ''
            cp -R ${self} source
            chmod -R u+w source
            cd source
            patchShebangs bin
            python3 -m unittest discover -s tests -v
            bin/phase0b-test
            bin/phase0c-test
            bin/stack-finish-test
            touch $out
          '';
        });
    };
}
