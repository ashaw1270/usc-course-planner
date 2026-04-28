"""In-memory cache for scraped program data with TTL and force-refresh."""
import time
from typing import Any

from app.config import settings
from app.models import GeneralEducationCatalog, Program


class ProgramCache:
    """Simple TTL cache keyed by (catoid, poid)."""

    def __init__(self, ttl_seconds: int | None = None):
        self._ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl_seconds
        self._store: dict[tuple[int, int], tuple[Program, float]] = {}

    def get(self, catoid: int, poid: int, force_refresh: bool = False) -> Program | None:
        """Return cached Program if present and not expired. None otherwise."""
        if force_refresh:
            return None
        key = (catoid, poid)
        if key not in self._store:
            return None
        program, fetched_at = self._store[key]
        if time.monotonic() - fetched_at > self._ttl:
            del self._store[key]
            return None
        return program

    def set(self, catoid: int, poid: int, program: Program) -> None:
        """Store program in cache."""
        self._store[(catoid, poid)] = (program, time.monotonic())


class GeneralEducationCatalogCache:
    """Simple TTL cache keyed by (catoid, poid)."""

    def __init__(self, ttl_seconds: int | None = None):
        self._ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl_seconds
        self._store: dict[tuple[int, int], tuple[GeneralEducationCatalog, float]] = {}

    def get(self, catoid: int, poid: int, force_refresh: bool = False) -> GeneralEducationCatalog | None:
        """Return cached GE catalog if present and not expired. None otherwise."""
        if force_refresh:
            return None
        key = (catoid, poid)
        if key not in self._store:
            return None
        ge_catalog, fetched_at = self._store[key]
        if time.monotonic() - fetched_at > self._ttl:
            del self._store[key]
            return None
        return ge_catalog

    def set(self, catoid: int, poid: int, ge_catalog: GeneralEducationCatalog) -> None:
        """Store GE catalog in cache."""
        self._store[(catoid, poid)] = (ge_catalog, time.monotonic())


class CourseExistenceCache:
    """Simple TTL cache keyed by (term_code, normalized_course_id)."""

    def __init__(self, ttl_seconds: int | None = None):
        self._ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl_seconds
        self._store: dict[tuple[int, str], tuple[bool, float]] = {}

    def get(self, term_code: int, course_id: str, force_refresh: bool = False) -> bool | None:
        """Return cached existence for a normalized course id."""
        if force_refresh:
            return None
        key = (term_code, course_id)
        if key not in self._store:
            return None
        exists, fetched_at = self._store[key]
        if time.monotonic() - fetched_at > self._ttl:
            del self._store[key]
            return None
        return exists

    def set(self, term_code: int, course_id: str, exists: bool) -> None:
        """Store existence result for a normalized course id."""
        self._store[(term_code, course_id)] = (exists, time.monotonic())


# Module-level singleton
_cache: ProgramCache | None = None
_ge_cache: GeneralEducationCatalogCache | None = None
_course_existence_cache: CourseExistenceCache | None = None


def get_cache() -> ProgramCache:
    global _cache
    if _cache is None:
        _cache = ProgramCache()
    return _cache


def get_ge_cache() -> GeneralEducationCatalogCache:
    global _ge_cache
    if _ge_cache is None:
        _ge_cache = GeneralEducationCatalogCache()
    return _ge_cache


def get_course_existence_cache() -> CourseExistenceCache:
    global _course_existence_cache
    if _course_existence_cache is None:
        _course_existence_cache = CourseExistenceCache()
    return _course_existence_cache
