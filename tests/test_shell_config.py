"""engine/config.py's reading of the shell's config: the plugin is enabled
exactly when the shell would say so (PluginRegistry.isEnabled for a
third-party plugin), and any file that cannot be read says enabled.

Every path points into a temp dir, HOME included: the real
shell.json is never read.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import config  # noqa: E402

ID = config.PLUGIN_ID


class Rule(unittest.TestCase):
    def enabled(self, data) -> bool:
        # The shell only uses a file with version 1; give every case one.
        if isinstance(data, dict) and "version" not in data:
            data = {"version": 1, **data}
        return config.plugin_enabled_in(data)

    def test_the_id_is_the_manifests(self):
        manifest = json.loads((ROOT / "manifest.json").read_text("utf-8"))
        self.assertEqual(ID, manifest["id"])

    def test_a_bar_entry_in_any_section(self):
        for section in ("left", "centerBegin", "center", "centerEnd", "right", "tray"):
            self.assertTrue(self.enabled({"bar": {"layout": {section: [{"id": ID}]}}}), section)
        # Entries can be the bare id too.
        self.assertTrue(self.enabled({"bar": {"layout": {"right": ["omarchy.clock", ID]}}}))

    def test_a_nested_entry(self):
        group = {"id": "some.group", "entries": [{"id": "omarchy.clock"}, {"id": ID}]}
        self.assertTrue(self.enabled({"bar": {"layout": {"right": [group]}}}))
        self.assertTrue(self.enabled({"bar": {"layout": {"right": [[ID]]}}}))

    def test_a_plugins_entry(self):
        self.assertTrue(self.enabled({"plugins": [{"id": "other"}, {"id": ID}]}))
        # The shell reads plugins[j].id; a bare string there names nothing.
        self.assertFalse(self.enabled({"plugins": [ID]}))

    def test_the_selected_bar(self):
        self.assertTrue(self.enabled({"bar": {"id": ID}}))

    def test_not_named_anywhere_is_disabled(self):
        self.assertFalse(self.enabled({}))
        self.assertFalse(self.enabled({"bar": {"layout": {"right": [{"id": "omarchy.clock"}]}},
                                       "plugins": [{"id": "io.github.hominluo.podcast"}]}))
        # Close is not the same.
        self.assertFalse(self.enabled({"bar": {"layout": {"right": [{"id": ID + "-clone"}]}}}))

    def test_disabled_plugins_wins(self):
        data = {"bar": {"layout": {"right": [{"id": ID}]}}, "plugins": [{"id": ID}],
                "disabledPlugins": ["omarchy.lock", ID]}
        self.assertFalse(self.enabled(data))
        data["disabledPlugins"] = ["omarchy.lock"]
        self.assertTrue(self.enabled(data))

    def test_odd_shapes_do_not_raise(self):
        for data in ({"bar": None}, {"bar": {"layout": []}}, {"bar": {"layout": {"right": None}}},
                     {"plugins": {"id": ID}}, {"disabledPlugins": ID},
                     {"bar": {"layout": {"right": [None, 3, {"id": None}]}}}):
            self.assertFalse(self.enabled(data), data)

    def test_no_object_is_no_answer(self):
        for data in (None, [], "text", 3):
            self.assertTrue(self.enabled(data), data)

    def test_a_file_without_version_1_is_no_answer(self):
        # The shell ignores such a file and runs on its defaults, so what it
        # lists says nothing about what the shell has loaded.
        for version in (None, 2, "1", True, 0):
            data = {"disabledPlugins": [ID]}
            if version is not None:
                data["version"] = version
            self.assertTrue(config.plugin_enabled_in(data), version)
        self.assertFalse(config.plugin_enabled_in({"version": 1, "disabledPlugins": [ID]}))


class File(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="am-shellcfg-")
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.root / "home"),
                                                "XDG_CONFIG_HOME": str(self.root / "elsewhere")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, text: str) -> Path:
        path = config.shell_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        return path

    def test_path_is_the_shells_not_xdg(self):
        # shell.qml hard-codes $HOME/.config; XDG_CONFIG_HOME must not move it.
        self.assertEqual(config.shell_config_path(),
                         self.root / "home" / ".config" / "omarchy" / "shell.json")

    def test_plugin_installed_follows_the_unresolved_root(self):
        plugin = self.root / "plugins" / ID
        plugin.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"APPLE_MUSIC_PLUGIN_ROOT": str(plugin)}):
            self.assertFalse(config.plugin_installed())
            (plugin / "manifest.json").write_text("{}", "utf-8")
            self.assertTrue(config.plugin_installed())
        # Without the override it is the directory this engine was loaded from.
        self.assertEqual(config.plugin_root(), ROOT)

    def test_reads_the_file(self):
        self.write(json.dumps({"version": 1, "bar": {"layout": {"right": [{"id": ID}]}}}))
        stamp, enabled = config.plugin_enabled()
        self.assertTrue(enabled)
        self.assertIsNotNone(stamp)
        self.write(json.dumps({"version": 1, "bar": {"layout": {"right": [{"id": ID}]}}, "disabledPlugins": [ID]}))
        self.assertFalse(config.plugin_enabled()[1])

    def test_missing_unreadable_or_half_written_means_enabled(self):
        self.assertEqual(config.plugin_enabled(), (None, True))
        self.write('{"bar": {"layout": {"right": [')
        self.assertTrue(config.plugin_enabled()[1])
        self.write("")
        self.assertTrue(config.plugin_enabled()[1])
        self.write("[" * 100000 + "]" * 100000)  # past json's recursion limit
        self.assertTrue(config.plugin_enabled()[1])
        depth = 5000  # deeper than the parser or the search can follow
        self.write('{"bar": {"layout": {"right": [' + '{"id": "g", "entries": [' * depth
                   + '{"id": "g"}' + "]}" * depth + "]}}}")
        self.assertTrue(config.plugin_enabled()[1])
        config.shell_config_path().write_bytes(b"\xff\xfe{")
        self.assertTrue(config.plugin_enabled()[1])
        path = config.shell_config_path()
        path.unlink()
        path.mkdir()  # a directory where the file should be
        self.assertTrue(config.plugin_enabled()[1])

    def test_the_stamp_moves_with_every_save(self):
        self.assertIsNone(config.shell_config_stamp())
        path = self.write("{}")
        first = config.shell_config_stamp()
        self.assertEqual(config.shell_config_stamp(), first)
        # Same size, same second: an atomic replace still shows (new inode).
        temp = path.with_name("shell.json.tmp")
        temp.write_text("[]", "utf-8")
        stamp = time.time() - 100
        os.utime(temp, (stamp, stamp))
        os.utime(path, (stamp, stamp))
        before = config.shell_config_stamp()
        os.replace(temp, path)
        self.assertNotEqual(config.shell_config_stamp(), before)


if __name__ == "__main__":
    unittest.main()
