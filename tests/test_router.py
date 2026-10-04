from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io
import shutil
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
from skill_router.doctor import doctor
from skill_router.jev import JevProvider, recommend_local
from skill_router.manager import (
    apply_native_adoption,
    _run_curses_menu,
    apply_ripwire_adoption,
    config_after_sync,
    native_skill,
    plan_native_adoption,
    plan_ripwire_adoption,
    run_menu,
    sync_assignments,
)
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

    def test_select_alias_dispatches_to_manager(self) -> None:
        with patch("skill_router.cli._manage_command", return_value=0) as manage:
            self.assertEqual(main(["select"]), 0)
        manage.assert_called_once()

    def test_select_accepts_a_direct_search_query(self) -> None:
        with patch("skill_router.cli._manage_command", return_value=0) as manage:
            self.assertEqual(main(["select", "spreadsheet"]), 0)
        self.assertEqual(manage.call_args.args[0].query, "spreadsheet")

    def test_skills_alias_dispatches_to_manager(self) -> None:
        with patch("skill_router.cli._manage_command", return_value=0) as manage:
            self.assertEqual(main(["skills", "spreadsheet"]), 0)
        self.assertEqual(manage.call_args.args[0].query, "spreadsheet")

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

    def test_manage_syncs_links_when_saved(self) -> None:
        target = self.root / "codex"
        config = RouterConfig(
            target_roots=(("codex", target),),
            assignments=(
                SkillAssignment(
                    "python-debug",
                    self.root / "python-debug" / "SKILL.md",
                    frozenset({"codex"}),
                    native_targets=frozenset({"codex"}),
                ),
            ),
        )
        config_file = self.root / "config.toml"
        with patch.dict("os.environ", {"SKILL_ROUTER_CONFIG": str(config_file)}), patch(
            "skill_router.cli.run_menu", return_value=config
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["manage", "--root", str(self.root)]), 0)

        link = target / "python-debug"
        self.assertTrue(link.is_symlink())
        saved = load_config(config_file)
        self.assertEqual([(link.target, link.skill_id) for link in saved.managed_links], [("codex", "python-debug")])

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

class NativeSymlinkAdoptionTests(SkillFixture, unittest.TestCase):
    def test_native_adoption_uses_a_direct_symlink_source(self) -> None:
        home = self.root / "home"
        project_root = home / "projects" / "codexspin" / "skills"
        source = project_root / "codex"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: codex\ndescription: Run Codex.\n---\n\nUse Codex.\n",
            encoding="utf-8",
        )
        claude_root = home / ".claude" / "skills"
        claude_root.mkdir(parents=True)
        native_link = claude_root / "codex"
        native_link.symlink_to(source, target_is_directory=True)
        config = RouterConfig(target_roots=(("claude", claude_root),))

        skill = native_skill("codex", config, home=home)
        self.assertIsNotNone(skill)
        assert skill is not None
        self.assertEqual(skill.path.parent.resolve(), source.resolve())
        plan = plan_native_adoption(config, skill, home=home)

        saved, actions = apply_native_adoption(plan)
        router_skill = home / ".local" / "share" / "skill-router" / "skills" / "codex"
        self.assertTrue((router_skill / "SKILL.md").is_file())
        self.assertTrue(source.is_dir())
        self.assertTrue(native_link.is_symlink())
        self.assertEqual(native_link.resolve(), router_skill.resolve())
        self.assertEqual([action.action for action in actions], ["keep"])
        self.assertEqual(saved.assignment_map()["codex"].source, router_skill / "SKILL.md")

    def test_native_adoption_preserves_router_targets_and_records_direct_exposure(self) -> None:
        home = self.root / "home"
        claude_root = home / ".claude" / "skills"
        source = claude_root / "demystify-startup"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: demystify-startup\ndescription: Explain startup.\n---\n",
            encoding="utf-8",
        )
        config = RouterConfig(
            target_roots=(("claude", claude_root), ("codex", home / ".codex" / "skills")),
            assignments=(
                SkillAssignment(
                    "demystify-startup",
                    source / "SKILL.md",
                    frozenset({"claude", "codex"}),
                    native_targets=frozenset(),
                ),
            ),
        )
        skill = scan_roots([claude_root])[0]
        plan = plan_native_adoption(config, skill, home=home)
        assignment = plan.config.assignment_map()["demystify-startup"]
        self.assertEqual(assignment.targets, frozenset({"claude", "codex"}))
        self.assertEqual(assignment.native_targets, frozenset({"claude"}))

class NativeCopyAdoptionTests(SkillFixture, unittest.TestCase):
    def test_native_adoption_moves_direct_skill_to_router_storage(self) -> None:
        home = self.root / "home"
        shared_root = home / ".agents" / "skills"
        shared_skill = shared_root / "local-tools"
        shared_skill.mkdir(parents=True)
        (shared_skill / "SKILL.md").write_text(
            "---\nname: local-tools\ndescription: Find local tools.\n---\n\nUse the catalog.\n",
            encoding="utf-8",
        )
        claude_skill = home / ".claude" / "skills" / "local-tools"
        claude_skill.parent.mkdir(parents=True)
        shutil.copytree(shared_skill, claude_skill)
        codex_root = home / ".codex" / "skills"
        codex_root.mkdir(parents=True)
        (codex_root / "local-tools").symlink_to(shared_skill, target_is_directory=True)
        config = RouterConfig(
            target_roots=(("codex", codex_root), ("claude", claude_skill.parent))
        )
        skill = scan_roots([shared_root])[0]

        plan = plan_native_adoption(config, skill, home=home)
        self.assertEqual(plan.config.assignment_map()["local-tools"].targets, frozenset({"codex", "claude"}))

        saved, actions = apply_native_adoption(plan)
        router_skill = home / ".local" / "share" / "skill-router" / "skills" / "local-tools"
        self.assertTrue((router_skill / "SKILL.md").is_file())
        self.assertFalse(shared_skill.exists())
        self.assertTrue((codex_root / "local-tools").is_symlink())
        self.assertTrue((claude_skill.parent / "local-tools").is_symlink())
        self.assertTrue((home / ".local" / "share" / "skill-router" / "backups" / "local-tools" / ".agents").is_dir())
        self.assertTrue((home / ".local" / "share" / "skill-router" / "backups" / "local-tools" / ".claude").is_dir())
        self.assertEqual([action.action for action in actions], ["keep", "keep"])
        self.assertEqual(saved.assignment_map()["local-tools"].source, router_skill / "SKILL.md")

class StateManagerTests(SkillFixture, unittest.TestCase):
    def test_ripwire_adoption_moves_shared_links_to_managed_targets(self) -> None:
        home = self.root / "home"
        source_root = home / ".local" / "share" / "ripwire" / "skills"
        source = source_root / "ripwire-orient"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: ripwire-orient\ndescription: Orient a repository.\n---\n\nUse the map.\n",
            encoding="utf-8",
        )
        shared_root = home / ".agents" / "skills"
        shared_root.mkdir(parents=True)
        shared_link = shared_root / "ripwire-orient"
        shared_link.symlink_to(source, target_is_directory=True)
        config = RouterConfig(
            target_roots=(
                ("codex", home / ".codex" / "skills"),
                ("claude", home / ".claude" / "skills"),
            )
        )
        skill = scan_roots([source_root])[0]

        plan = plan_ripwire_adoption(config, [skill], home=home)
        self.assertEqual(plan.shared_links, (shared_link,))
        self.assertEqual(plan.config.assignment_map()["ripwire-orient"].targets, frozenset({"codex", "claude"}))

        saved, actions = apply_ripwire_adoption(plan)
        self.assertFalse(shared_link.exists() or shared_link.is_symlink())
        self.assertTrue((home / ".codex" / "skills" / "ripwire-orient").is_symlink())
        self.assertTrue((home / ".claude" / "skills" / "ripwire-orient").is_symlink())
        self.assertEqual([action.action for action in actions], ["link", "link"])
        self.assertEqual(len(saved.managed_links), 2)

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


class DoctorTests(SkillFixture, unittest.TestCase):
    def test_doctor_reports_clean_managed_state(self) -> None:
        codex = self.root / "home" / ".codex" / "skills"
        source = self.root / "vault" / "python-debug"
        source.mkdir(parents=True)
        skill_file = source / "SKILL.md"
        skill_file.write_text(
            "---\nname: python-debug\ndescription: Debug Python.\n---\n",
            encoding="utf-8",
        )
        destination = codex / "python-debug"
        destination.parent.mkdir(parents=True)
        destination.symlink_to(source, target_is_directory=True)
        config = RouterConfig(
            target_roots=(("codex", codex), ("claude", self.root / "home" / ".claude" / "skills")),
            assignments=(
                SkillAssignment(
                    "python-debug",
                    skill_file,
                    frozenset({"codex"}),
                    native_targets=frozenset({"codex"}),
                ),
            ),
            managed_links=(ManagedLink("codex", "python-debug", destination, skill_file),),
        )
        report = doctor(config, source_roots=[self.root / "vault"], home=self.root / "home", cwd=self.root)
        self.assertTrue(report.ok)
        self.assertEqual(report.errors, 0)
        self.assertEqual(report.warnings, 0)

    def test_doctor_reports_broken_managed_link(self) -> None:
        source = self.root / "vault" / "python-debug" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text("---\nname: python-debug\n---\n", encoding="utf-8")
        destination = self.root / "home" / ".codex" / "skills" / "python-debug"
        destination.parent.mkdir(parents=True)
        config = RouterConfig(
            target_roots=(("codex", destination.parent),),
            assignments=(
                SkillAssignment(
                    "python-debug",
                    source,
                    frozenset({"codex"}),
                    native_targets=frozenset({"codex"}),
                ),
            ),
            managed_links=(ManagedLink("codex", "python-debug", destination, source),),
        )
        report = doctor(config, source_roots=[self.root / "vault"], home=self.root / "home", cwd=self.root)
        self.assertFalse(report.ok)
        self.assertIn("broken_managed_link", {finding.code for finding in report.findings})

    def test_doctor_reports_link_for_disabled_assignment(self) -> None:
        source = self.root / "vault" / "python-debug" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text("---\nname: python-debug\n---\n", encoding="utf-8")
        destination = self.root / "home" / ".codex" / "skills" / "python-debug"
        destination.parent.mkdir(parents=True)
        destination.symlink_to(source.parent, target_is_directory=True)
        config = RouterConfig(
            target_roots=(("codex", destination.parent),),
            assignments=(
                SkillAssignment(
                    "python-debug",
                    source,
                    frozenset(),
                    enabled=False,
                    native_targets=frozenset(),
                ),
            ),
            managed_links=(ManagedLink("codex", "python-debug", destination, source),),
        )
        report = doctor(config, source_roots=[self.root / "vault"], home=self.root / "home", cwd=self.root)
        self.assertIn("stale_managed_link", {finding.code for finding in report.findings})

    def test_doctor_reports_duplicate_and_unmanaged_skills(self) -> None:
        first = self.root / "first" / "duplicate"
        nested = self.root / "first" / "nested" / "duplicate"
        second = self.root / "second" / "duplicate"
        native = self.root / "home" / ".codex" / "skills" / "native-only"
        for path in (first, nested, second, native):
            path.mkdir(parents=True)
            (path / "SKILL.md").write_text(
                f"---\nname: {path.name}\n---\n",
                encoding="utf-8",
            )
        config = RouterConfig(
            target_roots=(("codex", native.parent),),
        )
        report = doctor(
            config,
            source_roots=[self.root / "first", self.root / "second"],
            home=self.root / "home",
            cwd=self.root,
        )
        codes = {finding.code for finding in report.findings}
        self.assertIn("duplicate_skill", codes)
        self.assertIn("unmanaged_native", codes)


class MenuDisplayTests(SkillFixture, unittest.TestCase):
    def test_curses_keeps_column_header_above_rows(self) -> None:
        class FakeScreen:
            def __init__(self, width: int = 120) -> None:
                self.width = width
                self.keys = iter([10, 10, ord("s")])
                self.lines: list[tuple[int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (10, self.width)

            def erase(self) -> None:
                pass

            def addnstr(self, row: int, column: int, value: str, *args: object) -> None:
                count = args[0] if args else len(value)
                if (
                    not isinstance(count, int)
                    or column < 0
                    or column >= self.width
                    or count < 1
                    or column + count > self.width
                ):
                    raise AssertionError("screen write exceeds terminal width")
                self.lines.append((row, value))

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

            @staticmethod
            def curs_set(_value: int) -> None:
                pass

        skill = Skill(
            "demo",
            "demo",
            "A demo skill.",
            self.root / "demo" / "SKILL.md",
            "Full skill instructions.\nSecond line.",
        )

        def render(width: int) -> FakeScreen:
            screen = FakeScreen(width)
            FakeCurses.wrapper = staticmethod(lambda function: function(screen))
            with patch.dict("sys.modules", {"curses": FakeCurses}):
                _run_curses_menu([skill], RouterConfig(), target="codex", search="")
            return screen

        screen = render(120)

        header = "".join(value for row, value in screen.lines if row == 1)
        skill_row = next(value for row, value in screen.lines if row == 2)
        self.assertIn("router claude", header)
        self.assertIn("ROUTER CODEX", header)
        self.assertIn("description", header)
        self.assertTrue(skill_row.startswith("  1. demo"))
        self.assertIn("Full skill instructions.", [value for _, value in screen.lines])
        self.assertTrue(render(40).lines)

    def test_curses_arrows_select_columns(self) -> None:
        class FakeScreen:
            def __init__(self) -> None:
                self.keys = iter([261, 32, 10, 10, ord("s")])

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
