{ lib, pkgs, ... }:

{
  tree-root-file = "treefmt.nix";
  on-unmatched = "fatal";

  excludes = [
    "*.lock"
    "*.md"
    ".gitignore"
    "LICENSE"
    # Digest-named source-review receipts are immutable byte contracts.
    "source-reviews/paperclip/*.json"
  ];

  formatter.nixfmt = {
    command = lib.getExe pkgs.nixfmt;
    includes = [ "*.nix" ];
    options = [ "--strict" ];
  };

  formatter.prettier = {
    command = lib.getExe pkgs.prettier;
    includes = [
      "*.js"
      "*.yml"
    ];
    options = [
      "--write"
      "--print-width=120"
      "--arrow-parens=avoid"
    ];
  };
  formatter.python = {
    command = lib.getExe pkgs.ruff;
    includes = [ "*.py" ];
    options = [ "format" ];
  };
}
