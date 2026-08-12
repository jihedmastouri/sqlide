# A Nix flake for sqlide.
#
# Two things live here:
#
#   * a *dev shell* (`nix develop`) — a throwaway environment with GTK4,
#     libadwaita, PyGObject, the database drivers and the dev tools on
#     PATH. Nothing is installed into your system, and nothing needs a
#     venv: the Python in the shell already imports `gi`.
#
#   * a *package* (`nix run`, `nix build`) — sqlide built and wrapped so
#     it can be launched without the repo or a Python environment.
#
# The awkward bit of a PyGObject app is that `import gi` needs C
# libraries and their GObject-introspection typelibs at *runtime*, not
# just at build time. `wrapGAppsHook4` is what sorts that out: in the
# package it bakes GI_TYPELIB_PATH/XDG_DATA_DIRS into the launcher, and
# in the dev shell it exports them into your interactive shell.
{
  description = "sqlide — a minimal SQL IDE for SQLite, MySQL and PostgreSQL (GTK4 + libadwaita)";

  inputs = {
    # The package set everything below is drawn from. `nix flake update`
    # moves this forward and records the exact commit in flake.lock —
    # the lock file is what makes builds reproducible, so commit it.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      # Every output has to be defined per-CPU-architecture. This just
      # saves writing the same `x86_64-linux = ...; aarch64-linux = ...;`
      # twice for each one.
      forAllSystems =
        fn: nixpkgs.lib.genAttrs systems (system: fn nixpkgs.legacyPackages.${system});

      # The C libraries. These are what `gi.require_version("Gtk", "4.0")`
      # is really looking for; GtkSourceView is optional (the query
      # console falls back to a plain text view without it).
      guiLibs = pkgs: [
        pkgs.gtk4
        pkgs.libadwaita
        pkgs.gtksourceview5
        pkgs.glib
        pkgs.adwaita-icon-theme
        pkgs.gsettings-desktop-schemas
      ];

      # Runtime Python dependencies — PyGObject plus every optional
      # extra from pyproject.toml. Nix has no notion of `pip install
      # .[all]`, so the extras are simply listed here.
      #
      # (`psycopg` needs no `[binary]` here: the nixpkgs build links
      # against a real libpq already.)
      pythonDeps = ps: [
        ps.pygobject3
        ps.pymysql
        ps.psycopg
        ps.sshtunnel
        ps.keyring
        ps.mcp
        ps.uvicorn
        ps.jaydebeapi
      ];

      # Only needed in the dev shell, never in the built package.
      pythonTestDeps = ps: [
        ps.pytest
        ps.httpx
      ];
    in
    {
      packages = forAllSystems (pkgs: rec {
        default = sqlide;

        sqlide = pkgs.python3Packages.buildPythonApplication {
          pname = "sqlide";
          version = "0.1.0";
          pyproject = true;
          src = ./.;

          build-system = [ pkgs.python3Packages.setuptools ];

          nativeBuildInputs = [
            pkgs.wrapGAppsHook4
            pkgs.gobject-introspection
          ];

          buildInputs = guiLibs pkgs;
          dependencies = pythonDeps pkgs.python3Packages;

          # The standard dance for a Python app that is also a GTK app:
          # let wrapGAppsHook4 collect the GTK environment, but hand the
          # result to the Python launcher wrapper instead of adding a
          # second wrapper on top of it.
          dontWrapGApps = true;
          makeWrapperArgs = [ "\${gappsWrapperArgs[@]}" ];

          # The test suite wants live MySQL/PostgreSQL servers, which a
          # sandboxed build does not have. `nix develop` + `pytest` is
          # where tests belong.
          doCheck = false;

          # setuptools only packages the Python tree, so the desktop
          # entry, icon and AppStream metadata are copied in by hand —
          # this is what makes the app appear in a desktop launcher.
          postInstall = ''
            install -Dm644 data/dev.jihed.sqlide.desktop \
              $out/share/applications/dev.jihed.sqlide.desktop
            install -Dm644 data/dev.jihed.sqlide.metainfo.xml \
              $out/share/metainfo/dev.jihed.sqlide.metainfo.xml
            install -Dm644 data/icons/dev.jihed.sqlide.svg \
              $out/share/icons/hicolor/scalable/apps/dev.jihed.sqlide.svg
          '';

          meta = {
            description = "A minimal SQL IDE for SQLite, MySQL and PostgreSQL";
            mainProgram = "sqlide";
            platforms = systems;
          };
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          # In a shell, wrapGAppsHook4 and gobject-introspection do not
          # wrap anything — they export GI_TYPELIB_PATH, XDG_DATA_DIRS
          # and friends, which is exactly what `python3 -m sqlide` needs.
          nativeBuildInputs = [
            pkgs.wrapGAppsHook4
            pkgs.gobject-introspection
          ];

          buildInputs =
            (guiLibs pkgs)
            ++ [
              (pkgs.python3.withPackages (ps: pythonDeps ps ++ pythonTestDeps ps))

              pkgs.ruff # `make lint` / `make fmt`
              pkgs.sqlite # the `sqlite3` CLI, for poking at demo.db
              pkgs.sqls # optional SQL language server
              pkgs.jdk21 # JayDeBeApi needs a JVM for JDBC
            ];

          shellHook = ''
            export JAVA_HOME=${pkgs.jdk21}
            echo "sqlide dev shell — python $(python3 --version | cut -d' ' -f2)"
            echo "  python3 -m sqlide   run the app"
            echo "  pytest              run the tests (no venv needed here)"
          '';
        };
      });

      # `nix fmt` formats the .nix files in this repo.
      formatter = forAllSystems (pkgs: pkgs.nixfmt-rfc-style);
    };
}
