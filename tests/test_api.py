"""Integration tests for FastAPI endpoints (using HTML fixtures, no live HTTP)."""
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.scraper import parse_general_education_html, parse_program_html  # noqa: F401


def _load_fixture(name: str) -> str:
    base = __file__.replace("test_api.py", "fixtures")
    with open(f"{base}/{name}", encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def sample_ge_html():
    return _load_fixture("sample_ge_program.html")


@pytest.fixture
def sample_html():
    return _load_fixture("sample_program.html")


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_get_program_by_slug_unknown():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        r = await client.get("/programs/unknown-slug-xyz")
    assert r.status_code == 404
    assert "Unknown program slug" in r.json()["detail"]


@pytest.mark.asyncio
async def test_get_program_by_slug_success(monkeypatch, sample_html):
    """Test /programs/csci-bs returns program when scraper returns fixture data."""
    async def mock_fetch(catoid: int, poid: int, slug: str | None = None):
        return parse_program_html(sample_html, catoid, poid, slug)

    import app.main as main_module
    monkeypatch.setattr(main_module, "fetch_program", mock_fetch)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        r = await client.get("/programs/csci-bs")
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "Computer Science (BS)"
    assert data["catalog_year"] == "2025-2026"
    assert data["total_units_required"] == 128
    assert data["id"]["catoid"] == 21
    assert data["id"]["poid"] == 29994
    assert data["id"]["slug"] == "csci-bs"
    assert len(data["blocks"]) >= 5


@pytest.mark.asyncio
async def test_get_program_summary(monkeypatch, sample_html):
    async def mock_fetch(catoid: int, poid: int, slug: str | None = None):
        return parse_program_html(sample_html, catoid, poid, slug)

    import app.main as main_module
    monkeypatch.setattr(main_module, "fetch_program", mock_fetch)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        r = await client.get("/programs/csci-bs/summary")
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "Computer Science (BS)"
    assert data["slug"] == "csci-bs"
    assert data["total_units_required"] == 128
    assert "required_course_count" in data
    assert "block_count" in data


@pytest.mark.asyncio
async def test_get_ge_by_catalog_year(monkeypatch, sample_ge_html):
    async def mock_fetch_ge(catoid: int, poid: int):
        return parse_general_education_html(sample_ge_html, catoid, poid)

    import app.main as main_module
    monkeypatch.setattr(main_module, "fetch_general_education_catalog", mock_fetch_ge)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        r = await client.get("/ge/2025-2026?force_refresh=true")
    assert r.status_code == 200
    data = r.json()
    assert data["catalog_year"] == "2025-2026"
    assert len(data["categories"]) == 8


@pytest.mark.asyncio
async def test_get_ge_category(monkeypatch, sample_ge_html):
    async def mock_fetch_ge(catoid: int, poid: int):
        return parse_general_education_html(sample_ge_html, catoid, poid)

    import app.main as main_module
    monkeypatch.setattr(main_module, "fetch_general_education_catalog", mock_fetch_ge)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        r = await client.get("/ge/2025-2026/categories/GE-G?force_refresh=true")
    assert r.status_code == 200
    data = r.json()
    assert data["code"] == "GE-G"
    assert data["required_count"] == 1
    courses = {c["course_id"]: c["specific_students_only"] for c in data["courses"]}
    assert courses["PHIL 174gw"] is False
    assert courses["CORE 104gw"] is True


@pytest.mark.asyncio
async def test_get_ge_by_id_uses_cache(monkeypatch, sample_ge_html):
    call_count = 0

    async def mock_fetch_ge(catoid: int, poid: int):
        nonlocal call_count
        call_count += 1
        return parse_general_education_html(sample_ge_html, catoid, poid)

    import app.main as main_module
    monkeypatch.setattr(main_module, "fetch_general_education_catalog", mock_fetch_ge)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        r1 = await client.get("/ge/by-id?catoid=99&poid=199")
        r2 = await client.get("/ge/by-id?catoid=99&poid=199")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert call_count == 1
