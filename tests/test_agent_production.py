"""Deployment acceptance for an offline Agent state upgrade."""

import contextlib
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import coding_agent
import workbench
from accounts import Store


class AgentProductionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / "source"
        self.source.mkdir()
        self.clone = self.source.with_name("candidate")
        Store(self.source / "workspace.sqlite3").create_invite()
        files = self.source / "staged-repositories"
        files.mkdir()
        (files / "fixture").write_bytes(b"stored artifact")

    def invoke(self, runs, *args):
        argv = ["workbench.py", "--runs-dir", str(runs), *args]
        with patch("sys.argv", argv), patch.dict("os.environ", {}, clear=True):
            return workbench.main()

    def prepare(self):
        return self.invoke(self.clone, "--prepare-coding-agent-production-from", str(self.source))

    def test_prepares_full_clone_and_starts_with_production_limits(self):
        before = (self.source / "workspace.sqlite3").read_bytes()
        self.assertEqual(self.prepare(), 0)
        self.assertEqual((self.clone / "workspace.sqlite3").read_bytes(), before)
        self.assertEqual((self.clone / "staged-repositories/fixture").read_bytes(), b"stored artifact")
        self.assertEqual((self.source / "workspace.sqlite3").read_bytes(), before)
        with patch.object(workbench, "serve") as serve, patch.object(coding_agent, "dependencies"):
            self.assertEqual(self.invoke(self.clone, "--coding-agent-production"), 0)
        self.assertEqual(serve.call_args.args[2], self.clone)
        settings = serve.call_args.kwargs
        self.assertTrue(settings["coding_agent_enabled"])
        self.assertEqual(settings["generation_limits"], (5, 500))
        self.assertEqual(settings["coding_agent_max_attempts"], 16)
        self.assertEqual(settings["coding_agent_timeout"], 120)

    def assert_rejected(self, message, runs, *args):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch.object(workbench, "serve") as serve:
            with self.assertRaises(SystemExit) as result:
                self.invoke(runs, *args)
        self.assertEqual(result.exception.code, 2)
        self.assertIn(message, stderr.getvalue())
        serve.assert_not_called()

    def test_original_and_copied_marker_are_rejected(self):
        self.assert_rejected("未准备", self.source, "--coding-agent-production")
        self.prepare()
        shutil.copytree(self.clone, self.source.with_name("impostor"))
        self.assert_rejected("副本身份", self.source.with_name("impostor"), "--coding-agent-production")

    def test_existing_destination_and_unsafe_source_are_rejected(self):
        self.assert_rejected("目标目录已存在", self.source,
                             "--prepare-coding-agent-production-from", str(self.source))
        (self.source / "workspace.sqlite3-wal").touch()
        self.assert_rejected("SQLite 日志", self.clone,
                             "--prepare-coding-agent-production-from", str(self.source))
        (self.source / "workspace.sqlite3-wal").unlink()
        (self.source / "link").symlink_to(self.source / "workspace.sqlite3")
        self.assert_rejected("普通文件", self.clone,
                             "--prepare-coding-agent-production-from", str(self.source))
        self.assertFalse(self.clone.exists())

    def test_rejects_external_git_store_and_incomplete_copy(self):
        repo = self.source / "staged-repositories/tool.git"
        (repo / "objects/info").mkdir(parents=True)
        (repo / "objects/info/alternates").write_text("/old/git/objects\n")
        self.assert_rejected("Git", self.clone, "--prepare-coding-agent-production-from", str(self.source))
        self.assertFalse(self.clone.exists())
        shutil.rmtree(repo)
        copy = shutil.copytree

        def change_source(source, destination, *args, **kwargs):
            result = copy(source, destination, *args, **kwargs)
            if Path(source).resolve() == self.source.resolve():
                (self.source / "changed").touch()
            return result

        with patch("agent_production.shutil.copytree", side_effect=change_source):
            self.assert_rejected("发生变化", self.clone,
                                 "--prepare-coding-agent-production-from", str(self.source))
        self.assertFalse(self.clone.exists())

    def test_rejects_trial_state(self):
        (self.source / ".root-coding-agent-trial").touch()
        self.assert_rejected("未准备", self.source, "--coding-agent-production")
        self.assert_rejected("尚未启用 Agent", self.clone,
                             "--prepare-coding-agent-production-from", str(self.source))

    def test_missing_agent_dependencies_stop_production_startup(self):
        self.prepare()
        self.assert_rejected("Agent 依赖", self.clone, "--coding-agent-production")
