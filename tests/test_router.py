from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from skill_router.catalog import Skill, load_skill, scan_roots
from skill_router.cli import main
from skill_router.config import (
    ManagedLink,
    RouterConfig,
    SkillAssignment,
    config_path,
    load_config,
    save_config,
)
from skill_router.jev import JevProvider, recommend_local
from skill_router.manager import _run_curses_menu, config_after_sync, run_menu, sync_assignments
from skill_router.search import search_skills
from skill_router.state import claude_plugin_roots, claude_skill_overrides, inspect_skills


class FakeJevClient:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[object, object, dict[str, object]]] = []

    def system_one(self, state: object, questions: object, **kwargs: object) -> object:
        self.calls.append((state, questions, kwargs))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class SkillFixture:
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "python-debug").mkdir()
        (self.root / "python-debug" / "SKILL.md").write_text(
            "---\nname: python-debug\ndescription: Debug Python tracebacks and failing tests.\n---\n\n# Debug\n\nRead the traceback.\n",
            encoding="utf-8",
        )
        (self.root / "react-ui").mkdir()
        (self.root / "react-ui" / "SKILL.md").write_text(
            "---\nname: react-ui\ndescription: Build React user interfaces.\n---\n\n# UI\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()


class RouterTests(SkillFixture, unittest.TestCase):

    def test_scans_and_loads_skill(self) -> None:
        skills = scan_roots([self.root])
        self.assertEqual([skill.skill_id for skill in skills], ["python-debug", "react-ui"])
        skill = load_skill("python-debug", [self.root])
        self.assertEqual(skill.name, "python-debug")
        self.assertIn("Read the traceback", skill.body)

    def test_search_ranks_matching_skill(self) -> None:
        results = search_skills(scan_roots([self.root]), "debug a Python traceback", limit=2)
        self.assertEqual(results[0].skill.skill_id, "python-debug")
        self.assertEqual(len(results), 1)

    def test_search_can_abstain(self) -> None:
        self.assertEqual(search_skills(scan_roots([self.root]), "manage a payroll", limit=3), [])

    def test_load_does_not_accept_a_path(self) -> None:
        with self.assertRaises(KeyError):
            load_skill("../python-debug", [self.root])


class JevTests(SkillFixture, unittest.TestCase):
    def test_local_recommendation_routes_one_skill(self) -> None:
        result = recommend_local(scan_roots([self.root]), "debug a Python traceback")
        self.assertEqual(result.status, "route")
        self.assertEqual(result.provider, "local")
        self.assertEqual(result.skill.skill_id, "python-debug")

    def test_jev_routes_from_metadata_without_skill_body(self) -> None:
        response = SimpleNamespace(
            model="jev-test",
            choices={
                "which": SimpleNamespace(choice="python-debug", confidence=0.9),
            },
            nouls={
                "fits::python-debug": SimpleNamespace(noul=0.8),
            },
            usage=SimpleNamespace(input_tokens=12, output_tokens=3),
        )
        client = FakeJevClient(response=response)
        result = JevProvider(client=client).recommend(
            scan_roots([self.root]), "debug a Python traceback"
        )
        self.assertEqual(result.status, "route")
        self.assertEqual(result.provider, "jev")
        self.assertEqual(result.skill.skill_id, "python-debug")
        state, questions, _ = client.calls[0]
        self.assertNotIn("body", str(state))
        self.assertIn("which", questions)
        self.assertIn("fits::python-debug", questions)
        self.assertEqual(result.metrics.input_tokens, 12)

    def test_jev_can_abstain(self) -> None:
        response = SimpleNamespace(
            model="jev-test",
            choices={"which": SimpleNamespace(choice="none", confidence=0.9)},
            nouls={},
            usage=SimpleNamespace(input_tokens=12, output_tokens=2),
        )
        result = JevProvider(client=FakeJevClient(response=response)).recommend(
            scan_roots([self.root]), "debug a Python traceback"
        )
        self.assertEqual(result.status, "no_tool")
        self.assertIsNone(result.skill)

    def test_jev_failure_falls_back_to_local(self) -> None:
        result = JevProvider(
            client=FakeJevClient(error=RuntimeError("offline"))
        ).recommend(scan_roots([self.root]), "debug a Python traceback")
        self.assertEqual(result.status, "fallback")
        self.assertEqual(result.provider, "local")
        self.assertEqual(result.skill.skill_id, "python-debug")
        self.assertIn("offline", result.error)


class ConfigTests(unittest.TestCase):
    def test_config_defaults_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            "os.environ", {"SKILL_ROUTER_CONFIG": str(Path(td) / "config.toml")}
        ):
            self.assertFalse(load_config().jev_enabled)

    def test_config_persists_jev_state(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            "os.environ", {"SKILL_ROUTER_CONFIG": str(Path(td) / "config.toml")}
        ):
            path = save_config(RouterConfig(jev_enabled=True))
            self.assertEqual(path, config_path())
            self.assertTrue(load_config().jev_enabled)

    def test_config_rejects_invalid_state(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            "os.environ", {"SKILL_ROUTER_CONFIG": str(Path(td) / "config.toml")}
        ):
            config = Path(td) / "config.toml"
            config.write_text("[jev]\nenabled = 'yes'\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config()

    def test_cli_persists_provider_state(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            "os.environ", {"SKILL_ROUTER_CONFIG": str(Path(td) / "config.toml")}
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["config", "set", "jev", "enabled"]), 0)
            self.assertTrue(load_config().jev_enabled)
            self.assertEqual(main(["config", "set", "jev", "disabled"]), 0)
            self.assertFalse(load_config().jev_enabled)

    def test_assignment_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            source = Path(td) / "hey" / "SKILL.md"
            config = RouterConfig(
                target_roots=(("codex", Path(td) / "codex"),),
                assignments=(
                    SkillAssignment("hey", source, frozenset({"codex"})),
                ),
                managed_links=(
                    ManagedLink("codex", "hey", Path(td) / "codex" / "hey", source),
                ),
            )
            save_config(config, path)
            loaded = load_config(path)
            self.assertEqual(loaded.assignments, config.assignments)
            self.assertEqual(loaded.roots()["codex"], Path(td) / "codex")
            self.assertEqual(loaded.managed_links, config.managed_links)

    def test_assignment_round_trip_native_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            source = Path(td) / "hey" / "SKILL.md"
            config = RouterConfig(
                assignments=(
                    SkillAssignment(
                        "hey",
                        source,
                        frozenset({"claude"}),
                        native_targets=frozenset({"codex"}),
                    ),
                ),
            )
            save_config(config, path)
            self.assertEqual(load_config(path).assignments, config.assignments)

    def test_target_json(self) -> None:
        with patch("skill_router.cli.load_config", return_value=RouterConfig()), patch(
            "skill_router.cli.config_path", return_value=Path("/tmp/config.toml")
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["config", "target", "show", "--json"]), 0)
            self.assertIn('"codex"', output.getvalue())

class StateTests(unittest.TestCase):
    def test_enabled_plugin_root_uses_latest_install(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            plugin_root = home / "plugin"
            plugin_root.mkdir()
            settings = home / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text('{"enabledPlugins": {"demo@market": true}}', encoding="utf-8")
            installed = home / ".claude" / "plugins" / "installed_plugins.json"
            installed.parent.mkdir(parents=True)
            installed.write_text(
                '{"plugins": {"demo@market": [{"installPath": "'
                + str(plugin_root)
                + '", "lastUpdated": "2026-10-01"}]}}',
                encoding="utf-8",
            )
            self.assertEqual(claude_plugin_roots(home=home, cwd=home), [plugin_root])

    def test_claude_skill_overrides_read_local_settings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            settings = home / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text(
                '{"skillOverrides": {"docs": "off", "xlsx": "user-invocable-only"}}',
                encoding="utf-8",
            )
            self.assertEqual(
                claude_skill_overrides(home=home, cwd=home),
                {"docs": "off", "xlsx": "user-invocable-only"},
            )


class ManagerTests(SkillFixture, unittest.TestCase):
    def test_menu_targets(self) -> None:
        current = RouterConfig()
        commands = iter(["1", "t claude", "2", "s"])
        updated = run_menu(
            scan_roots([self.root]),
            current,
            input_fn=lambda _: next(commands),
            output_fn=lambda _: None,
        )
        assignments = updated.assignment_map()
        self.assertEqual(assignments["python-debug"].targets, frozenset({"codex"}))
        self.assertEqual(assignments["react-ui"].targets, frozenset({"claude"}))

    def test_menu_controls_router_and_native_targets(self) -> None:
        commands = iter(["1", "m", "1", "t claude", "1", "s"])
        updated = run_menu(
            scan_roots([self.root]),
            RouterConfig(),
            input_fn=lambda _: next(commands),
            output_fn=lambda _: None,
        )
        assignment = updated.assignment_map()["python-debug"]
        self.assertEqual(assignment.targets, frozenset({"codex"}))
        self.assertEqual(assignment.native_targets, frozenset({"codex", "claude"}))

    def test_menu_imports_legacy_native_state(self) -> None:
        native_root = self.root / "codex"
        skill_dir = native_root / "native-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: native-skill\ndescription: Native skill.\n---\n",
            encoding="utf-8",
        )
        config = RouterConfig(
            target_roots=(("codex", native_root),),
            assignments=(
                SkillAssignment(
                    "native-skill",
                    skill_dir / "SKILL.md",
                    frozenset(),
                    enabled=False,
                ),
            ),
        )
        output: list[str] = []
        run_menu(
            scan_roots([native_root]),
            config,
            input_fn=lambda _: "q",
            output_fn=output.append,
        )
        row = next(value for value in output if value.lstrip().startswith("1."))
        self.assertEqual(row.split(" —", 1)[0].split()[-4:], ["no", "no", "yes", "no"])

    def test_sync_uses_native_targets(self) -> None:
        source = self.root / "python-debug" / "SKILL.md"
        codex = self.root / "codex"
        claude = self.root / "claude"
        config = RouterConfig(
            target_roots=(("codex", codex), ("claude", claude)),
            assignments=(
                SkillAssignment(
                    "python-debug",
                    source,
                    frozenset({"claude"}),
                    native_targets=frozenset({"codex"}),
                ),
            ),
        )
        actions = sync_assignments(config)
        self.assertEqual([(action.action, action.target) for action in actions], [("link", "codex")])

    def test_prune_does_not_remove_direct_native_folder(self) -> None:
        target = self.root / "target"
        direct = target / "python-debug"
        direct.mkdir(parents=True)
        (direct / "SKILL.md").write_text("keep", encoding="utf-8")
        config = RouterConfig(
            target_roots=(("codex", target),),
            managed_links=(),
        )
        actions = sync_assignments(config, apply=True, prune=True)
        self.assertEqual(actions, [])
        self.assertTrue(direct.is_dir())
        self.assertEqual((direct / "SKILL.md").read_text(encoding="utf-8"), "keep")

    def test_sync_links(self) -> None:
        source = self.root / "python-debug" / "SKILL.md"
        target = self.root / "target"
        current = RouterConfig(
            target_roots=(("codex", target),),
            assignments=(SkillAssignment("python-debug", source, frozenset({"codex"})),),
        )
        plan = sync_assignments(current)
        self.assertEqual([action.action for action in plan], ["link"])
        self.assertFalse((target / "python-debug").exists())
        actions = sync_assignments(current, apply=True)
        self.assertTrue((target / "python-debug").is_symlink())
        saved = config_after_sync(current, actions)
        self.assertEqual(len(saved.managed_links), 1)

        deselected = RouterConfig(
            target_roots=current.target_roots,
            assignments=(),
            managed_links=saved.managed_links,
        )
        applied_without_prune = sync_assignments(deselected, apply=True)
        preserved = config_after_sync(deselected, applied_without_prune)
        self.assertEqual(preserved.managed_links, saved.managed_links)
        prune = sync_assignments(preserved, apply=True, prune=True)
        self.assertEqual([action.action for action in prune], ["unlink"])
        self.assertFalse((target / "python-debug").exists())

    def test_sync_keeps_existing_destination(self) -> None:
        source = self.root / "python-debug" / "SKILL.md"
        target = self.root / "target"
        target.mkdir()
        existing = target / "python-debug"
        existing.write_text("keep", encoding="utf-8")
        config = RouterConfig(
            target_roots=(("codex", target),),
            assignments=(SkillAssignment("python-debug", source, frozenset({"codex"})),),
        )

        actions = sync_assignments(config, apply=True)

        self.assertEqual([action.action for action in actions], ["conflict"])
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep")

class StateManagerTests(SkillFixture, unittest.TestCase):
    def test_status_reads_frontmatter_mode(self) -> None:
        skill_path = self.root / "python-debug" / "SKILL.md"
        skill_path.write_text(
            "---\nname: python-debug\ndisable-model-invocation: true\n---\n\nRead it.\n",
            encoding="utf-8",
        )
        config = RouterConfig(target_roots=(("claude", self.root),))
        skill = scan_roots([self.root])[0]
        rows = inspect_skills([skill], config, home=self.root, cwd=self.root)
        self.assertEqual(rows[0].claude_mode, "user-invocable-only")
        self.assertEqual(rows[0].claude_lock, "frontmatter")

    def test_status_separates_router_and_native_state(self) -> None:
        codex = self.root / "codex"
        claude = self.root / "claude"
        source = self.root / "python-debug" / "SKILL.md"
        config = RouterConfig(
            target_roots=(("codex", codex), ("claude", claude)),
            assignments=(SkillAssignment("python-debug", source, frozenset({"codex"})),),
        )
        skill = scan_roots([self.root])[0]
        rows = inspect_skills([skill], config, home=self.root, cwd=self.root)
        self.assertEqual(rows[0].router_targets, ("codex",))
        self.assertEqual(rows[0].codex_exposure, "-")
        self.assertEqual(rows[0].claude_mode, "-")


class MenuDisplayTests(SkillFixture, unittest.TestCase):
    def test_curses_keeps_column_header_above_rows(self) -> None:
        class FakeScreen:
            def __init__(self) -> None:
                self.lines: list[tuple[int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (10, 120)

            def erase(self) -> None:
                pass

            def addnstr(self, row: int, _column: int, value: str, *_args: object) -> None:
                self.lines.append((row, value))

            def refresh(self) -> None:
                pass

            def keypad(self, _enabled: bool) -> None:
                pass

            def getch(self) -> int:
                return 10

            def attron(self, _attribute: object) -> None:
                pass

            def attroff(self, _attribute: object) -> None:
                pass

        class FakeCurses:
            KEY_UP = 259
            KEY_DOWN = 258
            KEY_PPAGE = 339
            KEY_NPAGE = 338
            KEY_HOME = 262
            KEY_END = 360
            KEY_LEFT = 260
            KEY_RIGHT = 261
            KEY_ENTER = 343
            A_BOLD = 1
            A_DIM = 2
            A_REVERSE = 3
            error = RuntimeError

            @staticmethod
            def wrapper(function: object) -> RouterConfig | None:
                return function(FakeScreen())

            @staticmethod
            def curs_set(_value: int) -> None:
                pass

        screen = FakeScreen()

        def wrapper(function: object) -> RouterConfig | None:
            return function(screen)

        FakeCurses.wrapper = staticmethod(wrapper)
        skill = Skill("demo", "demo", "A demo skill.", self.root / "demo" / "SKILL.md", "")
        with patch.dict("sys.modules", {"curses": FakeCurses}):
            _run_curses_menu([skill], RouterConfig(), target="codex", search="")

        header = next(value for row, value in screen.lines if row == 2)
        skill_row = next(value for row, value in screen.lines if row == 3)
        self.assertIn("router claude", header)
        self.assertIn("description", header)
        self.assertTrue(skill_row.startswith("  1. demo"))

    def test_curses_arrows_select_columns(self) -> None:
        class FakeScreen:
            def __init__(self) -> None:
                self.keys = iter([261, 32, 10])

            def getmaxyx(self) -> tuple[int, int]:
                return (10, 120)

            def erase(self) -> None:
                pass

            def addnstr(self, *_args: object) -> None:
                pass

            def refresh(self) -> None:
                pass

            def keypad(self, _enabled: bool) -> None:
                pass

            def getch(self) -> int:
                return next(self.keys)

            def attron(self, _attribute: object) -> None:
                pass

            def attroff(self, _attribute: object) -> None:
                pass

        class FakeCurses:
            KEY_UP = 259
            KEY_DOWN = 258
            KEY_PPAGE = 339
            KEY_NPAGE = 338
            KEY_HOME = 262
            KEY_END = 360
            KEY_LEFT = 260
            KEY_RIGHT = 261
            KEY_ENTER = 343
            A_BOLD = 1
            A_DIM = 2
            A_REVERSE = 3
            error = RuntimeError
            wrapper = staticmethod(lambda function: function(FakeScreen()))
            curs_set = staticmethod(lambda _value: None)

        skill = Skill("demo", "demo", "A demo skill.", self.root / "demo" / "SKILL.md", "")
        with patch.dict("sys.modules", {"curses": FakeCurses}):
            updated = _run_curses_menu([skill], RouterConfig(), target="codex", search="")

        self.assertEqual(updated.assignment_map()["demo"].native_targets, frozenset({"codex"}))

    def test_description_width(self) -> None:
        skill = Skill("long", "long", "word " * 100, self.root / "long" / "SKILL.md", "")
        output: list[str] = []
        with patch(
            "skill_router.manager.shutil.get_terminal_size",
            return_value=SimpleNamespace(columns=70, lines=24),
        ):
            run_menu([skill], RouterConfig(), input_fn=lambda _: "s", output_fn=output.append)

        row = next(value for value in output if value.lstrip().startswith("1.") and "long" in value)
        self.assertLessEqual(len(row), 70)
        self.assertTrue(row.endswith("..."))


if __name__ == "__main__":
    unittest.main()
