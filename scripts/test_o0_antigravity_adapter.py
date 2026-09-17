"""Unit tests for Antigravity Builder adapter (o0_antigravity_adapter.py)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.o0_antigravity_adapter import main, _find_agy_binary


REAL_SUBPROCESS_RUN = subprocess.run


class TestO0AntigravityAdapter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp_dir.name).resolve()
        self.workspace = self.temp_root / "repo"
        self.workspace.mkdir(parents=True, exist_ok=True)

        # Initialize a real git repo
        REAL_SUBPROCESS_RUN(["git", "init"], cwd=self.workspace, check=True, capture_output=True)
        REAL_SUBPROCESS_RUN(["git", "config", "user.name", "Test Builder"], cwd=self.workspace, check=True, capture_output=True)
        REAL_SUBPROCESS_RUN(["git", "config", "user.email", "builder@test.local"], cwd=self.workspace, check=True, capture_output=True)

        # Initial commit
        (self.workspace / "init.txt").write_text("initial", encoding="utf-8")
        REAL_SUBPROCESS_RUN(["git", "add", "init.txt"], cwd=self.workspace, check=True, capture_output=True)
        REAL_SUBPROCESS_RUN(["git", "commit", "-m", "chore: initial commit"], cwd=self.workspace, check=True, capture_output=True)

        self.report_path = self.temp_root / "reports" / "builder-report.json"
        self.state_path = self.temp_root / "orchestrator-state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_find_agy_binary_override_not_found(self):
        with self.assertRaises(FileNotFoundError):
            _find_agy_binary("non_existent_binary_path_xyz")

    @patch("scripts.o0_antigravity_adapter.subprocess.run")
    @patch("scripts.o0_antigravity_adapter._find_agy_binary")
    def test_rejects_result_sha_equals_base_sha_outside_fix_required(self, mock_find_bin, mock_subproc_run):
        """Adapter must reject result_sha == base_sha when not in FIX_REQUIRED."""
        mock_find_bin.return_value = Path("agy.exe")

        def subproc_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                return REAL_SUBPROCESS_RUN(*args, **kwargs)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"status": "SUCCESS"}), stderr=""
            )

        mock_subproc_run.side_effect = subproc_side_effect

        # In workspace without changes (HEAD is base_sha, status is clean)
        env = {
            "IDEAS_STANDARD_REPORT": str(self.report_path),
            "IDEAS_STANDARD_STATE": str(self.state_path),
        }
        self.state_path.write_text(json.dumps({"machine_state": "READY_FOR_BUILD"}), encoding="utf-8")

        with patch.dict(os.environ, env, clear=False), patch("sys.argv", ["o0_antigravity_adapter.py"]):
            old_cwd = Path.cwd()
            try:
                os.chdir(self.workspace)
                ret = main()
                self.assertEqual(1, ret)
                self.assertFalse(self.report_path.exists())
            finally:
                os.chdir(old_cwd)

    @patch("scripts.o0_antigravity_adapter.subprocess.run")
    @patch("scripts.o0_antigravity_adapter._find_agy_binary")
    def test_rejects_result_sha_equals_base_sha_in_fix_required(self, mock_find_bin, mock_subproc_run):
        """Adapter must reject result_sha == base_sha when in FIX_REQUIRED."""
        mock_find_bin.return_value = Path("agy.exe")

        def subproc_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                return REAL_SUBPROCESS_RUN(*args, **kwargs)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"status": "SUCCESS"}), stderr=""
            )

        mock_subproc_run.side_effect = subproc_side_effect

        env = {
            "IDEAS_STANDARD_REPORT": str(self.report_path),
            "IDEAS_STANDARD_STATE": str(self.state_path),
        }
        self.state_path.write_text(json.dumps({"machine_state": "FIX_REQUIRED"}), encoding="utf-8")

        with patch.dict(os.environ, env, clear=False), patch("sys.argv", ["o0_antigravity_adapter.py"]):
            old_cwd = Path.cwd()
            try:
                os.chdir(self.workspace)
                ret = main()
                self.assertEqual(1, ret)
                self.assertFalse(self.report_path.exists())
            finally:
                os.chdir(old_cwd)

    @patch("scripts.o0_antigravity_adapter.subprocess.run")
    @patch("scripts.o0_antigravity_adapter._find_agy_binary")
    def test_rejects_when_no_tests_configured_or_detected(self, mock_find_bin, mock_subproc_run):
        """Adapter rejects READY_FOR_AUDIT when no test command is configured and none detected."""
        mock_find_bin.return_value = Path("agy.exe")

        def subproc_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                return REAL_SUBPROCESS_RUN(*args, **kwargs)
            # agy execution: write uncommitted file
            (self.workspace / "feature.py").write_text("def run(): pass\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"status": "SUCCESS"}), stderr=""
            )

        mock_subproc_run.side_effect = subproc_side_effect

        env = {
            "IDEAS_STANDARD_REPORT": str(self.report_path),
        }

        with patch.dict(os.environ, env, clear=False), patch("sys.argv", ["o0_antigravity_adapter.py"]):
            old_cwd = Path.cwd()
            try:
                os.chdir(self.workspace)
                ret = main()
                self.assertEqual(1, ret)
                self.assertFalse(self.report_path.exists())
            finally:
                os.chdir(old_cwd)

    @patch("scripts.o0_antigravity_adapter.subprocess.run")
    @patch("scripts.o0_antigravity_adapter._find_agy_binary")
    def test_autodetects_and_executes_tests_when_available(self, mock_find_bin, mock_subproc_run):
        """Adapter auto-detects test command when test files exist in workspace."""
        mock_find_bin.return_value = Path("agy.exe")
        scripts_dir = self.workspace / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "validate_standard.py").write_text("# dummy\n", encoding="utf-8")

        def subproc_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                return REAL_SUBPROCESS_RUN(*args, **kwargs)
            if isinstance(cmd, list) and "agy.exe" in str(cmd[0]):
                (self.workspace / "feature.py").write_text("def run(): pass\n", encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=json.dumps({"status": "SUCCESS"}), stderr=""
                )
            # test command execution via shell string
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="test pass", stderr="")

        mock_subproc_run.side_effect = subproc_side_effect

        env = {
            "IDEAS_STANDARD_REPORT": str(self.report_path),
        }

        with patch.dict(os.environ, env, clear=False), patch("sys.argv", ["o0_antigravity_adapter.py"]):
            old_cwd = Path.cwd()
            try:
                os.chdir(self.workspace)
                ret = main()
                self.assertEqual(0, ret)
                self.assertTrue(self.report_path.exists())
                report = json.loads(self.report_path.read_text(encoding="utf-8"))
                self.assertEqual("READY_FOR_AUDIT", report["result"])
                unit_check = next(c for c in report["checks"] if c["id"] == "antigravity-unit-tests")
                self.assertEqual("PASS", unit_check["status"])
            finally:
                os.chdir(old_cwd)

    @patch("scripts.o0_antigravity_adapter.subprocess.run")
    @patch("scripts.o0_antigravity_adapter._find_agy_binary")
    def test_runs_test_cmd_and_reports_evidence(self, mock_find_bin, mock_subproc_run):
        """Adapter runs test-cmd and includes specific test evidence in the report."""
        mock_find_bin.return_value = Path("agy.exe")

        def subproc_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                return REAL_SUBPROCESS_RUN(*args, **kwargs)
            if isinstance(cmd, list) and "agy.exe" in str(cmd[0]):
                (self.workspace / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=json.dumps({"status": "SUCCESS"}), stderr=""
                )
            # test command execution via shell string
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="test pass", stderr="")

        mock_subproc_run.side_effect = subproc_side_effect

        env = {
            "IDEAS_STANDARD_REPORT": str(self.report_path),
            "IDEAS_STANDARD_TEST_CMD": f'"{sys.executable}" -c "print(\'tests passed\')"',
        }

        with patch.dict(os.environ, env, clear=False), patch("sys.argv", ["o0_antigravity_adapter.py"]):
            old_cwd = Path.cwd()
            try:
                os.chdir(self.workspace)
                ret = main()
                self.assertEqual(0, ret)
                self.assertTrue(self.report_path.exists())
                report = json.loads(self.report_path.read_text(encoding="utf-8"))
                self.assertEqual("READY_FOR_AUDIT", report["result"])
                commit_check = next(c for c in report["checks"] if c["id"] == "antigravity-commit")
                self.assertEqual("PASS", commit_check["status"])
                unit_check = next(c for c in report["checks"] if c["id"] == "antigravity-unit-tests")
                self.assertEqual("PASS", unit_check["status"])
                self.assertIn("passed successfully", unit_check["evidence"])
            finally:
                os.chdir(old_cwd)

    @patch("scripts.o0_antigravity_adapter.subprocess.run")
    @patch("scripts.o0_antigravity_adapter._find_agy_binary")
    def test_token_optimization_flags_and_scoping(self, mock_find_bin, mock_subproc_run):
        """Adapter passes --disable-slash-commands and scopes --add-dir to task scope."""
        mock_find_bin.return_value = Path("agy.exe")
        src_dir = self.workspace / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        target_file = src_dir / "app.py"
        target_file.write_text("print('hello')", encoding="utf-8")

        executed_commands = []

        def subproc_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            executed_commands.append(cmd)
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                return REAL_SUBPROCESS_RUN(*args, **kwargs)
            if isinstance(cmd, list) and "agy.exe" in str(cmd[0]):
                (self.workspace / "src" / "app.py").write_text("print('updated')", encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=json.dumps({"status": "SUCCESS"}), stderr=""
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="test pass", stderr="")

        mock_subproc_run.side_effect = subproc_side_effect

        env = {
            "IDEAS_STANDARD_REPORT": str(self.report_path),
            "IDEAS_STANDARD_TEST_CMD": f'"{sys.executable}" -c "print(\'tests passed\')"',
            "IDEAS_STANDARD_TASK_SCOPE": json.dumps(["src/app.py"]),
        }

        with patch.dict(os.environ, env, clear=False), patch("sys.argv", ["o0_antigravity_adapter.py"]):
            old_cwd = Path.cwd()
            try:
                os.chdir(self.workspace)
                ret = main()
                self.assertEqual(0, ret)
                # Inspect agy call
                agy_call = next(c for c in executed_commands if isinstance(c, list) and "agy.exe" in str(c[0]))
                self.assertIn("--disable-slash-commands", agy_call)
                # Verify targeted scope directory
                expected_dir_flag = f"--add-dir={src_dir.resolve()}"
                self.assertIn(expected_dir_flag, agy_call)
                # Ensure the root workspace was not added when specific scope exists
                self.assertNotIn(f"--add-dir={self.workspace.resolve()}", agy_call)
            finally:
                os.chdir(old_cwd)

    @patch("scripts.o0_antigravity_adapter.subprocess.run")
    @patch("scripts.o0_antigravity_adapter._find_agy_binary")
    def test_local_prevalidation_catches_syntax_error(self, mock_find_bin, mock_subproc_run):
        """Adapter fails locally with exit code 1 if a modified Python file has a syntax error."""
        mock_find_bin.return_value = Path("agy.exe")

        def subproc_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                return REAL_SUBPROCESS_RUN(*args, **kwargs)
            if isinstance(cmd, list) and "agy.exe" in str(cmd[0]):
                # Introduce a syntax error in a python file
                (self.workspace / "broken.py").write_text("def unclosed_syntax(\n", encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=json.dumps({"status": "SUCCESS"}), stderr=""
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="test pass", stderr="")

        mock_subproc_run.side_effect = subproc_side_effect

        env = {
            "IDEAS_STANDARD_REPORT": str(self.report_path),
            "IDEAS_STANDARD_TEST_CMD": f'"{sys.executable}" -c "print(\'tests passed\')"',
        }

        with patch.dict(os.environ, env, clear=False), patch("sys.argv", ["o0_antigravity_adapter.py"]):
            old_cwd = Path.cwd()
            try:
                os.chdir(self.workspace)
                ret = main()
                self.assertEqual(1, ret)
                self.assertFalse(self.report_path.exists())
            finally:
                os.chdir(old_cwd)

    @patch("scripts.o0_antigravity_adapter.subprocess.run")
    @patch("scripts.o0_antigravity_adapter._find_agy_binary")
    def test_dynamic_model_tier_selection(self, mock_find_bin, mock_subproc_run):
        """Adapter selects fast/flash tier or deep/pro tier based on environment or state."""
        mock_find_bin.return_value = Path("agy.exe")
        executed_commands = []

        def subproc_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            executed_commands.append(cmd)
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                return REAL_SUBPROCESS_RUN(*args, **kwargs)
            if isinstance(cmd, list) and "agy.exe" in str(cmd[0]):
                (self.workspace / "valid.py").write_text("x = 1\n", encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=json.dumps({"status": "SUCCESS"}), stderr=""
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="test pass", stderr="")

        mock_subproc_run.side_effect = subproc_side_effect

        # Test 1: IDEAS_STANDARD_MODEL_TIER=fast selects gemini-2.5-flash
        env = {
            "IDEAS_STANDARD_REPORT": str(self.report_path),
            "IDEAS_STANDARD_TEST_CMD": f'"{sys.executable}" -c "print(\'tests passed\')"',
            "IDEAS_STANDARD_MODEL_TIER": "fast",
        }

        with patch.dict(os.environ, env, clear=False), patch("sys.argv", ["o0_antigravity_adapter.py"]):
            old_cwd = Path.cwd()
            try:
                os.chdir(self.workspace)
                ret = main()
                self.assertEqual(0, ret)
                agy_call = next(c for c in executed_commands if isinstance(c, list) and "agy.exe" in str(c[0]))
                self.assertIn("--model=gemini-2.5-flash", agy_call)
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()


