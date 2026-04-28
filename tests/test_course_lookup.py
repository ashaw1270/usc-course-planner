"""Tests for course existence lookup with USC classes Basic API."""
import pytest

from app.cache import CourseExistenceCache
from app.course_lookup import CourseExistenceService


@pytest.mark.asyncio
async def test_course_exists_true_when_basic_api_has_exact_match(monkeypatch):
    cache = CourseExistenceCache(ttl_seconds=60)
    service = CourseExistenceService(cache=cache)

    async def upstream_result(term_code: int, normalized_course_id: str) -> bool:
        return term_code == 20263 and normalized_course_id == "CSCI 467"

    monkeypatch.setattr("app.course_lookup._lookup_course_in_schedule", upstream_result)
    exists = await service.course_exists(20263, "CSCI 467")
    assert exists is True


@pytest.mark.asyncio
async def test_course_exists_false_when_basic_api_has_no_exact_match(monkeypatch):
    cache = CourseExistenceCache(ttl_seconds=60)
    service = CourseExistenceService(cache=cache)

    async def upstream_false(term_code: int, normalized_course_id: str) -> bool:
        return False

    monkeypatch.setattr("app.course_lookup._lookup_course_in_schedule", upstream_false)
    exists = await service.course_exists(20263, "ZZZZ 1234")
    assert exists is False
