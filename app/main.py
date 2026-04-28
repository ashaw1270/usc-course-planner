"""FastAPI application entrypoint."""
from pathlib import Path
import re

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.cache import get_cache, get_ge_cache
from app.catalog_config import resolve_slug
from app.course_lookup import get_course_existence_service
from app.models import AnyOfNode, AllOfNode, CourseNode, GeneralEducationCatalog, Program, SelectNode
from planner.requirement_eval import (
    EvaluateBody,
    ProgramEvaluationResult,
    evaluate_program,
    normalize_course_id,
)
from app.scraper import fetch_general_education_catalog, fetch_program

# GE listing used for planner /evaluate alongside the selected major program (USC catalogue IDs).
GE_EVAL_CATOID = 21
GE_EVAL_POID = 29462

app = FastAPI(
    title="USC Catalogue API",
    description="API for retrieving course requirements for USC majors and minors from catalogue.usc.edu",
    version="0.1.0",
)


class CourseExistenceResult(BaseModel):
    course_id: str
    normalized_course_id: str
    exists: bool
    term_code: int


def _fall_term_code_from_catalog_year(catalog_year: str) -> int:
    """Map 'YYYY-YYYY' catalog year to fall term code YYYY3."""
    m = re.search(r"(\d{4})\s*-\s*(\d{4})", catalog_year or "")
    if not m:
        raise ValueError(f"Cannot derive term code from catalog year: {catalog_year!r}")
    start_year = int(m.group(1))
    return start_year * 10 + 3

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


@app.post("/programs/evaluate", response_model=ProgramEvaluationResult)
async def post_evaluate_program(
    body: EvaluateBody,
    catoid: int = Query(..., description="Catalog ID (e.g. 21 for 2025-2026)"),
    poid: int = Query(..., description="Program object ID"),
    force_refresh: bool = Query(False, description="Bypass cache and re-scrape"),
):
    """Evaluate how taken courses satisfy each requirement block (advisory only)."""
    try:
        program = await _get_program(catoid, poid, slug=None, force_refresh=force_refresh)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Program not found")
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")

    ge_catalog: GeneralEducationCatalog | None = None
    ge_error: str | None = None
    try:
        ge_catalog = await _get_ge_catalog(GE_EVAL_CATOID, GE_EVAL_POID, force_refresh=force_refresh)
    except httpx.HTTPStatusError as e:
        ge_error = f"General education catalog unavailable (HTTP {e.response.status_code})."
    except httpx.RequestError as e:
        ge_error = f"General education catalog unavailable: {e!s}"

    course_lookup = get_course_existence_service()
    term_code = _fall_term_code_from_catalog_year(program.catalog_year)

    recognized: list[str] = []
    unrecognized: list[str] = []
    seen: set[str] = set()
    for raw in body.taken:
        trimmed = raw.strip()
        if not trimmed:
            continue
        normalized = normalize_course_id(trimmed)
        if not normalized:
            continue
        exists = await course_lookup.course_exists(term_code, normalized, force_refresh=force_refresh)
        if exists:
            if normalized not in seen:
                recognized.append(normalized)
                seen.add(normalized)
        else:
            unrecognized.append(trimmed)

    return evaluate_program(
        program,
        recognized,
        ge_catalog=ge_catalog,
        ge_error=ge_error,
        unrecognized_courses=unrecognized,
    )


@app.get("/courses/exists", response_model=CourseExistenceResult)
async def get_course_exists(
    course_id: str = Query(..., description="Course id to check, e.g. CSCI 102L"),
    term_code: int = Query(..., description="USC term code (e.g. 20263 for Fall 2026)"),
    force_refresh: bool = Query(False, description="Bypass cache and re-check upstream"),
):
    """Return whether a course exists in USC classes API for a given term code."""
    normalized = normalize_course_id(course_id)
    if not normalized:
        raise HTTPException(status_code=422, detail="course_id is required")
    exists = await get_course_existence_service().course_exists(
        term_code,
        normalized,
        force_refresh=force_refresh,
    )
    return CourseExistenceResult(
        course_id=course_id,
        normalized_course_id=normalized,
        exists=exists,
        term_code=term_code,
    )


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


_planner_web_dir = Path(__file__).resolve().parent.parent / "planner" / "web"
if _planner_web_dir.is_dir():
    app.mount(
        "/planner",
        StaticFiles(directory=str(_planner_web_dir), html=True),
        name="planner",
    )
