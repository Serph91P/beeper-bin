#!/usr/bin/env python3
"""Push one local AUR package directory to the remote repository via SSH.

The script copies PKGBUILD and .SRCINFO into a fresh AUR clone, commits only
when those files changed, and pushes to the AUR master branch. It requires an
explicit known_hosts payload when an SSH key is provided.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


NO_CHANGE_REASONS = {
    "No local changes for AUR package",
    "No AUR_SSH_KEY provided",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push a local PKGBUILD package to AUR")
    parser.add_argument(
        "--package-name",
        default=os.getenv("AUR_PACKAGE_NAME", "beeper-bin"),
        help="AUR package name to push",
    )
    parser.add_argument(
        "--package-dir",
        default=None,
        help="Absolute or relative package directory",
    )
    parser.add_argument(
        "--aur-remote-template",
        default=os.getenv(
            "AUR_REMOTE_TEMPLATE",
            "ssh://aur@aur.archlinux.org/{package}.git",
        ),
        help="Template for AUR git remote with {package}",
    )
    parser.add_argument(
        "--push-ssh-key",
        default=os.getenv("AUR_SSH_KEY", ""),
        help="Private SSH key for AUR access",
    )
    parser.add_argument(
        "--ssh-known-hosts",
        default=os.getenv("AUR_SSH_KNOWN_HOSTS", ""),
        help="Pinned known_hosts content for aur.archlinux.org",
    )
    parser.add_argument(
        "--commit-email",
        default=os.getenv("GIT_COMMIT_EMAIL", "actions@github.com"),
    )
    parser.add_argument(
        "--commit-name",
        default=os.getenv("GIT_COMMIT_NAME", "AUR Update Bot"),
    )
    parser.add_argument(
        "--package-ver",
        default=None,
        help=(
            "Optional package version for commit message. If absent, read from PKGBUILD "
            "and use pkgver."
        ),
    )
    return parser.parse_args()


def _resolve_path(value: str) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    return p


def _read_pkgver(pkgbuild: Path) -> str:
    regex = re.compile(r"^\s*pkgver\s*=\s*['\"]?([^\n'\"]+)")
    for line in pkgbuild.read_text(encoding="utf-8").splitlines():
        match = regex.match(line)
        if match:
            return match.group(1)
    raise RuntimeError("pkgver not found in PKGBUILD")


def _copy_tree(src: Path, dst: Path) -> None:
    for item in src.iterdir():
        if item.name == ".git":
            continue
        target = dst / item.name
        if target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def build_ssh_env(base_env: dict[str, str], key_path: Path, known_hosts_path: Path) -> dict[str, str]:
    env = dict(base_env)
    env["GIT_SSH_COMMAND"] = " ".join(
        [
            "ssh",
            "-i",
            shlex.quote(key_path.as_posix()),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={shlex.quote(known_hosts_path.as_posix())}",
        ]
    )
    return env


def _write_secret_files(workdir: Path, key: str, known_hosts: str) -> tuple[Path, Path]:
    if not known_hosts.strip():
        raise RuntimeError("AUR_SSH_KNOWN_HOSTS is required when AUR_SSH_KEY is set")
    key_path = workdir / "id_ed25519"
    known_hosts_path = workdir / "known_hosts"
    key_path.write_text(key.rstrip() + "\n", encoding="utf-8")
    known_hosts_path.write_text(known_hosts.rstrip() + "\n", encoding="utf-8")
    key_path.chmod(0o600)
    known_hosts_path.chmod(0o644)
    return key_path, known_hosts_path


def push_package(args: argparse.Namespace) -> dict[str, object]:
    package_dir = _resolve_path(args.package_dir) if args.package_dir else _resolve_path(".") / "packages" / args.package_name
    pkgbuild = package_dir / "PKGBUILD"
    srcinfo = package_dir / ".SRCINFO"
    if not pkgbuild.exists():
        raise FileNotFoundError(f"PKGBUILD missing in {package_dir}")
    if not srcinfo.exists():
        raise FileNotFoundError(f".SRCINFO missing in {package_dir}")

    remote = args.aur_remote_template.format(package=args.package_name)
    if not args.push_ssh_key:
        return {"pushed": False, "remote": remote, "reason": "No AUR_SSH_KEY provided"}

    version = args.package_ver or _read_pkgver(pkgbuild)

    with tempfile.TemporaryDirectory(prefix="aur-push-") as workdir_str:
        workdir = Path(workdir_str)
        key_path, known_hosts_path = _write_secret_files(workdir, args.push_ssh_key, args.ssh_known_hosts)
        env = build_ssh_env(os.environ.copy(), key_path, known_hosts_path)

        subprocess.run(
            ["git", "clone", "--depth", "1", remote, (workdir / "aur").as_posix()],
            check=True,
            cwd=workdir,
            env=env,
        )
        aur_dir = workdir / "aur"

        _copy_tree(package_dir, aur_dir)
        subprocess.run(["git", "-C", aur_dir.as_posix(), "config", "user.email", args.commit_email], check=True, env=env)
        subprocess.run(["git", "-C", aur_dir.as_posix(), "config", "user.name", args.commit_name], check=True, env=env)
        subprocess.run(["git", "-C", aur_dir.as_posix(), "add", "PKGBUILD", ".SRCINFO"], check=True, env=env)

        status = subprocess.run(
            ["git", "-C", aur_dir.as_posix(), "status", "--porcelain"],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            return {"pushed": False, "remote": remote, "reason": "No local changes for AUR package"}

        subprocess.run(
            ["git", "-C", aur_dir.as_posix(), "commit", "-m", f"{args.package_name}: update to {version}"],
            check=True,
            env=env,
            text=True,
        )
        subprocess.run(["git", "-C", aur_dir.as_posix(), "push", "origin", "HEAD:master"], check=True, env=env)

    return {"pushed": True, "remote": remote, "reason": None}


def exit_code_for_result(result: dict[str, object]) -> int:
    if result.get("pushed"):
        return 0
    reason = str(result.get("reason") or "")
    return 0 if reason in NO_CHANGE_REASONS else 2


def main() -> int:
    args = parse_args()
    try:
        result = push_package(args)
    except Exception as e:  # pragma: no cover
        print(json.dumps({"pushed": False, "error": str(e)}, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return exit_code_for_result(result)


if __name__ == "__main__":
    raise SystemExit(main())
