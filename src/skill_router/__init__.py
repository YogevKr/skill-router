"""Local, scoped routing for agent skills."""

from .catalog import Skill, load_skill, scan_roots
from .config import (
    ConfigError,
    ManagedLink,
    RouterConfig,
    SkillAssignment,
    config_path,
    load_config,
    save_config,
)
from .jev import JevMetrics, JevProvider, Recommendation, recommend_local
from .manager import (
    SyncAction,
    assignment_config,
    config_after_sync,
    default_source_roots,
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
    "SyncAction",
    "assignment_config",
    "SearchResult",
    "Skill",
    "load_skill",
    "config_path",
    "config_after_sync",
    "default_source_roots",
    "load_config",
    "recommend_local",
    "run_menu",
    "save_config",
    "scan_roots",
    "search_skills",
    "sync_assignments",
]
