import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ro import policy


class PolicyLock(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.rule = root / "AGENTS.md"
        self.rule.write_text("GPT 一律 #high\n", encoding="utf-8")
        pol = root / "policy"
        for name, val in (("POLICY", pol), ("SNAP", pol / "snapshot"), ("LOCK", pol / "lock.json"),
                          ("LOG", pol / "events.log")):
            p = patch.object(policy, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(policy, "targets", lambda: {"workspace/AGENTS.md": self.rule})
        p.start()
        self.addCleanup(p.stop)
        policy.lock("测试授权")

    def test_drift_is_restored(self):
        os.chmod(self.rule, 0o600)
        self.rule.write_text("GPT 一律 #xhigh\n", encoding="utf-8")
        policy.enforce(log=lambda m: None)
        self.assertEqual(self.rule.read_text(encoding="utf-8"), "GPT 一律 #high\n")
        self.assertIn("restored", policy.LOG.read_text(encoding="utf-8"))

    def test_tampered_snapshot_refused(self):
        snap = policy.SNAP / "workspace/AGENTS.md"
        os.chmod(snap, 0o600)
        snap.write_text("改过\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            policy.enforce(log=lambda m: None)


if __name__ == "__main__":
    unittest.main()
