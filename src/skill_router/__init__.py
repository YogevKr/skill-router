"""Local, scoped routing for agent skills."""

from .catalog import Skill, load_skill, scan_roots
from .search import SearchResult, search_skills

__all__ = ["SearchResult", "Skill", "load_skill", "scan_roots", "search_skills"]

