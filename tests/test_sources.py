from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skill_router.catalog import scan_roots
from skill_router.cli import main
from skill_router.config import ConfigError, RouterConfig, SkillAssignment, load_config, save_config
from skill_router.doctor import doctor
from skill_router.manager import apply_source_adoption, config_after_sync, plan_source_adoption, sync_assignments


class SourceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.config_path = self.home / "config.toml"
        self.enterContext(patch("pathlib.Path.home", return_value=self.home))
        self.enterContext(patch("pathlib.Path.cwd", return_value=self.home))
        self.enterContext(patch.dict("os.environ", {
            "SKILL_ROUTER_CONFIG": str(self.config_path),
            "SKILL_ROUTER_ROOT": "",
        }))

    def skill(self, root: Path, name: str) -> Path:
        source = root / name / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text(f"---\nname: {name}\ndescription: Debug a Python traceback.\n---\n\nRead it.\n")
        return source

    def cli(self, *args: str) -> tuple[int, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            result = main(list(args))
        return result, output.getvalue() + errors.getvalue()

    def managed_ids(self, *args: str) -> set[str]:
        with patch("skill_router.cli.run_menu", return_value=None) as menu:
            result, _ = self.cli("manage", *args)
        self.assertEqual(result, 0)
        return {skill.skill_id for skill in menu.call_args.args[0]}

    def test_source_registration_round_trip_preserves_assignments(self) -> None:
        source = self.skill(self.home / "external", "traceback")
        assignment = SkillAssignment("traceback", source, frozenset({"codex"}))
        save_config(RouterConfig(assignments=(assignment,)))
        self.assertEqual(self.cli("config", "source", "set", "team", str(source.parent.parent))[0], 0)
        config = load_config()
        self.assertEqual(dict(config.source_roots), {"team": source.parent.parent})
        self.assertEqual(config.assignments, (assignment,))
        code, output = self.cli("config", "source", "show", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {"team": str(source.parent.parent)})
        self.assertEqual(self.cli("config", "source", "remove", "team")[0], 0)
        self.assertEqual(load_config().source_roots, ())
        self.assertEqual(load_config().assignments, (assignment,))
        self.assertTrue(source.is_file())
        self.assertEqual(self.managed_ids(), {"traceback"})

    def test_manager_save_preserves_registered_sources_for_future_discovery(self) -> None:
        root = self.home / "external"
        self.skill(root, "first")
        config = RouterConfig(source_roots=(("team", root),))
        save_config(config)
        with patch("sys.stdin", io.StringIO("s\n")):
            self.assertEqual(self.cli("manage")[0], 0)
        self.assertEqual(load_config().source_roots, config.source_roots)
        self.skill(root, "second")
        self.assertEqual(self.managed_ids(), {"first", "second"})
        self.assertEqual(self.cli("adopt", "team")[0], 0)

    def test_invalid_source_configuration_is_rejected(self) -> None:
        for text in ('sources = []', '[sources]\nteam = 42', '[sources]\nteam = "relative"',
                     '[sources]\nnative = "/tmp"', '[sources]\n" " = "/tmp"'):
            with self.subTest(text=text):
                self.config_path.write_text(text)
                with self.assertRaises(ConfigError):
                    load_config()

    def test_unicode_source_names_and_paths_survive_manager_saves(self) -> None:
        root = self.home / "team🚀"
        source = self.skill(root, "traceback🚀")
        name = "team🚀\x7f"
        self.assertEqual(self.cli("config", "source", "set", name, str(root))[0], 0)
        self.assertEqual(dict(load_config().source_roots), {name: root})
        with patch("sys.stdin", io.StringIO("s\n")):
            self.assertEqual(self.cli("manage")[0], 0)
        self.assertEqual(load_config().assignment_map()["traceback🚀"].source, source)
        self.assertEqual(self.cli("config", "source", "remove", name)[0], 0)
        self.assertEqual(load_config().source_roots, ())

    def test_source_cli_rejects_missing_symlink_and_reserved_inputs(self) -> None:
        directory = self.home / "external"
        directory.mkdir()
        link = self.home / "link"
        link.symlink_to(directory)
        for name, path in (("native", directory), (" ", directory), ("team", link),
                           ("team", self.home / "missing")):
            with self.subTest(name=name, path=path):
                self.assertEqual(self.cli("config", "source", "set", name, str(path))[0], 2)
        self.assertFalse(self.config_path.exists())

    def test_manage_includes_native_configured_and_assigned_sources(self) -> None:
        self.skill(self.home / ".agents" / "skills", "shared")
        self.skill(self.home / ".codex" / "skills", "codex")
        self.skill(self.home / ".claude" / "skills", "claude")
        self.skill(self.home / ".local" / "share" / "skill-router" / "skills", "stored")
        self.skill(self.home / "team-skills", "team-skill")
        self.skill(self.home / "another-source", "another-skill")
        remembered = self.skill(self.home / "old-source", "remembered")
        save_config(RouterConfig(
            source_roots=(("team", self.home / "team-skills"), ("another", self.home / "another-source")),
            assignments=(SkillAssignment("remembered", remembered, frozenset(), enabled=False),),
        ))
        expected = {"shared", "codex", "claude", "stored", "team-skill", "another-skill", "remembered"}
        self.assertEqual(self.managed_ids(), expected)
        self.assertEqual(self.managed_ids("--all"), expected)
        code, output = self.cli("status", "--json")
        self.assertEqual(code, 0)
        rows = {row["id"]: row for row in json.loads(output)}
        self.assertEqual(set(rows), expected)
        self.assertEqual(rows["team-skill"]["source"], "source:team")
        self.assertEqual(rows["another-skill"]["source"], "source:another")
        self.assertEqual(rows["shared"]["codex"], "native")
        self.assertEqual(rows["shared"]["claude"], "native")

    def test_manage_uses_custom_target_roots(self) -> None:
        root = self.home / "custom-codex"
        self.skill(root, "custom")
        save_config(RouterConfig(target_roots=(("codex", root),)))
        self.assertEqual(self.managed_ids(), {"custom"})

    def test_manager_save_does_not_copy_runtime_exposure_into_native_link_requests(self) -> None:
        plugin_root = self.home / ".claude" / "plugins" / "cache" / "demo" / "1.0"
        self.skill(plugin_root / "skills", "plugin-skill")
        self.skill(self.home / ".codex" / "skills" / ".system", "bundled-skill")
        self.skill(self.home / ".claude" / "skills" / "synced" / "workspace", "synced-skill")
        self.skill(self.home / ".agents" / "skills", "shared-skill")
        settings = self.home / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"demo@market": True}}))
        installed = self.home / ".claude" / "plugins" / "installed_plugins.json"
        installed.write_text(json.dumps({"plugins": {"demo@market": [{"installPath": str(plugin_root)}]}}))
        expected = {"plugin-skill", "bundled-skill", "synced-skill", "shared-skill"}
        self.assertEqual(self.managed_ids(), expected)
        with patch("sys.stdin", io.StringIO("s\n")):
            self.assertEqual(self.cli("manage")[0], 0)
        saved = load_config()
        self.assertEqual(saved.managed_links, ())
        for name in expected:
            self.assertEqual(saved.assignment_map()[name].native_targets, frozenset())
            for target in (".codex", ".claude"):
                self.assertFalse((self.home / target / "skills" / name).exists())
        settings.write_text(json.dumps({"enabledPlugins": {"demo@market": False}}))
        code, output = self.cli("status", "--json")
        self.assertEqual(code, 0)
        rows = {row["id"]: row for row in json.loads(output)}
        self.assertEqual(rows["plugin-skill"]["claude"], "-")
        self.assertEqual(rows["plugin-skill"]["claude_mode"], "-")

    def test_saved_source_wins_when_newly_visible_sources_share_its_id(self) -> None:
        self.skill(self.home / ".local" / "share" / "skill-router" / "skills", "shared-id")
        self.skill(self.home / "team", "shared-id")
        self.skill(self.home / ".claude" / "skills", "shared-id")
        saved = self.skill(self.home / "chosen", "shared-id")
        save_config(RouterConfig(
            source_roots=(("team", self.home / "team"),),
            assignments=(SkillAssignment("shared-id", saved, frozenset({"codex"})),),
        ))
        with patch("skill_router.cli.run_menu", return_value=None) as menu:
            self.assertEqual(self.cli("manage")[0], 0)
        self.assertEqual(menu.call_args.args[0][0].path, saved)
        code, output = self.cli("load", "shared-id", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["path"], str(saved))

    def test_exact_saved_files_win_over_nested_skills_in_other_assignments(self) -> None:
        first = self.skill(self.home / "sources", "a")
        self.skill(first.parent / "examples", "z")
        chosen = self.skill(self.home / "chosen", "z")
        save_config(RouterConfig(
            source_roots=(("team", self.home / "sources"),),
            assignments=(
                SkillAssignment("a", first, frozenset({"codex"})),
                SkillAssignment("z", chosen, frozenset({"codex"})),
            ),
        ))
        code, output = self.cli("load", "z", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["path"], str(chosen))
        with patch("sys.stdin", io.StringIO("s\n")):
            self.assertEqual(self.cli("manage")[0], 0)
        self.assertEqual(load_config().assignment_map()["z"].source, chosen)

    def test_explicit_roots_limit_management_and_search(self) -> None:
        self.skill(self.home / ".agents" / "skills", "native")
        self.skill(self.home / "external", "external")
        self.skill(self.home / "selected", "selected")
        save_config(RouterConfig(source_roots=(("external", self.home / "external"),)))
        self.assertEqual(self.managed_ids("--root", str(self.home / "selected")), {"selected"})
        code, output = self.cli("search", "Python", "--root", str(self.home / "selected"), "--json")
        self.assertEqual(code, 0)
        self.assertEqual([row["id"] for row in json.loads(output)], ["selected"])
        with patch.dict("os.environ", {"SKILL_ROUTER_ROOT": str(self.home / "selected")}):
            code, output = self.cli("search", "Python", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([row["id"] for row in json.loads(output)], ["selected"])

    def test_search_load_and_recommend_use_configured_sources(self) -> None:
        source = self.skill(self.home / "external", "traceback")
        self.skill(self.home / ".agents" / "skills", "unassigned-native")
        save_config(RouterConfig(
            source_roots=(("team", self.home / "external"),),
            assignments=(SkillAssignment("traceback", source, frozenset({"codex"})),),
        ))
        code, output = self.cli("search", "Python", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([row["id"] for row in json.loads(output)], ["traceback"])
        code, output = self.cli("load", "traceback", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["id"], "traceback")
        code, output = self.cli("recommend", "Python", "--target", "codex", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["skill"]["id"], "traceback")
        code, output = self.cli("recommend", "Python", "--target", "claude", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "no_tool")

    def test_ripwire_requires_the_same_registration_as_other_sources(self) -> None:
        root = self.home / ".local" / "share" / "ripwire" / "skills"
        self.skill(root, "orient")
        self.assertEqual(self.managed_ids(), set())
        self.assertEqual(self.cli("adopt", "ripwire")[0], 2)
        self.assertEqual(self.cli("config", "source", "set", "tools", str(root))[0], 0)
        self.assertEqual(self.managed_ids(), {"orient"})
        self.assertEqual(self.cli("adopt", "tools")[0], 0)

    def test_doctor_does_not_report_duplicates_for_overlapping_roots(self) -> None:
        root = self.home / "external"
        source = self.skill(root, "traceback")
        config = RouterConfig(
            source_roots=(("team", root), ("alias", root)),
            assignments=(SkillAssignment("traceback", source, frozenset(), enabled=False),),
        )
        report = doctor(config, home=self.home, cwd=self.home)
        self.assertFalse(any(finding.code == "duplicate_skill" for finding in report.findings))

    def adoption(self):
        root = self.home / "external"
        source = self.skill(root, "plain-name")
        shared = self.home / ".agents" / "skills" / "plain-name"
        shared.parent.mkdir(parents=True)
        shared.symlink_to(source.parent)
        config = RouterConfig(source_roots=(("team", root),))
        plan = plan_source_adoption(config, scan_roots([root]), source_root=root, home=self.home)
        return root, source, shared, config, plan

    def test_source_adoption_stops_on_conflict_without_a_name_prefix(self) -> None:
        _, source, shared, _, plan = self.adoption()
        destination = self.home / ".codex" / "skills" / "plain-name"
        destination.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "cannot adopt source safely"):
            apply_source_adoption(plan)
        self.assertTrue(shared.is_symlink())
        self.assertTrue(source.is_file())
        self.assertFalse((self.home / ".claude" / "skills").exists())

    def test_source_adoption_preserves_shared_links_used_as_agent_targets(self) -> None:
        root, source, shared, config, _ = self.adoption()
        config = replace(config, target_roots=(("codex", shared.parent),))
        plan = plan_source_adoption(config, scan_roots([root]), source_root=root, home=self.home)
        self.assertEqual(plan.shared_links, ())
        saved, actions = apply_source_adoption(plan)
        self.assertEqual(shared.resolve(), source.parent)
        self.assertTrue(shared.is_symlink())
        self.assertTrue(any(action.action == "keep" and action.path == shared for action in actions))
        self.assertTrue(all(link.path.is_symlink() for link in saved.managed_links))

    def test_source_adoption_preserves_target_roots_that_alias_the_shared_root(self) -> None:
        root, source, shared, config, _ = self.adoption()
        alias = self.home / "shared-alias"
        alias.symlink_to(shared.parent)
        config = replace(config, target_roots=(("codex", alias),))
        plan = plan_source_adoption(config, scan_roots([root]), source_root=root, home=self.home)
        self.assertEqual(plan.shared_links, ())
        saved, _ = apply_source_adoption(plan)
        self.assertEqual(shared.resolve(), source.parent)
        self.assertTrue(shared.is_symlink())
        self.assertTrue(all(link.path.is_symlink() for link in saved.managed_links))

    def test_source_adoption_prunes_a_disabled_shared_target_only_once(self) -> None:
        root, source, shared, config, _ = self.adoption()
        config = replace(config, target_roots=(("codex", shared.parent),))
        plan = plan_source_adoption(config, scan_roots([root]), source_root=root, home=self.home)
        saved, _ = apply_source_adoption(plan)
        assignment = replace(saved.assignment_map()["plain-name"], native_targets=frozenset())
        saved = replace(saved, assignments=(assignment,))
        plan = plan_source_adoption(saved, scan_roots([root]), source_root=root, home=self.home)
        self.assertEqual(plan.shared_links, ())
        saved, actions = apply_source_adoption(plan)
        self.assertFalse(shared.is_symlink())
        self.assertTrue(source.is_file())
        self.assertEqual(saved.managed_links, ())
        self.assertEqual(sum(action.path == shared and action.action == "unlink" for action in actions), 1)

    def test_shared_target_link_remains_until_the_last_agent_releases_it(self) -> None:
        root, source, shared, config, _ = self.adoption()
        alias = self.home / "shared-alias"
        alias.symlink_to(shared.parent)
        config = replace(config, target_roots=(("codex", shared.parent), ("claude", alias)))
        plan = plan_source_adoption(config, scan_roots([root]), source_root=root, home=self.home)
        saved, _ = apply_source_adoption(plan)
        self.assertEqual(len(saved.managed_links), 2)
        for targets in (frozenset({"codex"}), frozenset()):
            assignment = replace(saved.assignment_map()["plain-name"], native_targets=targets)
            saved = replace(saved, assignments=(assignment,))
            plan = plan_source_adoption(saved, scan_roots([root]), source_root=root, home=self.home)
            saved, _ = apply_source_adoption(plan)
            self.assertEqual(shared.is_symlink(), bool(targets))
            self.assertEqual({link.target for link in saved.managed_links}, set(targets))
        self.assertTrue(source.is_file())

    def test_source_adoption_removes_one_link_when_both_shared_targets_are_disabled(self) -> None:
        root, source, shared, config, _ = self.adoption()
        config = replace(config, target_roots=(("codex", shared.parent), ("claude", shared.parent)))
        plan = plan_source_adoption(config, scan_roots([root]), source_root=root, home=self.home)
        saved, _ = apply_source_adoption(plan)
        assignment = replace(saved.assignment_map()["plain-name"], native_targets=frozenset())
        saved = replace(saved, assignments=(assignment,))
        plan = plan_source_adoption(saved, scan_roots([root]), source_root=root, home=self.home)
        saved, actions = apply_source_adoption(plan)
        self.assertFalse(shared.is_symlink())
        self.assertEqual(saved.managed_links, ())
        self.assertEqual(sum(action.action == "unlink" for action in actions), 1)
        self.assertEqual(sum(action.action == "forget" for action in actions), 1)
        self.assertTrue(source.is_file())

    def test_sync_creates_one_link_for_two_targets_in_the_same_directory(self) -> None:
        source = self.skill(self.home / "external", "plain-name")
        target = self.home / "shared-target"
        config = RouterConfig(
            target_roots=(("codex", target), ("claude", target)),
            assignments=(SkillAssignment("plain-name", source, frozenset({"codex", "claude"})),),
        )
        actions = sync_assignments(config, apply=True)
        saved = config_after_sync(config, actions)
        self.assertEqual((target / "plain-name").resolve(), source.parent)
        self.assertEqual(len(saved.managed_links), 2)

    def test_source_conflict_preserves_shared_ownership_until_cleanup(self) -> None:
        original = self.skill(self.home / "original", "plain-name")
        replacement = self.skill(self.home / "replacement", "plain-name")
        target = self.home / "shared-target"
        config = RouterConfig(
            target_roots=(("codex", target), ("claude", target)),
            assignments=(SkillAssignment("plain-name", original, frozenset({"codex"})),),
        )
        saved = config_after_sync(config, sync_assignments(config, apply=True))
        original_links = saved.managed_links
        requested = SkillAssignment("plain-name", replacement, frozenset({"claude"}))
        saved = replace(saved, assignments=(requested,))
        actions = sync_assignments(saved, apply=True, prune=True)
        saved = config_after_sync(saved, actions)
        self.assertEqual({action.action for action in actions}, {"conflict", "skip-prune"})
        self.assertEqual(saved.managed_links, original_links)
        self.assertEqual((target / "plain-name").resolve(), original.parent)
        saved = replace(saved, assignments=(replace(requested, enabled=False),))
        saved = config_after_sync(saved, sync_assignments(saved, apply=True, prune=True))
        self.assertEqual(saved.managed_links, ())
        self.assertFalse((target / "plain-name").is_symlink())
        self.assertTrue(original.is_file())
        self.assertTrue(replacement.is_file())

    def test_source_adoption_stops_if_shared_link_changes(self) -> None:
        _, _, shared, _, plan = self.adoption()
        other = self.skill(self.home / "other", "plain-name")
        shared.unlink()
        shared.symlink_to(other.parent)
        with self.assertRaisesRegex(ValueError, "shared link changed"):
            apply_source_adoption(plan)
        self.assertEqual(shared.resolve(), other.parent)
        self.assertFalse((self.home / ".codex" / "skills").exists())

    def test_source_adoption_preserves_unrelated_assignments_and_links(self) -> None:
        root, source, shared, config, _ = self.adoption()
        other = self.skill(self.home / "other", "unrelated")
        assignment = SkillAssignment("unrelated", other, frozenset({"codex"}))
        config = replace(config, assignments=(assignment,))
        plan = plan_source_adoption(config, scan_roots([root]), source_root=root, home=self.home)
        saved, actions = apply_source_adoption(plan)
        self.assertEqual(saved.assignment_map()["unrelated"], assignment)
        self.assertTrue(all(action.skill_id == "plain-name" for action in actions))
        self.assertFalse((self.home / ".codex" / "skills" / "unrelated").exists())
        self.assertFalse(shared.is_symlink())
        self.assertTrue(source.is_file())
        self.assertEqual((self.home / ".codex" / "skills" / "plain-name").resolve(), source.parent)

    def test_source_adoption_refuses_to_replace_a_different_saved_source(self) -> None:
        root, _, shared, config, _ = self.adoption()
        other = self.skill(self.home / "other", "plain-name")
        config = replace(config, assignments=(SkillAssignment("plain-name", other, frozenset({"codex"})),))
        with self.assertRaisesRegex(ValueError, "different source"):
            plan_source_adoption(config, scan_roots([root]), source_root=root, home=self.home)
        self.assertTrue(shared.is_symlink())

    def test_adopt_cli_plans_then_applies_a_configured_source(self) -> None:
        _, source, shared, config, _ = self.adoption()
        save_config(config)
        before = self.config_path.read_bytes()
        code, output = self.cli("adopt", "team")
        self.assertEqual(code, 0)
        self.assertIn("dry-run", output)
        self.assertTrue(shared.is_symlink())
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self.cli("adopt", "team", "--apply")[0], 0)
        self.assertFalse(shared.is_symlink())
        saved = load_config()
        self.assertEqual(saved.assignment_map()["plain-name"].source, source)
        self.assertEqual(len(saved.managed_links), 2)
        self.assertEqual(saved.source_roots, config.source_roots)
