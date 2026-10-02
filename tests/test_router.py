from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from skill_router.catalog import load_skill, scan_roots
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
from skill_router.manager import config_after_sync, run_menu, sync_assignments
from skill_router.search import search_skills


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

    def test_target_json(self) -> None:
        with patch("skill_router.cli.load_config", return_value=RouterConfig()), patch(
            "skill_router.cli.config_path", return_value=Path("/tmp/config.toml")
        ), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["config", "target", "show", "--json"]), 0)
            self.assertIn('"codex"', output.getvalue())


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
        prune = sync_assignments(deselected, apply=True, prune=True)
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


if __name__ == "__main__":
    unittest.main()
