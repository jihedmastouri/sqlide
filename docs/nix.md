---
title: Nix
description: Building and developing sqlide with the flake.
order: 11
---

The repo ships a `flake.nix`. It is entirely optional — everything in
[Installation](/docs/installation/) still works — but on a machine with
Nix it replaces the system GTK packages, the venv and the driver
`pip install`s with one command.

## Requirements

Nix, with flakes enabled:

```sh
sh <(curl -L https://nixos.org/nix/install) --daemon
mkdir -p ~/.config/nix
echo 'experimental-features = nix-command flakes' >> ~/.config/nix/nix.conf
```

Log out and back in (or open a new terminal) so `nix` is on `PATH`.

## Run it without installing anything

```sh
nix run github:jihedmastouri/sqlide     # or `nix run .` inside the repo
```

This builds sqlide and its whole dependency tree — GTK4, libadwaita,
PyGObject, the database drivers — into `/nix/store`, then launches it.
Nothing is added to your system; `nix store gc` removes it again.

## The dev shell

```sh
nix develop
```

You land in a shell where `python3` already imports `gi`, the drivers
are present, and `ruff`, `pytest`, `sqlite3`, `sqls` and a JDK are on
`PATH`. There is no venv and nothing to install:

```sh
python3 scripts/make_demo_db.py   # writes ./demo.db
python3 -m sqlide                 # run the app
pytest                            # run the tests
ruff check sqlide tests scripts
```

Use `python3 -m pytest` directly rather than `make test`: the Makefile
targets that depend on `venv` build a `.venv`, which the shell makes
redundant.

`make servers` still works from inside the shell, since Docker is a
host daemon rather than something the flake provides.

## Building

```sh
nix build          # result/bin/sqlide, plus the .desktop file and icon
./result/bin/sqlide
```

The built launcher carries its own `GI_TYPELIB_PATH` and
`XDG_DATA_DIRS`, so it runs from anywhere, outside the repo and outside
the dev shell.

## Updating

`flake.lock` pins the exact nixpkgs commit every build resolves
against, which is what makes two machines get the same environment. It
is committed on purpose. To move to newer package versions:

```sh
nix flake update     # rewrites flake.lock
```

## Notes

- Flakes only see files that Git knows about. A newly created file is
  invisible to `nix build` until it is at least `git add`-ed —
  including `flake.nix` itself.
- The package sets `doCheck = false`. The test suite wants live
  MySQL/PostgreSQL servers, which a sandboxed Nix build has no access
  to; run the tests from the dev shell instead.
- If you use [direnv](https://direnv.net/), `echo 'use flake' > .envrc
  && direnv allow` enters the shell automatically on `cd`. `.envrc` is
  gitignored, so that stays a personal choice.
