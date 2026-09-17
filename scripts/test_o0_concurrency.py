import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.orchestrate_handoffs import init_state


class O0ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = Path(__file__).resolve().parents[1]
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._run(["git", "init", "-b", "builder/o0-c39"], self.repository)
        self._run(["git", "config", "user.name", "O0 C39"], self.repository)
        self._run(["git", "config", "user.email", "o0-c39@example.invalid"], self.repository)
        (self.repository / "artifact.txt").write_text("candidate\n", encoding="utf-8")
        self._run(["git", "add", "artifact.txt"], self.repository)
        self._run(["git", "commit", "-m", "controlled candidate"], self.repository)
        self.sha = self._run(["git", "rev-parse", "HEAD"], self.repository).stdout.strip()
        self.state = self.root / "state.json"
        init_state(
            self.source / "orchestration" / "builder-auditor-policy.json",
            self.state,
            project_id="o0-c39-concurrency",
            phase="O0",
            gate="S1",
            builder_branch="builder/o0-c39",
        )
        self.actor = self.root / "actor.py"
        self.actor.write_text(
            """import json, os, sys, time
from pathlib import Path
marker, entered, release = map(Path, sys.argv[1:])
with marker.open('a', encoding='utf-8') as stream:
    stream.write(f'{os.getpid()}\\n')
try:
    entered.open('x').close()
    while not release.exists():
        time.sleep(0.02)
except FileExistsError:
    pass
sha = __import__('subprocess').run(['git','rev-parse','HEAD'], capture_output=True, text=True, check=True).stdout.strip()
report = {'schema_version':'0.1','executor_id':'real-builder','role':'BUILDER','authority':'IMPLEMENTATION','result_sha':sha,'result':'READY_FOR_AUDIT','summary':'real actor completed','changed_paths':['artifact.txt'],'checks':[{'id':'real-process','status':'PASS','evidence':'one real actor execution'}],'limitations':[],'disputed_findings':[],'escalation':None}
Path(os.environ['IDEAS_STANDARD_REPORT']).write_text(json.dumps(report), encoding='utf-8')
""",
            encoding="utf-8",
        )
        self.marker = self.root / "actor-runs.txt"
        self.entered = self.root / "actor-entered"
        self.release = self.root / "actor-release"
        self.config = self.root / "runner.json"
        self.config.write_text(json.dumps({
            "repository": str(self.repository),
            "state_path": str(self.state),
            "reports_dir": str(self.root / "reports"),
            "builder_workspace": str(self.repository),
            "audit_workspaces": str(self.root / "audits"),
            "builder_command": [sys.executable, str(self.actor), str(self.marker), str(self.entered), str(self.release)],
            "auditor_command": ["must-not-run"],
            "lock_timeout_seconds": 0,
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _run(command, cwd):
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=True)

    def _runner_command(self):
        return [sys.executable, "-m", "scripts.o0_runner", "--config", str(self.config)]

    def _wait_for(self, path: Path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), f"timed out waiting for {path}")

    def test_concurrent_runner_rejects_before_second_actor_or_mutation(self):
        first = subprocess.Popen(
            self._runner_command(), cwd=self.source, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            self._wait_for(self.entered)
            second = subprocess.run(
                self._runner_command(), cwd=self.source, capture_output=True, text=True, timeout=5, check=False
            )
            self.release.touch()
            first_stdout, first_stderr = first.communicate(timeout=5)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()

        self.assertEqual(2, second.returncode)
        self.assertIn("Runner state lock is busy", second.stderr)
        self.assertEqual(0, first.returncode, first_stderr)
        self.assertEqual(1, len(self.marker.read_text(encoding="utf-8").splitlines()))
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual("READY_FOR_AUDIT", persisted["machine_state"])
        self.assertEqual(0, persisted["audit_round"])
        self.assertEqual(self.sha, persisted["audit_target_sha"])
        self.assertEqual(1, len(list((self.root / "reports").glob("*.json"))))
        self.assertEqual(1, len(list((self.root / "reports" / "evidence").glob("*.json"))))

    def test_process_exit_releases_lock_even_when_lock_file_remains(self):
        holder = self.root / "holder.py"
        ready = self.root / "lock-held"
        holder.write_text(
            """import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from scripts.o0_runner import StateLock
with StateLock(Path(sys.argv[1]), 0):
    Path(sys.argv[2]).touch()
    time.sleep(30)
""",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(holder), str(self.state), str(ready), str(self.source)], cwd=self.source
        )
        self._wait_for(ready)
        process.kill()
        process.wait(timeout=5)
        self.release.touch()

        resumed = subprocess.run(
            self._runner_command(), cwd=self.source, capture_output=True, text=True, timeout=5, check=False
        )

        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertTrue(self.state.with_name("state.json.lock").is_file())
        self.assertEqual("READY_FOR_AUDIT", json.loads(self.state.read_text(encoding="utf-8"))["machine_state"])

    def test_actor_failure_preserves_valid_state_and_releases_lock(self):
        failing = self.root / "failing-actor.py"
        failing.write_text("raise SystemExit(7)\n", encoding="utf-8")
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["builder_command"] = [sys.executable, str(failing)]
        self.config.write_text(json.dumps(config), encoding="utf-8")
        before = self.state.read_bytes()

        failed = subprocess.run(
            self._runner_command(), cwd=self.source, capture_output=True, text=True, timeout=5, check=False
        )

        self.assertEqual(2, failed.returncode)
        self.assertIn("Actor command failed with exit code 7", failed.stderr)
        self.assertEqual(before, self.state.read_bytes())
        persisted = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual("READY_FOR_BUILD", persisted["machine_state"])

        config["builder_command"] = [
            sys.executable, str(self.actor), str(self.marker), str(self.entered), str(self.release)
        ]
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.release.touch()
        recovered = subprocess.run(
            self._runner_command(), cwd=self.source, capture_output=True, text=True, timeout=5, check=False
        )
        self.assertEqual(0, recovered.returncode, recovered.stderr)


if __name__ == "__main__":
    unittest.main()
