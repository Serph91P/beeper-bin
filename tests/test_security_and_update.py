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


class UpdateParsingTests(unittest.TestCase):
    def test_replace_array_preserves_single_quoted_pkgbuild_style(self):
        lines = ["source=('old')\n", "sha256sums=('0')\n"]

        changed, old = aur_update._replace_array_first(lines, "source", "new-value")

        self.assertTrue(changed)
        self.assertEqual(old, ("old",))
        self.assertEqual(lines[0], "source=('new-value')\n")

    def test_publish_no_changes_returns_success_exit_code(self):
        self.assertEqual(publish_aur.exit_code_for_result({"pushed": False, "reason": "No local changes for AUR package"}), 0)


if __name__ == "__main__":
    unittest.main()
