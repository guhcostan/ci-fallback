import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import fallbackd


class SlotHomeTest(unittest.TestCase):
    def setUp(self):
        folder = self.enter_temp_dir()
        self.home = folder / "personal"
        self.home.mkdir()
        (self.home / ".ssh").mkdir()
        (self.home / "setup-pnpm").mkdir()
        self.slots_dir = folder / "slots"
        self.add_patch(patch.multiple(fallbackd, HOME=str(folder), SLOTS_DIR=str(self.slots_dir)))
        self.add_patch(patch.dict(os.environ, {"HOME": str(self.home), "PATH": "/tools", "GH_TOKEN": "fixture"}, clear=True))
        self.proc = Mock()
        self.proc.poll.return_value = None
        self.spawn = self.add_patch(patch.object(fallbackd.subprocess, "Popen", return_value=self.proc))
        self.add_patch(patch.object(fallbackd, "log"))
        self.gh = Mock()
        self.gh.request.return_value = {"encoded_jit_config": "fixture", "runner": {"id": 1}}

    def enter_temp_dir(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def add_patch(self, context):
        self.addCleanup(context.stop)
        return context.start()

    def make_slot(self, index):
        slot = fallbackd.Slot(index)
        Path(slot.dir).mkdir(parents=True)
        (Path(slot.dir) / "run.sh").touch()
        return slot

    def start(self, slot):
        slot.start(self.gh, "owner/repo", slot.index)
        self.addCleanup(slot.log.close)
        return self.spawn.call_args.kwargs["env"]

    def test_concurrent_slots_use_private_homes(self):
        for index in (1, 2):
            slot = self.make_slot(index)
            env = self.start(slot)
            home = Path(env["HOME"])
            self.assertEqual(home.parent, Path(slot.dir))
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(home.iterdir()), [])
            self.assertNotIn("GH_TOKEN", env)
            self.assertEqual(env["PATH"], "/tools")
            self.assertEqual(self.spawn.call_args.kwargs["cwd"], slot.dir)
            (home / "setup-pnpm").mkdir()
            (home / "setup-pnpm/version").write_text(str(index))

        self.assertEqual((self.slots_dir / "1/_home/setup-pnpm/version").read_text(), "1")
        self.assertEqual(os.environ["HOME"], str(self.home))
        self.assertEqual(list((self.home / "setup-pnpm").iterdir()), [])

    def test_reused_slot_gets_a_clean_home(self):
        slot = self.make_slot(1)
        first_home = Path(self.start(slot)["HOME"])
        (first_home / "leftover").touch()
        (first_home / "external").symlink_to(self.home, target_is_directory=True)
        self.proc.poll.return_value = 0
        slot.reap()
        self.proc.poll.return_value = None

        self.assertEqual(Path(self.start(slot)["HOME"]), first_home)
        self.assertEqual(list(first_home.iterdir()), [])
        self.assertTrue((self.home / ".ssh").is_dir())

    def test_read_only_tree_is_removed(self):
        slot = self.make_slot(1)
        home = Path(self.start(slot)["HOME"])
        locked = home / "go/pkg/mod/example@v1"
        locked.mkdir(parents=True)
        (locked / "file").write_text("x")
        (locked / "file").chmod(0o444)
        locked.chmod(0o555)
        self.proc.poll.return_value = 0
        slot.reap()

        self.assertFalse(home.exists())
        self.assertTrue((self.home / ".ssh").is_dir())

    def test_home_symlink_prevents_registration(self):
        slot = self.make_slot(1)
        (Path(slot.dir) / "_home").symlink_to(self.home, target_is_directory=True)

        with self.assertRaises(OSError):
            slot.start(self.gh, "owner/repo", 1)

        self.gh.request.assert_not_called()
        self.spawn.assert_not_called()
        self.assertTrue((self.home / ".ssh").is_dir())


class CorepackShimTest(unittest.TestCase):
    def test_removes_only_corepack_shims(self):
        with tempfile.TemporaryDirectory() as d:
            bindir = Path(d, "node/22.23.5/arm64/bin")
            bindir.mkdir(parents=True)
            for name in ("node", "npm"):
                (bindir / name).touch()
            (bindir / "corepack").symlink_to("../lib/node_modules/corepack/dist/corepack.js")
            for name in ("pnpm", "pnpx", "yarn", "yarnpkg"):
                (bindir / name).symlink_to("../lib/node_modules/corepack/dist/%s.js" % name)
            other = Path(d, "node/20.0.0/arm64/bin")
            other.mkdir(parents=True)
            (other / "pnpm").symlink_to("../lib/node_modules/pnpm/bin/pnpm.cjs")

            removed = fallbackd.remove_corepack_shims(d)

            self.assertEqual(len(removed), 4)
            self.assertEqual(sorted(p.name for p in bindir.iterdir()), ["corepack", "node", "npm"])
            self.assertTrue((other / "pnpm").is_symlink())

    def test_missing_tool_dir(self):
        self.assertEqual(fallbackd.remove_corepack_shims("/nonexistent/_tool"), [])


if __name__ == "__main__":
    unittest.main()
