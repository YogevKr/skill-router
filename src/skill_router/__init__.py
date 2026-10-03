"""Local, scoped routing for agent skills."""

from .catalog import Skill, load_skill, scan_roots
from .config import (
    ConfigError,
    ManagedLink,
    RouterConfig,
    SkillAssignment,
    config_path,
    effective_native_targets,
    load_config,
    save_config,
)
from .jev import JevMetrics, JevProvider, Recommendation, recommend_local
from .manager import (
    RipwireAdoptionPlan,
    SyncAction,
    apply_ripwire_adoption,
    assignment_config,
    config_after_sync,
    default_source_roots,
    plan_ripwire_adoption,
    ripwire_source_root,
    run_menu,
    sync_assignments,
)
from .search import SearchResult, search_skills

__all__ = [
    "JevMetrics",
    "JevProvider",
    "ConfigError",
    "ManagedLink",
    "Recommendation",
    "RouterConfig",
    "SkillAssignment",
    "effective_native_targets",
    "SyncAction",
    "RipwireAdoptionPlan",
    "apply_ripwire_adoption",
    "assignment_config",
    "SearchResult",
    "Skill",
    "load_skill",
    "config_path",
    "config_after_sync",
    "default_source_roots",
    "plan_ripwire_adoption",
    "ripwire_source_root",
    "load_config",
    "recommend_local",
    "run_menu",
    "save_config",
    "scan_roots",
    "search_skills",
    "sync_assignments",
]
