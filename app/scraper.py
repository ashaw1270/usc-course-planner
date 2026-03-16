"""Fetch and parse USC catalogue program pages into structured models."""
import re
from typing import Literal

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.models import (
    CourseGroupRequirement,
    CourseRequirement,
    FreeElectiveRequirement,
    Program,
    ProgramId,
    RequirementBlock,
    TextNote,
)


def _block_kind_from_title(title: str) -> Literal["core", "elective", "ge", "pre_major", "supporting", "other"]:
    """Infer requirement block kind from section title."""
    t = title.lower()
    if "pre-major" in t or "pre major" in t:
        return "pre_major"
    if "general education" in t or "ge " in t or "gen ed" in t:
        return "ge"
    if "elective" in t and "free" in t:
        return "elective"
    if "technical elective" in t or "elective" in t:
        return "elective"
    if "major requirement" in t or "core" in t:
        return "core"
    if "composition" in t or "writing" in t:
        return "supporting"
    return "other"


def _parse_units_from_title(title: str) -> tuple[int | None, int | None]:
    """Extract (min_units, max_units) from a block title like 'Major Requirements (62 Units)'."""
    match = re.search(r"\((\d+)\s*[Uu]nits?\)", title)
    if match:
        u = int(match.group(1))
        return (u, u)
    return (None, None)


def _parse_course_line(link_text: str, units_text: str | None) -> tuple[str, str | None, float | None]:
    """Parse 'CSCI 102L Introduction to Programming' and 'Units: 2' into course_id, title, units."""
    course_id = ""
    title = None
    # Course ID is typically first token(s): e.g. CSCI 102L, WRIT 150, MATH 125g
    parts = link_text.strip().split(None, 2)
    if len(parts) >= 2:
        # Handle "MATH 125g" or "CSCI 102L" or "WRIT 150"
        course_id = f"{parts[0]} {parts[1]}"
        if len(parts) == 3:
            title = parts[2].strip()
    elif len(parts) == 1:
        course_id = parts[0]
    units = None
    if units_text:
        um = re.search(r"Units?:\s*(\d+(?:\.\d+)?)", units_text, re.I)
        if um:
            units = float(um.group(1))
    return (course_id, title, units)


def _slug_from_title(title: str) -> str:
    """Generate a simple id/slug from block title for RequirementBlock.id."""
    s = re.sub(r"[^\w\s]", "", title.lower())
    s = re.sub(r"\s+", "_", s.strip())
    return s or "block"


def _extract_total_units(description_text: str) -> int | None:
    """Parse 'minimum requirement for the degree is 128 units' or 'Total Units: 128'."""
    m = re.search(r"(?:degree is|total units?:\s*)(\d+)\s*units?", description_text, re.I)
    return int(m.group(1)) if m else None


def _extract_program_notes(description_el) -> list[str]:
    """Collect program-wide notes from description paragraphs."""
    notes: list[str] = []
    if not description_el:
        return notes
    for p in description_el.find_all("p"):
        text = p.get_text(separator=" ", strip=True)
        if text and len(text) > 10:
            notes.append(text)
    return notes


def _parse_course_li(li) -> CourseRequirement | None:
    """Parse a single li.acalog-course into CourseRequirement."""
    a = li.find("a", href=True)
    if not a:
        return None
    link_text = a.get_text(strip=True)
    span = li.find("span")
    rest = ""
    if span:
        rest = span.get_text(strip=True)
    # Rest often includes "Units: 4" and possibly "or" / "and" / footnotes
    units_text = rest.replace(link_text, "").strip() if link_text in rest else rest
    course_id, title, units = _parse_course_line(link_text, units_text)
    if not course_id:
        return None
    return CourseRequirement(
        course_id=course_id,
        title=title or None,
        units=units,
        required=True,
    )


def _parse_block(block_el, block_title: str, min_units: int | None, max_units: int | None, kind: str) -> RequirementBlock:
    """Parse a div.acalog-core into RequirementBlock."""
    block_id = _slug_from_title(block_title)
    items: list = []
    notes: list[str] = []

    # Sub-blocks (h3, h4) under this block
    for elem in block_el.find_all(["h2", "h3", "h4", "ul", "p"], recursive=True):
        if elem.name == "p":
            text = elem.get_text(strip=True)
            if text and not re.match(r"^\s*$", text):
                if "one of the following" in text.lower() or "take at least" in text.lower():
                    notes.append(text)
                else:
                    items.append(TextNote(text=text))
            continue
        if elem.name in ("h2", "h3", "h4"):
            # When we hit a new heading at same level, we could break; for now keep collecting
            continue
        if elem.name == "ul":
            # Decide whether this <ul> is a simple list or an \"or\"/\"and\" choice group.
            # We treat \"or\" as indicating alternatives and \"and\" as courses that belong
            # to the same sequence within a choice.
            ul_items: list = []

            # First, collect all course <li> elements in order and detect connectors.
            course_lis: list[tuple[int, any]] = []  # (index, li)
            for idx, li in enumerate(elem.find_all("li", class_="acalog-course")):
                course_lis.append((idx, li))

            connectors: dict[int, str] = {}

            # Detect trailing \"and\"/\"or\" in course span text.
            for idx, li in course_lis:
                span = li.find("span")
                tail = span.get_text(" ", strip=True).lower() if span else li.get_text(" ", strip=True).lower()
                m = re.search(r"(and|or)\s*$", tail)
                if m:
                    connectors[idx] = m.group(1)

            # Detect standalone acalog-adhoc \"or\"/\"and\" between courses.
            adhoc_lis = elem.find_all("li", class_=re.compile("acalog-adhoc"))
            if adhoc_lis:
                # Map course <li> elements in DOM order so we can find the previous one.
                all_lis = [li for li in elem.find_all("li")]
                index_by_li = {li: i for i, li in enumerate(all_lis)}
                course_order = [index_by_li[li] for _, li in course_lis]
                for adhoc in adhoc_lis:
                    text = adhoc.get_text(strip=True).lower()
                    if text not in ("and", "or"):
                        continue
                    pos = index_by_li.get(adhoc)
                    if pos is None:
                        continue
                    # Find the last course before this adhoc li.
                    prev_course_idx = None
                    for idx_in_ul, order_pos in reversed(list(enumerate(course_order))):
                        if order_pos < pos:
                            prev_course_idx = idx_in_ul
                            break
                    if prev_course_idx is not None:
                        connectors[prev_course_idx] = text

            # Build list of CourseRequirement objects in order, plus connectors.
            courses: list[CourseRequirement] = []
            course_connectors: list[str | None] = []
            for idx, li in course_lis:
                cr = _parse_course_li(li)
                if not cr:
                    continue
                courses.append(cr)
                course_connectors.append(connectors.get(idx))

            if not courses:
                continue

            # Partition into local segments so that we can mix required courses
            # with local choice groups in a single <ul>.
            segments: list[tuple[list[CourseRequirement], list[str | None], bool]] = []
            seg_courses: list[CourseRequirement] = []
            seg_conns: list[str | None] = []
            seg_has_or = False

            for cr, conn in zip(courses, course_connectors):
                seg_courses.append(cr)
                seg_conns.append(conn)
                if conn == "or":
                    seg_has_or = True
                if conn is None:
                    # End of this local segment.
                    segments.append((seg_courses, seg_conns, seg_has_or))
                    seg_courses = []
                    seg_conns = []
                    seg_has_or = False

            # If the last course had a connector != None, close the trailing segment.
            if seg_courses:
                segments.append((seg_courses, seg_conns, seg_has_or))

            # Turn segments into either flat required courses or structured choice groups.
            for seg_courses, seg_conns, seg_has_or in segments:
                if not seg_has_or:
                    # No \"or\" in this segment: treat all courses here as required.
                    ul_items.extend(seg_courses)
                    continue

                # Segment contains at least one \"or\": build a CourseGroupRequirement.
                sequences: list[list[CourseRequirement]] = []
                current_seq: list[CourseRequirement] = []
                for cr, conn in zip(seg_courses, seg_conns):
                    current_seq.append(cr)
                    if conn == "and":
                        # same option sequence continues
                        continue
                    # conn is \"or\" or None => end current sequence
                    sequences.append(current_seq)
                    current_seq = []
                if current_seq:
                    sequences.append(current_seq)

                group_label = block_title
                # If we previously captured a \"one of the following\" note, use that.
                for n in notes:
                    if "one of the following" in n.lower():
                        group_label = n
                        break

                group = CourseGroupRequirement(
                    group_label=group_label,
                    min_courses=None,
                    max_courses=None,
                    min_units=None,
                    max_units=None,
                    courses=[],
                    options=sequences,
                    notes=[],
                )
                ul_items.append(group)

            items.extend(ul_items)

    return RequirementBlock(
        id=block_id,
        title=block_title,
        min_units=min_units,
        max_units=max_units,
        kind=kind,
        items=items,
        notes=notes,
    )


def _parse_free_elective_block(block_title: str, min_units: int | None, max_units: int | None) -> RequirementBlock:
    """Create a block that is just free electives."""
    return RequirementBlock(
        id=_slug_from_title(block_title),
        title=block_title,
        min_units=min_units,
        max_units=max_units,
        kind="elective",
        items=[FreeElectiveRequirement(min_units=min_units or 0, max_units=max_units)],
        notes=[],
    )


def _parse_total_units_block(block_title: str) -> tuple[int | None, RequirementBlock | None]:
    """Parse 'Total Units: 128' block; return (total_units, optional block for display)."""
    m = re.search(r"Total\s+Units?:\s*(\d+)", block_title, re.I)
    if m:
        return (int(m.group(1)), RequirementBlock(id="total_units", title=block_title, kind="other", items=[TextNote(text=block_title)]))
    return (None, None)


def parse_program_html(html: str, catoid: int, poid: int, slug: str | None = None) -> Program:
    """Parse full program page HTML into a Program model."""
    soup = BeautifulSoup(html, "lxml")
    warnings: list[str] = []

    # Program title
    title_el = soup.find("h1", id="acalog-page-title")
    title = title_el.get_text(strip=True) if title_el else ""

    # Catalog year
    catalog_el = soup.find("span", class_="acalog_catalog_name")
    catalog_year = ""
    if catalog_el:
        raw = catalog_el.get_text(strip=True)
        m = re.search(r"(\d{4}-\d{4})", raw)
        if m:
            catalog_year = m.group(1)
        else:
            catalog_year = raw

    # Level and type from title/description
    level: Literal["undergraduate", "graduate"] = "undergraduate"
    program_type: Literal["major", "minor", "certificate"] = "major"
    if "minor" in title.lower():
        program_type = "minor"
    elif "certificate" in title.lower():
        program_type = "certificate"
    if "graduate" in title.lower() or "master" in title.lower() or "ph.d" in title.lower():
        level = "graduate"

    # Description and total units / notes
    desc_el = soup.find("div", class_=re.compile("program_description"))
    description_text = desc_el.get_text(separator=" ", strip=True) if desc_el else ""
    total_units = _extract_total_units(description_text)
    program_notes = _extract_program_notes(desc_el)

    # Restrict to main content (td.block_content) to avoid nav/footer
    content_root = title_el.find_parent("td", class_="block_content") if title_el else None
    search_root = content_root if content_root else soup
    core_divs = search_root.find_all("div", class_="acalog-core")

    # All acalog-core divs (requirement sections)
    blocks: list[RequirementBlock] = []
    seen: set[str] = set()
    for div in core_divs:
        h2 = div.find("h2")
        h3 = div.find("h3")
        h4 = div.find("h4")
        heading = h2 or h3 or h4
        if not heading:
            continue
        block_title = heading.get_text(strip=True)
        min_u, max_u = _parse_units_from_title(block_title)
        kind = _block_kind_from_title(block_title)

        # Total Units: 128
        tot, tot_block = _parse_total_units_block(block_title)
        if tot is not None:
            total_units = total_units or tot
            if tot_block and tot_block.id not in seen:
                blocks.append(tot_block)
                seen.add(tot_block.id)
            continue

        # Free Electives
        if "free elective" in block_title.lower():
            b = _parse_free_elective_block(block_title, min_u, max_u)
            if b.id not in seen:
                blocks.append(b)
                seen.add(b.id)
            continue

        # Technical Electives (no course list, just note)
        if "technical elective" in block_title.lower():
            items: list = []
            for p in div.find_all("p"):
                items.append(TextNote(text=p.get_text(strip=True)))
            b = RequirementBlock(id=_slug_from_title(block_title), title=block_title, min_units=min_u, max_units=max_u, kind="elective", items=items, notes=[])
            if b.id not in seen:
                blocks.append(b)
                seen.add(b.id)
            continue

        block = _parse_block(div, block_title, min_u, max_u, kind)
        # Skip empty parent headers (e.g. "Pre-Major Requirements" with no direct ul)
        if (block.items or block.notes) and block.id not in seen:
            blocks.append(block)
            seen.add(block.id)

    # Blocks already de-duplicated by id during construction
    unique_blocks = blocks

    return Program(
        id=ProgramId(catoid=catoid, poid=poid, slug=slug),
        title=title,
        level=level,
        type=program_type,
        catalog_year=catalog_year,
        total_units_required=total_units,
        blocks=unique_blocks,
        notes=program_notes,
        warnings=warnings,
    )


async def fetch_program(catoid: int, poid: int, slug: str | None = None) -> Program:
    """Fetch program page from USC catalogue and return parsed Program."""
    url = f"{settings.catalogue_base_url.rstrip('/')}/preview_program.php"
    params = {"catoid": catoid, "poid": poid}
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
    return parse_program_html(response.text, catoid, poid, slug)
