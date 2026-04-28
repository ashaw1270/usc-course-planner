"""Course existence lookup service backed by classes.usc.edu Basic API."""
from __future__ import annotations

import re

import httpx

from app.cache import CourseExistenceCache, get_course_existence_cache
from app.config import settings
from planner.requirement_eval import normalize_course_id


_BASIC_SEARCH_API = "https://classes.usc.edu/api/Search/Basic"


def _normalize_lookup_token(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().upper())


def _course_matches_result(normalized_course_id: str, course: dict) -> bool:
    candidates: list[str] = []
    if isinstance(course, dict):
        for key in ("fullCourseName",):
            v = course.get(key)
            if isinstance(v, str):
                candidates.append(v)
        for key in ("scheduledCourseCode", "publishedCourseCode", "matchedCourseCode"):
            code_obj = course.get(key)
            if isinstance(code_obj, dict):
                for subkey in ("courseSpace", "courseHyphen", "courseSmashed"):
                    sv = code_obj.get(subkey)
                    if isinstance(sv, str):
                        candidates.append(sv)
    target = _normalize_lookup_token(normalized_course_id)
    target_smashed = target.replace(" ", "")
    for raw in candidates:
        c = _normalize_lookup_token(raw)
        if c == target or c.replace("-", " ") == target:
            return True
        if c.replace("-", "").replace(" ", "") == target_smashed:
            return True
    return False


async def _lookup_course_in_schedule(term_code: int, normalized_course_id: str) -> bool:
    """Use USC classes Basic search API to determine if a course exists in the term."""
    params = {
        "termCode": term_code,
        "searchTerm": normalized_course_id.lower(),
    }
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        response = await client.get(_BASIC_SEARCH_API, params=params)
        response.raise_for_status()
        payload = response.json()
    courses = payload.get("courses") if isinstance(payload, dict) else None
    if not isinstance(courses, list):
        return False
    return any(_course_matches_result(normalized_course_id, course) for course in courses)


class CourseExistenceService:
    """Checks whether course ids exist for a term code, with TTL cache."""

    def __init__(self, cache: CourseExistenceCache | None = None):
        self._cache = cache or get_course_existence_cache()

    async def course_exists(self, term_code: int, course_id: str, *, force_refresh: bool = False) -> bool:
        """Return whether the normalized course id exists in USC classes schedule API."""
        normalized = normalize_course_id(course_id)
        if not normalized:
            return False

        cached = self._cache.get(term_code, normalized, force_refresh=force_refresh)
        if cached is not None:
            return cached

        if not re.fullmatch(r"[A-Z]{2,5} \d{1,4}[A-Z]*", normalized):
            self._cache.set(term_code, normalized, False)
            return False

        try:
            exists = await _lookup_course_in_schedule(term_code, normalized)
        except (httpx.HTTPError, ValueError):
            exists = False
        self._cache.set(term_code, normalized, exists)
        return exists


_course_existence_service: CourseExistenceService | None = None


def get_course_existence_service() -> CourseExistenceService:
    global _course_existence_service
    if _course_existence_service is None:
        _course_existence_service = CourseExistenceService()
    return _course_existence_service
