"""FastAPI application entrypoint."""
import httpx
from fastapi import FastAPI, HTTPException, Query

from app.cache import get_cache, get_ge_cache
from app.catalog_config import resolve_slug
from app.models import AnyOfNode, AllOfNode, CourseNode, GeneralEducationCatalog, Program, SelectNode
from app.scraper import fetch_general_education_catalog, fetch_program

app = FastAPI(
    title="USC Catalogue API",
    description="API for retrieving course requirements for USC majors and minors from catalogue.usc.edu",
    version="0.1.0",
)

GE_CATALOG_YEAR_TO_IDS: dict[str, tuple[int, int]] = {
    "2025-2026": (21, 29462),
}


@app.get("/health")
async def health():
    """Simple status payload to ensure service is alive."""
    return {"status": "ok"}


async def _get_program(catoid: int, poid: int, slug: str | None, force_refresh: bool) -> Program:
    """Return program from cache or by fetching; slug only used for Program.id.slug."""
    cache = get_cache()
    if not force_refresh:
        cached = cache.get(catoid, poid, force_refresh=False)
        if cached is not None:
            return cached
    program = await fetch_program(catoid, poid, slug=slug)
    cache.set(catoid, poid, program)
    return program


async def _get_ge_catalog(catoid: int, poid: int, force_refresh: bool) -> GeneralEducationCatalog:
    """Return GE catalog from cache or by fetching."""
    cache = get_ge_cache()
    if not force_refresh:
        cached = cache.get(catoid, poid, force_refresh=False)
        if cached is not None:
            return cached
    ge_catalog = await fetch_general_education_catalog(catoid, poid)
    cache.set(catoid, poid, ge_catalog)
    return ge_catalog


@app.get("/programs/by-id", response_model=Program)
async def get_program_by_id(
    catoid: int = Query(..., description="Catalog ID (e.g. 21 for 2025-2026)"),
    poid: int = Query(..., description="Program object ID"),
    force_refresh: bool = Query(False, description="Bypass cache and re-scrape"),
):
    """Fetch program requirements by catalogue id and program id."""
    try:
        return await _get_program(catoid, poid, slug=None, force_refresh=force_refresh)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Program not found")
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")


@app.get("/programs/{slug}", response_model=Program)
async def get_program_by_slug(
    slug: str,
    force_refresh: bool = Query(False, description="Bypass cache and re-scrape"),
):
    """Fetch program requirements by slug (e.g. csci-bs)."""
    ref = resolve_slug(slug)
    if ref is None:
        raise HTTPException(status_code=404, detail=f"Unknown program slug: {slug}")
    try:
        return await _get_program(ref.catoid, ref.poid, slug=slug, force_refresh=force_refresh)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Program not found")
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")


@app.get("/programs/{slug}/summary")
async def get_program_summary(
    slug: str,
    force_refresh: bool = Query(False, description="Bypass cache and re-scrape"),
):
    """Return high-level summary: total units, counts of required vs elective courses."""
    ref = resolve_slug(slug)
    if ref is None:
        raise HTTPException(status_code=404, detail=f"Unknown program slug: {slug}")
    try:
        program = await _get_program(ref.catoid, ref.poid, slug=slug, force_refresh=force_refresh)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Program not found")
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")

    required_count = 0
    elective_count = 0
    total_block_units = 0

    def walk(node):
        nonlocal required_count, elective_count
        if isinstance(node, CourseNode):
            required_count += 1
            return
        if isinstance(node, SelectNode):
            # Select pools are elective-like choices.
            elective_count += 1
            for child in node.pool.items:
                walk(child)
            return
        if isinstance(node, AllOfNode):
            for c in node.children:
                walk(c)
            return
        if isinstance(node, AnyOfNode):
            elective_count += 1
            for opt in node.options:
                walk(opt)
            return

    for block in program.blocks:
        if block.min_units:
            total_block_units += block.min_units
        walk(block.root)

    return {
        "title": program.title,
        "slug": slug,
        "catalog_year": program.catalog_year,
        "total_units_required": program.total_units_required,
        "block_units_sum": total_block_units,
        "required_course_count": required_count,
        "elective_course_count": elective_count,
        "block_count": len(program.blocks),
    }


@app.get("/ge/by-id", response_model=GeneralEducationCatalog)
async def get_ge_by_id(
    catoid: int = Query(..., description="Catalog ID (e.g. 21 for 2025-2026)"),
    poid: int = Query(..., description="Program object ID for GE page"),
    force_refresh: bool = Query(False, description="Bypass cache and re-scrape"),
):
    """Fetch GE listing by catalogue id and GE program id."""
    try:
        return await _get_ge_catalog(catoid, poid, force_refresh=force_refresh)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="GE catalog page not found")
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")


@app.get("/ge/{catalog_year}", response_model=GeneralEducationCatalog)
async def get_ge_by_catalog_year(
    catalog_year: str,
    force_refresh: bool = Query(False, description="Bypass cache and re-scrape"),
):
    """Fetch GE listing for a known catalog year."""
    ids = GE_CATALOG_YEAR_TO_IDS.get(catalog_year)
    if ids is None:
        raise HTTPException(status_code=404, detail=f"Unknown GE catalog year: {catalog_year}")
    catoid, poid = ids
    try:
        return await _get_ge_catalog(catoid, poid, force_refresh=force_refresh)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="GE catalog page not found")
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")


@app.get("/ge/{catalog_year}/categories/{code}")
async def get_ge_category(
    catalog_year: str,
    code: str,
    force_refresh: bool = Query(False, description="Bypass cache and re-scrape"),
):
    """Fetch one GE category from a catalog year."""
    ids = GE_CATALOG_YEAR_TO_IDS.get(catalog_year)
    if ids is None:
        raise HTTPException(status_code=404, detail=f"Unknown GE catalog year: {catalog_year}")
    catoid, poid = ids
    ge_catalog = await _get_ge_catalog(catoid, poid, force_refresh=force_refresh)
    normalized = code.strip().upper()
    category = next((c for c in ge_catalog.categories if c.code == normalized), None)
    if category is None:
        raise HTTPException(status_code=404, detail=f"Unknown GE category code: {normalized}")
    return {
        "catalog_year": ge_catalog.catalog_year,
        "code": category.code,
        "label": category.label,
        "required_count": category.required_count,
        "courses": [c.model_dump() for c in category.courses],
    }
