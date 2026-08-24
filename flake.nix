{
  description = "Stnt Phase 1C development and checks";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "aarch64-darwin";
      pkgs = import nixpkgs { inherit system; };
      developmentPackages = [ pkgs.bash pkgs.git pkgs.jq pkgs.python3 pkgs.util-linux ];
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = developmentPackages;
      };

      checks.${system}.phase1c = pkgs.runCommand "stnt-phase1c-check" {
        nativeBuildInputs = developmentPackages;
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
    };
}
