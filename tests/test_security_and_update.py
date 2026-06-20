import argparse
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import publish_aur
import aur_update


class PublishSecurityTests(unittest.TestCase):
    def test_push_requires_known_hosts_when_key_is_present(self):
        args = argparse.Namespace(
            package_name="beeper-bin",
            package_dir=str(REPO / "packages" / "beeper-bin"),
            aur_remote_template="ssh://aur@aur.archlinux.org/{package}.git",
            push_ssh_key="dummy-key",
            ssh_known_hosts="",
            commit_email="actions@github.com",
            commit_name="AUR Update Bot",
            package_ver=None,
        )

        with self.assertRaisesRegex(RuntimeError, "AUR_SSH_KNOWN_HOSTS"):
            publish_aur.push_package(args)

    def test_git_ssh_command_uses_strict_host_key_checking_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = publish_aur.build_ssh_env(
                base_env={},
                key_path=Path(tmp) / "id_ed25519",
                known_hosts_path=Path(tmp) / "known_hosts",
            )

        command = env["GIT_SSH_COMMAND"]
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("UserKnownHostsFile=", command)
        self.assertNotIn("StrictHostKeyChecking=no", command)


class PkgbuildDesktopRegistrationTests(unittest.TestCase):
    def test_beeper_bin_extracts_app_asar_before_patching_linux_config(self):
        pkgbuild = (REPO / "packages" / "beeper-bin" / "PKGBUILD").read_text(encoding="utf-8")

        self.assertIn("app.asar", pkgbuild)
        self.assertIn("asar extract", pkgbuild)
        self.assertIn('asar extract "$_asar_path" "$_app_dir" || return 1', pkgbuild)
        self.assertLess(pkgbuild.index("asar extract"), pkgbuild.index("registerLinuxConfig"))

    def test_beeper_bin_fails_build_if_linux_config_patch_target_is_missing(self):
        pkgbuild = (REPO / "packages" / "beeper-bin" / "PKGBUILD").read_text(encoding="utf-8")

        self.assertNotIn("skipping patch", pkgbuild)
        self.assertRegex(pkgbuild, r"(?s)could not find file exporting registerLinuxConfig.*return 1")

    def test_beeper_bin_preserves_nullglob_state_under_errexit(self):
        pkgbuild = (REPO / "packages" / "beeper-bin" / "PKGBUILD").read_text(encoding="utf-8")

        self.assertIn("_oldnull=$(shopt -p nullglob || true)", pkgbuild)
        self.assertNotIn("_oldnull=$(shopt -p nullglob)\n", pkgbuild)


class AppImageLayoutInspectionTests(unittest.TestCase):
    def write_pkgbuild(self, root: Path, makedepends: str = "makedepends=('asar')") -> Path:
        pkgbuild = root / "PKGBUILD"
        pkgbuild.write_text(
            "pkgname='beeper-bin'\n"
            "pkgver=1\n"
            "source=('Beeper-1-x86_64.AppImage::https://example.invalid/Beeper.AppImage')\n"
            "sha256sums=('0')\n"
            f"{makedepends}\n",
            encoding="utf-8",
        )
        return pkgbuild

    def write_registration_file(self, root: Path, relative: str = "resources/app/build/main/linux-main.mjs") -> Path:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "const real=function(){};\n"
            "export{real as registerLinuxConfig};\n"
            "desktop-file-install --dir ~/.local/share/applications\n",
            encoding="utf-8",
        )
        return target

    def test_inspects_unpacked_resources_app_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "squashfs-root"
            self.write_registration_file(root)
            pkgbuild = self.write_pkgbuild(Path(tmp))

            inspection = aur_update.validate_appimage_layout(root, pkgbuild)

        self.assertEqual(inspection.layout, "resources/app")
        self.assertFalse(inspection.requires_asar_makedepend)
        self.assertEqual(inspection.register_linux_config_targets, ("resources/app/build/main/linux-main.mjs",))
        self.assertEqual(inspection.desktop_registration_targets, ("resources/app/build/main/linux-main.mjs",))

    def test_inspects_resources_app_asar_layout_via_asar_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "squashfs-root"
            resources = root / "resources"
            resources.mkdir(parents=True)
            app_asar = resources / "app.asar"
            app_asar.write_text("fake asar", encoding="utf-8")
            extracted = tmp_path / "fake-extracted-asar"
            self.write_registration_file(extracted, "build/main/linux-asar.mjs")
            pkgbuild = self.write_pkgbuild(tmp_path)

            old_extract = aur_update._extract_asar_to_dir

            def fake_extract(asar_path: Path, destination: Path) -> None:
                self.assertEqual(asar_path, app_asar)
                import shutil

                shutil.copytree(extracted, destination)

            try:
                aur_update._extract_asar_to_dir = fake_extract
                inspection = aur_update.validate_appimage_layout(root, pkgbuild)
            finally:
                aur_update._extract_asar_to_dir = old_extract

        self.assertEqual(inspection.layout, "resources/app.asar")
        self.assertTrue(inspection.requires_asar_makedepend)
        self.assertEqual(inspection.register_linux_config_targets, ("resources/app.asar:build/main/linux-asar.mjs",))

    def test_fails_when_register_linux_config_target_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "squashfs-root"
            app = root / "resources" / "app" / "build" / "main"
            app.mkdir(parents=True)
            (app / "linux-main.mjs").write_text("console.log('no registration');\n", encoding="utf-8")
            pkgbuild = self.write_pkgbuild(Path(tmp))

            with self.assertRaisesRegex(RuntimeError, "registerLinuxConfig.*not found"):
                aur_update.validate_appimage_layout(root, pkgbuild)

    def test_fails_when_multiple_register_linux_config_targets_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "squashfs-root"
            self.write_registration_file(root, "resources/app/build/main/a.mjs")
            self.write_registration_file(root, "resources/app/build/main/b.mjs")
            pkgbuild = self.write_pkgbuild(Path(tmp))

            with self.assertRaisesRegex(RuntimeError, "multiple registerLinuxConfig"):
                aur_update.validate_appimage_layout(root, pkgbuild)

    def test_app_asar_layout_requires_asar_makedepend_in_pkgbuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "squashfs-root"
            resources = root / "resources"
            resources.mkdir(parents=True)
            app_asar = resources / "app.asar"
            app_asar.write_text("fake asar", encoding="utf-8")
            extracted = tmp_path / "fake-extracted-asar"
            self.write_registration_file(extracted, "build/main/linux-asar.mjs")
            pkgbuild = self.write_pkgbuild(tmp_path, makedepends="makedepends=('desktop-file-utils')")

            old_extract = aur_update._extract_asar_to_dir

            def fake_extract(asar_path: Path, destination: Path) -> None:
                import shutil

                shutil.copytree(extracted, destination)

            try:
                aur_update._extract_asar_to_dir = fake_extract
                with self.assertRaisesRegex(RuntimeError, "makedepends.*asar"):
                    aur_update.validate_appimage_layout(root, pkgbuild)
            finally:
                aur_update._extract_asar_to_dir = old_extract

    def test_run_inspects_changed_upstream_before_writing_pkgbuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pkgbuild = self.write_pkgbuild(tmp_path)
            srcinfo = tmp_path / ".SRCINFO"
            inspected = []

            old_detect = aur_update.detect_upstream
            old_inspect = aur_update.inspect_upstream_appimage_source
            old_generate = aur_update.generate_srcinfo
            try:
                aur_update.detect_upstream = lambda query_url, regex, timeout: (
                    "2",
                    "Beeper-2-x86_64.AppImage::https://example.invalid/Beeper-2.AppImage",
                    "1",
                )

                def fake_inspect(source_spec: str, pkgbuild_path: Path, timeout: int):
                    inspected.append((source_spec, pkgbuild_path, timeout))
                    current = pkgbuild_path.read_text(encoding="utf-8")
                    self.assertIn("pkgver=1", current)
                    return None

                aur_update.inspect_upstream_appimage_source = fake_inspect
                aur_update.generate_srcinfo = lambda package_dir, srcinfo_command, dry_run: srcinfo.write_text("ok", encoding="utf-8") or True
                args = argparse.Namespace(
                    package_name="beeper-bin",
                    package_dir=tmp_path.as_posix(),
                    query_url="https://example.invalid/feed.json",
                    version_regex=r"Beeper-(?P<version>[0-9.]+)",
                    srcinfo_command="makepkg",
                    dry_run=False,
                    json=False,
                    push=False,
                    no_push=False,
                    push_ssh_key="",
                    ssh_known_hosts="",
                    aur_remote_template="ssh://aur@aur.archlinux.org/{package}.git",
                    timeout=7,
                    commit_email="actions@github.com",
                    commit_name="AUR Update Bot",
                    skip_appimage_inspection=False,
                )

                result, _ = aur_update.run(args)
            finally:
                aur_update.detect_upstream = old_detect
                aur_update.inspect_upstream_appimage_source = old_inspect
                aur_update.generate_srcinfo = old_generate

        self.assertTrue(result.changed)
        self.assertEqual(len(inspected), 1)
        self.assertEqual(inspected[0][0], "Beeper-2-x86_64.AppImage::https://example.invalid/Beeper-2.AppImage")


class UpdateParsingTests(unittest.TestCase):
    def test_replace_array_preserves_single_quoted_pkgbuild_style(self):
        lines = ["source=('old')\n", "sha256sums=('0')\n"]

        changed, old = aur_update._replace_array_first(lines, "source", "new-value")

        self.assertTrue(changed)
        self.assertEqual(old, ("old",))
        self.assertEqual(lines[0], "source=('new-value')\n")

    def test_detect_upstream_from_update_feed_json(self):
        feed_url = "https://api.beeper.com/desktop/update-feed.json?channel=stable"
        payload = {
            "version": "4.2.908",
            "url": "https://beeper-desktop.download.beeper.com/builds/Beeper-4.2.908-x86_64.AppImage",
        }

        def fake_feed(url, timeout):
            self.assertEqual(url, feed_url)
            return payload

        def fake_hash(url, timeout):
            self.assertEqual(url, payload["url"])
            return "b2aa"

        old_feed = aur_update._fetch_update_feed
        old_hash = aur_update._hash_streamed
        try:
            aur_update._fetch_update_feed = fake_feed
            aur_update._hash_streamed = fake_hash
            pkgver, source, sha256 = aur_update.detect_upstream(feed_url, r"Beeper-(?P<version>[0-9.]+)", 30)
        finally:
            aur_update._fetch_update_feed = old_feed
            aur_update._hash_streamed = old_hash

        self.assertEqual(pkgver, "4.2.908")
        self.assertEqual(source, "Beeper-4.2.908-x86_64.AppImage::https://beeper-desktop.download.beeper.com/builds/Beeper-4.2.908-x86_64.AppImage")
        self.assertEqual(sha256, "b2aa")

    def test_publish_no_changes_returns_success_exit_code(self):
        self.assertEqual(publish_aur.exit_code_for_result({"pushed": False, "reason": "No local changes for AUR package"}), 0)


if __name__ == "__main__":
    unittest.main()
