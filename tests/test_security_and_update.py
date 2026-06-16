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


class UpdateParsingTests(unittest.TestCase):
    def test_replace_array_preserves_single_quoted_pkgbuild_style(self):
        lines = ["source=('old')\n", "sha256sums=('0')\n"]

        changed, old = aur_update._replace_array_first(lines, "source", "new-value")

        self.assertTrue(changed)
        self.assertEqual(old, ("old",))
        self.assertEqual(lines[0], "source=('new-value')\n")

    def test_detect_upstream_from_update_feed_json(self):
        feed_url = "https://api.beeper.com/desktop/update-feed.json?channel=nightly"
        payload = {
            "version": "4.2.908",
            "url": "https://beeper-desktop.download.beeper.com/builds/Beeper-Nightly-4.2.908-x86_64.AppImage",
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
        self.assertEqual(source, "Beeper-4.2.908-x86_64.AppImage::https://beeper-desktop.download.beeper.com/builds/Beeper-Nightly-4.2.908-x86_64.AppImage")
        self.assertEqual(sha256, "b2aa")

    def test_publish_no_changes_returns_success_exit_code(self):
        self.assertEqual(publish_aur.exit_code_for_result({"pushed": False, "reason": "No local changes for AUR package"}), 0)


if __name__ == "__main__":
    unittest.main()
