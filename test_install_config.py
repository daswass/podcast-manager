import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class InstallConfigTest(unittest.TestCase):
    def test_secret_files_are_ignored_and_examples_are_generic(self):
        gitignore = (ROOT / ".gitignore").read_text()
        self.assertIn(".env\n", gitignore)
        self.assertIn("local.env\n", gitignore)
        env_example = (ROOT / ".env.example").read_text()
        local_example = (ROOT / "local.env.example").read_text()
        self.assertIn("API_KEY=\n", env_example)
        self.assertIn("API_SECRET=\n", env_example)
        self.assertIn("LAUNCH_LABEL_PREFIX=com.podcast-manager\n", local_example)
        self.assertNotIn("/Users/", env_example + local_example)

    def test_committed_scripts_and_templates_have_no_personal_paths(self):
        for folder in ("scripts", "launchd"):
            for path in (ROOT / folder).rglob("*"):
                if not path.is_file():
                    continue
                text = path.read_text()
                self.assertNotIn("/Users/", text, str(path))
                self.assertNotIn("ericwasserman", text, str(path))

    def test_render_plist_fills_the_callers_repo_and_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "agent.plist"
            subprocess.run(
                [
                    "bash",
                    "-c",
                    'source scripts/install-lib.sh && render_plist "$1" "$2" "$3" "$4" "$5"',
                    "bash",
                    "launchd/nightly-prepare.plist.template",
                    str(dest),
                    "/tmp/podcast-checkout",
                    "com.example.podcast-manager.nightly-prepare",
                    "/custom/bin:/usr/bin",
                ],
                cwd=ROOT,
                check=True,
            )
            text = dest.read_text()
        self.assertIn("/tmp/podcast-checkout/scripts/run-nightly-prepare.sh", text)
        self.assertIn("/tmp/podcast-checkout", text)
        self.assertIn("com.example.podcast-manager.nightly-prepare", text)
        self.assertIn("/custom/bin:/usr/bin", text)
        self.assertNotIn("__", text)

    def test_label_prefix_reads_the_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            path.write_text("# comment\nLAUNCH_LABEL_PREFIX=com.example.mine\n")
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source scripts/install-lib.sh && read_label_prefix "$1" && printf "%s" "$LAUNCH_LABEL_PREFIX"',
                    "bash",
                    str(path),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.stdout, "com.example.mine")


if __name__ == "__main__":
    unittest.main()
