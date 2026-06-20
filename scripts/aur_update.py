#!/usr/bin/env python3
"""Utility to maintain beeper-v4 AUR package metadata.

The script updates pkgver/source/sha256sums in the target PKGBUILD, regenerates
.SRCINFO via makepkg, and optionally pushes the changed tree to AUR using SSH.

All required runtime settings are read from environment variables. No secrets are
hardcoded in this repository.
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
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_UPSTREAM_QUERY_URL = (
    "https://api.beeper.com/desktop/update-feed.json"
    "?bundleID=com.automattic.beeper.desktop"
    "&version=0.0.1"
    "&platform=linux"
    "&arch=x64"
    "&channel=stable"
)


UA = "beeper-aur-auto-updater/1.0"


@dataclass
class UpdateResult:
    package_name: str
    package_dir: Path
    old_pkgver: str
    new_pkgver: str
    old_source: str | None
    new_source: str
    old_sha256sums: tuple[str, ...]
    new_sha256: str
    changed: bool
    srcinfo_generated: bool


@dataclass
class PushResult:
    pushed: bool
    remote: str | None = None
    reason: str | None = None


def _to_path(value: str | None) -> Path:
    if not value:
        raise ValueError("path is empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def _read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Beeper AUR package metadata.")
    parser.add_argument(
        "--package-name",
        default=os.getenv("AUR_PACKAGE_NAME", "beeper-bin"),
        help="AUR package name (also used for package subfolder)",
    )
    parser.add_argument(
        "--package-dir",
        default=None,
        help="Path to package directory, defaults to packages/<AUR_PACKAGE_NAME>",
    )
    parser.add_argument(
        "--query-url",
        default=os.getenv("UPSTREAM_QUERY_URL", DEFAULT_UPSTREAM_QUERY_URL),
        help=(
            "URL for Beeper's update-feed JSON or a download endpoint that resolves "
            "to the latest binary artifact"
        ),
    )
    parser.add_argument(
        "--version-regex",
        default=os.getenv("UPSTREAM_VERSION_REGEX", r"Beeper-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"),
        help="Regex applied to resolved artifact URL or filename to extract version",
    )
    parser.add_argument(
        "--srcinfo-command",
        default=os.getenv("SRCINFO_COMMAND", "makepkg"),
        help="Command used to generate .SRCINFO",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report planned changes without writing files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write JSON summary to stdout",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push updated files to AUR after writing .SRCINFO",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Disable push even if --push was set by environment",
    )
    parser.add_argument(
        "--push-ssh-key",
        default=os.getenv("AUR_SSH_KEY", ""),
        help="Private SSH key content for git push to AUR",
    )
    parser.add_argument(
        "--ssh-known-hosts",
        default=os.getenv("AUR_SSH_KNOWN_HOSTS", ""),
        help="Pinned known_hosts content for aur.archlinux.org",
    )
    parser.add_argument(
        "--aur-remote-template",
        default=os.getenv(
            "AUR_REMOTE_TEMPLATE",
            "ssh://aur@aur.archlinux.org/{package}.git",
        ),
        help="Template for AUR remote URL; {package} is replaced by AUR package name",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("HTTP_TIMEOUT", "30")),
        help="Timeout in seconds for upstream HTTP requests",
    )
    parser.add_argument(
        "--commit-email",
        default=os.getenv("GIT_COMMIT_EMAIL", "actions@github.com"),
        help="Git user email for AUR push commit",
    )
    parser.add_argument(
        "--commit-name",
        default=os.getenv("GIT_COMMIT_NAME", "AUR Update Bot"),
        help="Git user name for AUR push commit",
    )
    return parser.parse_args()


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    repo_root = Path(__file__).resolve().parents[1]
    package_dir = _to_path(args.package_dir) if args.package_dir else repo_root / "packages" / args.package_name
    pkgbuild_path = package_dir / "PKGBUILD"
    srcinfo_path = package_dir / ".SRCINFO"
    if not pkgbuild_path.exists():
        raise FileNotFoundError(f"PKGBUILD missing: {pkgbuild_path}")
    return pkgbuild_path, srcinfo_path


def _fetch_final_url(url: str, timeout: int = 30) -> str:
    """Resolve redirect chain and return final URL.

    Uses HEAD first for efficiency and GET as fallback.
    """
    headers = {"User-Agent": UA}
    for method in ("HEAD", "GET"):
        request = Request(url, headers=headers, method=method)
        if method == "GET":
            request.add_header("Range", "bytes=0-0")
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.geturl()
        except HTTPError as exc:
            if method == "HEAD" and exc.code in {403, 405, 501}:  # fallback to GET
                continue
            raise
        except URLError:
            continue
    raise RuntimeError(f"Could not resolve final URL for {url}")


def _fetch_update_feed(url: str, timeout: int = 30) -> dict[str, object] | None:
    """Return Beeper update-feed JSON when query_url points at the feed."""
    if "/desktop/update-feed" not in url:
        return None
    request = Request(url, headers={"User-Agent": UA}, method="GET")
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    if not raw.strip():
        raise RuntimeError(f"Beeper update feed returned no data for {url}")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("Beeper update feed did not return a JSON object")
    if not payload.get("version") or not payload.get("url"):
        raise RuntimeError(f"Beeper update feed missing version/url: {payload}")
    return payload


def _canonical_appimage_name(pkgver: str) -> str:
    return f"Beeper-{pkgver}-x86_64.AppImage"


def _extract_version_from_url(url: str, version_regex: str) -> str:
    match = re.search(version_regex, url)
    if not match:
        raise ValueError(f"Could not extract version from {url} using {version_regex}")
    if "version" in match.groupdict():
        return match.group("version")
    return match.group(0)


def _hash_streamed(url: str, timeout: int = 30) -> str:
    h = sha256()
    request = Request(url, headers={"User-Agent": UA}, method="GET")
    with urlopen(request, timeout=timeout) as response:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _extract_field(lines: list[str], field: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(field)}=", re.IGNORECASE)
    for line in lines:
        if pattern.match(line):
            # keep plain value without quote; keep first token in case inline array/comment
            return line.split("=", 1)[1].strip()
    return None


def _replace_scalar(lines: list[str], field: str, value: str) -> tuple[bool, str | None]:
    """Replace a scalar shell variable and return changed flag + old value."""
    regex = re.compile(rf"^(?P<indent>\s*){re.escape(field)}\s*=\s*(?P<value>.+?)(?P<trailing>(?:\s*#.*)? )?$")
    for idx, line in enumerate(lines):
        match = regex.match(line)
        if not match:
            continue
        old = match.group("value").strip()
        if old.startswith(("'", '"')) and old.endswith(old[0]):
            old = old[1:-1]
        new_line = f"{match.group('indent')}{field}={value}\n"
        if match.group("trailing"):
            new_line = f"{match.group('indent')}{field}={value}{match.group('trailing')}\n"
        if old == value:
            return False, old
        lines[idx] = new_line
        return True, old
    raise ValueError(f"Field {field} not found")


def _extract_array(lines: list[str], field: str) -> tuple[list[str], int, int]:
    open_re = re.compile(rf"^(?P<indent>\s*){re.escape(field)}\s*=\(")
    for idx, line in enumerate(lines):
        if open_re.match(line):
            block_lines = []
            end_idx = idx
            while end_idx < len(lines):
                block_lines.append(lines[end_idx])
                if ")" in lines[end_idx]:
                    break
                end_idx += 1
            else:
                raise ValueError(f"Array field {field} has no closing )")

            first_line = block_lines[0]
            before_equals, _, rest = first_line.partition("=")
            indent = before_equals[: len(before_equals) - len(before_equals.lstrip())]
            block_text = "".join(block_lines)
            before_comment, *comment_part = block_text.split("#", 1)
            comment = ""
            if comment_part:
                comment = "#" + comment_part[0].strip("\n")
            open_idx = before_comment.find("(")
            close_idx = before_comment.rfind(")")
            if open_idx == -1 or close_idx == -1 or close_idx < open_idx:
                raise ValueError(f"Malformed array field {field}")
            inner = before_comment[open_idx + 1 : close_idx]
            tokens = shlex.split(inner)
            return tokens, idx, end_idx
    raise ValueError(f"Array field {field} not found")


def _replace_array_first(lines: list[str], field: str, new_value: str) -> tuple[bool, tuple[str, ...]]:
    tokens, start, end = _extract_array(lines, field)
    old_tokens = tuple(tokens)
    if tokens:
        tokens[0] = new_value
    else:
        tokens = [new_value]
    indent = re.match(rf"^(?P<indent>\s*){re.escape(field)}", lines[start]).group("indent")
    original_block = "".join(lines[start : end + 1])
    quote = "'" if re.search(r"\(\s*'", original_block) else '"'
    escaped = new_value.replace("\\", "\\\\")
    if quote == "'":
        escaped = escaped.replace("'", "'\\''")
    else:
        escaped = escaped.replace('"', '\\"')
    new_entry = quote + escaped + quote
    new_block = f"{indent}{field}=({new_entry})"
    if end == start:
        suffix = "\n"
    else:
        suffix = "\n"
    lines[start : end + 1] = [new_block + suffix]
    return (tuple(tokens) != old_tokens), old_tokens


def update_pkgbuild(
    pkgbuild_path: Path,
    new_pkgver: str,
    new_source: str,
    new_sha256: str,
    dry_run: bool,
) -> UpdateResult:
    content = _read_text(pkgbuild_path)
    lines = content.splitlines(keepends=True)
    old_pkgver, old_source_text, old_sha = _extract_field(lines, "pkgver"), _extract_field(lines, "source"), _extract_field(lines, "sha256sums")

    changed_pkgver, old_pkgver_value = _replace_scalar(lines, "pkgver", new_pkgver)

    changed_source, old_source_tokens = _replace_array_first(lines, "source", new_source)
    old_source = old_source_tokens[0] if old_source_tokens else None

    changed_sha, old_sha_tokens = _replace_array_first(lines, "sha256sums", new_sha256)
    old_sha_value = old_sha_tokens[0] if old_sha_tokens else None

    changed = changed_pkgver or changed_source or changed_sha

    if changed and not dry_run:
        _write_text(pkgbuild_path, "".join(lines))

    return UpdateResult(
        package_name=pkgbuild_path.parent.name,
        package_dir=pkgbuild_path.parent,
        old_pkgver=old_pkgver_value or old_pkgver or "",
        new_pkgver=new_pkgver,
        old_source=old_source,
        new_source=new_source,
        old_sha256sums=(old_sha_value,) if old_sha_value else (),
        new_sha256=new_sha256,
        changed=changed,
        srcinfo_generated=False,
    )


def generate_srcinfo(
    package_dir: Path,
    srcinfo_command: str,
    dry_run: bool,
) -> bool:
    if dry_run:
        return False
    cmd = [srcinfo_command, "--printsrcinfo"]
    result = subprocess.run(
        cmd,
        cwd=package_dir,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{srcinfo_command} --printsrcinfo failed:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    srcinfo_path = package_dir / ".SRCINFO"
    srcinfo_path.write_text(result.stdout, encoding="utf-8")
    return True


def _detect_upstream(
    query_url: str,
    regex: str,
    timeout: int,
) -> tuple[str, str]:
    final_url = _fetch_final_url(query_url, timeout=timeout)
    version = _extract_version_from_url(final_url, regex)
    return version, final_url


def detect_upstream(
    query_url: str,
    regex: str,
    timeout: int,
) -> tuple[str, str, str]:
    """Return (pkgver, source_spec, sha256)."""
    update_feed = _fetch_update_feed(query_url, timeout)
    if update_feed:
        pkgver = str(update_feed["version"])
        source_url = str(update_feed["url"])
    else:
        pkgver, source_url = _detect_upstream(query_url, regex, timeout)

    sha256sum = _hash_streamed(source_url, timeout=timeout)
    filename = _canonical_appimage_name(pkgver)
    return pkgver, f"{filename}::{source_url}", sha256sum


def has_git_changes(path: Path) -> bool:
    root = path
    while root.parent != root and not (root / ".git").exists():
        root = root.parent
    if not (root / ".git").exists():
        return False
    result = subprocess.run(
        ["git", "status", "--porcelain", path.as_posix()],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _copy_package_files(src: Path, dst: Path) -> None:
    for item in src.iterdir():
        if item.name in {".git"}:
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


def push_to_aur(
    result: UpdateResult,
    aur_remote_template: str,
    push_ssh_key: str,
    ssh_known_hosts: str,
    commit_email: str,
    commit_name: str,
) -> PushResult:
    remote = aur_remote_template.format(package=result.package_name)
    if not push_ssh_key:
        return PushResult(pushed=False, remote=remote, reason="No AUR_SSH_KEY provided")
    if not ssh_known_hosts.strip():
        raise RuntimeError("AUR_SSH_KNOWN_HOSTS is required when AUR_SSH_KEY is set")
    with tempfile.TemporaryDirectory(prefix="aur-push-") as tempdir_str:
        tempdir = Path(tempdir_str)
        key_path = tempdir / "deploy_key"
        known_hosts_path = tempdir / "known_hosts"
        key_path.write_text(push_ssh_key.rstrip() + "\n", encoding="utf-8")
        known_hosts_path.write_text(ssh_known_hosts.rstrip() + "\n", encoding="utf-8")
        key_path.chmod(0o600)
        known_hosts_path.chmod(0o644)

        if has_git_changes(result.package_dir):
            pass

        # Prepare minimal env for ssh
        git_env = os.environ.copy()
        git_env["GIT_SSH_COMMAND"] = (
            f"ssh -i {shlex.quote(key_path.as_posix())} "
            f"-o IdentitiesOnly=yes "
            f"-o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={shlex.quote(known_hosts_path.as_posix())}"
        )

        subprocess.run(
            ["git", "clone", "--depth", "1", remote, tempdir / "aur"],
            check=True,
            env=git_env,
            cwd=tempdir,
        )
        aur_dir = tempdir / "aur"

        _copy_package_files(result.package_dir, aur_dir)

        subprocess.run(
            ["git", "-C", aur_dir.as_posix(), "config", "user.email", commit_email],
            check=True,
            env=git_env,
        )
        subprocess.run(
            ["git", "-C", aur_dir.as_posix(), "config", "user.name", commit_name],
            check=True,
            env=git_env,
        )

        subprocess.run(
            ["git", "-C", aur_dir.as_posix(), "add", "PKGBUILD", ".SRCINFO"],
            check=True,
            env=git_env,
        )

        status = subprocess.run(
            ["git", "-C", aur_dir.as_posix(), "status", "--porcelain"],
            check=True,
            env=git_env,
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            return PushResult(pushed=False, remote=remote, reason="No changes for remote package")

        commit_message = f"{result.package_name}: update to {result.new_pkgver}-1"
        subprocess.run(
            ["git", "-C", aur_dir.as_posix(), "commit", "-m", commit_message],
            check=True,
            env=git_env,
            input="" if result.changed else "",
            text=True,
        )
        subprocess.run(
            ["git", "-C", aur_dir.as_posix(), "push", "origin", "HEAD:master"],
            check=True,
            env=git_env,
        )
    return PushResult(pushed=True, remote=remote, reason=None)


def run(args: argparse.Namespace) -> tuple[UpdateResult, PushResult | None]:
    pkgbuild_path, srcinfo_path = resolve_paths(args)
    package_dir = pkgbuild_path.parent

    upstream_pkgver, source_url, checksum = detect_upstream(
        args.query_url,
        args.version_regex,
        args.timeout,
    )

    result = update_pkgbuild(pkgbuild_path, upstream_pkgver, source_url, checksum, args.dry_run)

    if result.changed and not args.dry_run:
        result.srcinfo_generated = generate_srcinfo(package_dir, args.srcinfo_command, args.dry_run)

        if args.push and not args.no_push:
            push_result = push_to_aur(
                result,
                args.aur_remote_template,
                args.push_ssh_key,
                args.ssh_known_hosts,
                args.commit_email,
                args.commit_name,
            )
        else:
            push_result = PushResult(pushed=False, reason="Push disabled")
    else:
        push_result = PushResult(pushed=False, reason="No local package changes")

    return result, push_result


def main() -> int:
    args = parse_args()
    try:
        result, push_result = run(args)
    except Exception as exc:  # pragma: no cover - keeps cli clear in user runs
        print(f"error: {exc}")
        return 1

    if args.json:
        payload = {
            "package_name": result.package_name,
            "package_dir": result.package_dir.as_posix(),
            "old_pkgver": result.old_pkgver,
            "new_pkgver": result.new_pkgver,
            "old_source": result.old_source,
            "new_source": result.new_source,
            "old_sha256sums": list(result.old_sha256sums),
            "new_sha256": result.new_sha256,
            "changed": result.changed,
            "srcinfo_generated": result.srcinfo_generated,
            "pushed": push_result.pushed if push_result else False,
            "remote": push_result.remote if push_result else None,
            "push_reason": push_result.reason if push_result else "",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if result.changed:
            print(f"Updated {result.package_name}: pkgver {result.old_pkgver} -> {result.new_pkgver}")
            print(f"source: {result.old_source} -> {result.new_source}")
            print(f"sha256: {result.old_sha256sums[0] if result.old_sha256sums else '-'} -> {result.new_sha256}")
            if result.srcinfo_generated:
                print("Regenerated .SRCINFO")
            if args.push:
                status = "pushed" if (push_result and push_result.pushed) else f"not pushed ({push_result.reason if push_result else 'unknown'})"
                print(status)
        else:
            print(f"No changes for {result.package_name}; already at {result.new_pkgver}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
