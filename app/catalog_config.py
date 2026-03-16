"""Catalog ID and slug mappings for USC programmes (major/minor)."""
from typing import NamedTuple


class ProgramRef(NamedTuple):
    """Reference to a program by catalog and program id."""

    catoid: int
    poid: int


# Slug -> (catoid, poid). Current catalog 2025-2026 uses catoid=21.
# Add more programmes as needed; see https://catalogue.usc.edu/
SLUG_TO_PROGRAM: dict[str, ProgramRef] = {
    "csci-bs": ProgramRef(21, 29994),   # Computer Science (BS)
    "cs-bs": ProgramRef(21, 29994),      # alias
    "computer-science-bs": ProgramRef(21, 29994),
}


def resolve_slug(slug: str) -> ProgramRef | None:
    """Resolve a slug to (catoid, poid). Returns None if unknown."""
    normalized = slug.lower().strip().replace(" ", "-")
    return SLUG_TO_PROGRAM.get(normalized)
