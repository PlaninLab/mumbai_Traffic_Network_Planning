"""Build the BMC/MMRDA major-road coverage inventory from OpenStreetMap.

This module deliberately contains *geometry and collection targets only*.  It
does not create speeds, flows, capacities, queues, V/C ratios, or any other
traffic observation.  Every discovered junction starts with
``status="awaiting_collection"`` so the collection pipeline can enrich it
later with real provider observations.

The two scopes are nested by construction:

* ``bmc`` is the union of the OSM Mumbai City and Mumbai Suburban district
  boundary relations (the Greater Mumbai/BMC extent used by this project).
* ``mmrda`` is the OSM Mumbai Metropolitan Region boundary.  The effective
  MMRDA polygon is unioned with BMC before extraction, which protects the
  containment invariant from small boundary topology discrepancies in OSM.

Only OSM motorway, trunk, primary and secondary ways (and their ``*_link``
ways) are downloaded.  Nearby topology nodes belonging to the same roads are
consolidated into one physical junction, avoiding a marker for each carriageway
or interchange ramp node.

CLI examples::

    # Fetch/update OSM geometry and write data/processed/map/coverage.json
    python -m src.network.coverage --download

    # Rebuild the payload without network access from the cached raw extract
    python -m src.network.coverage

OpenStreetMap data is licensed under ODbL: https://www.openstreetmap.org/copyright
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_OSM_DIR = REPO_ROOT / "data" / "raw" / "osm"
MAP_DIR = REPO_ROOT / "data" / "processed" / "map"

GRAPH_CACHE = RAW_OSM_DIR / "coverage_major.graphml"
BOUNDARY_CACHE = RAW_OSM_DIR / "coverage_boundaries.geojson"
COVERAGE_JSON = MAP_DIR / "coverage.json"

# These are stable OSM relation identifiers, not Nominatim name searches.
OSM_RELATIONS = {
    "mmrda": 13312356,              # Mumbai Metropolitan Region
    "mumbai_suburban": 7964375,     # Mumbai Suburban District
    "mumbai_city": 7964376,         # Mumbai City District
}

OSM_COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
OSM_ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0"

ROAD_CLASSES = ["motorway", "trunk", "primary", "secondary", "other"]
MAJOR_HIGHWAYS = tuple(ROAD_CLASSES[:4])
MAJOR_HIGHWAY_TAGS = tuple(
    tag
    for base in MAJOR_HIGHWAYS
    for tag in (base, f"{base}_link")
)

# OSMnx accepts Overpass QL fragments as custom_filter.  Keeping this narrow is
# important: the MMR is large, while this product is an inventory of major
# intersections rather than a turn-by-turn street graph.
MAJOR_ROAD_FILTER = (
    '["highway"~"^(motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link)$"]'
    '["area"!~"yes"]'
)

SCHEMA_VERSION = 1
DEFAULT_CLUSTER_RADIUS_M = 90.0

# Cartographic display anchors for orientation on the deliberately tile-free
# map. Coordinates follow OpenStreetMap/Nominatim place or locality centres;
# they are labels, not analytical zones or traffic observations. ``min_zoom``
# and ``max_zoom`` keep the wide MMRDA view legible while revealing finer BMC
# locality context as the user moves closer.
AREA_LABEL_ANCHORS: tuple[dict[str, Any], ...] = (
    # Metropolitan anchors visible in the default MMRDA view.
    {"name": "Mumbai", "lat": 19.0759, "lon": 72.8777,
     "kind": "city", "min_zoom": 8.0, "max_zoom": 9.7},
    {"name": "Vasai-Virar", "lat": 19.4210, "lon": 72.8197,
     "kind": "city", "min_zoom": 8.3, "max_zoom": 9.45},
    {"name": "Mira-Bhayandar", "lat": 19.2960, "lon": 72.8550,
     "kind": "city", "min_zoom": 8.3},
    {"name": "Thane", "lat": 19.1943, "lon": 72.9702,
     "kind": "city", "min_zoom": 8.2},
    {"name": "Bhiwandi", "lat": 19.3026, "lon": 73.0588,
     "kind": "city", "min_zoom": 8.3},
    {"name": "Kalyan-Dombivli", "lat": 19.2273, "lon": 73.1138,
     "kind": "city", "min_zoom": 8.3, "max_zoom": 9.5},
    {"name": "Navi Mumbai", "lat": 19.0308, "lon": 73.0199,
     "kind": "city", "min_zoom": 8.2, "max_zoom": 9.55},
    {"name": "Panvel", "lat": 18.9895, "lon": 73.1222,
     "kind": "city", "min_zoom": 8.3},
    {"name": "Uran", "lat": 18.8808, "lon": 72.9386,
     "kind": "town", "min_zoom": 8.7},
    {"name": "Karjat", "lat": 18.9128, "lon": 73.3228,
     "kind": "town", "min_zoom": 8.7},
    {"name": "Khopoli", "lat": 18.7877, "lon": 73.3438,
     "kind": "town", "min_zoom": 8.7},
    {"name": "Pen", "lat": 18.7354, "lon": 73.0868,
     "kind": "town", "min_zoom": 8.7},
    {"name": "Alibag", "lat": 18.6498, "lon": 72.8765,
     "kind": "town", "min_zoom": 8.7},
    # Secondary metropolitan places appear after one zoom step.
    {"name": "Kalyan", "lat": 19.2397, "lon": 73.1366,
     "kind": "town", "min_zoom": 9.5},
    {"name": "Dombivli", "lat": 19.2149, "lon": 73.0910,
     "kind": "town", "min_zoom": 9.5},
    {"name": "Ulhasnagar", "lat": 19.2236, "lon": 73.1672,
     "kind": "town", "min_zoom": 9.4},
    {"name": "Ambernath", "lat": 19.2016, "lon": 73.2005,
     "kind": "town", "min_zoom": 9.4},
    {"name": "Badlapur", "lat": 19.1666, "lon": 73.2389,
     "kind": "town", "min_zoom": 9.4},
    {"name": "Matheran", "lat": 18.9902, "lon": 73.2700,
     "kind": "town", "min_zoom": 9.4},
    {"name": "Neral", "lat": 19.0266, "lon": 73.3181,
     "kind": "town", "min_zoom": 9.4},
    {"name": "Titwala", "lat": 19.2964, "lon": 73.2031,
     "kind": "town", "min_zoom": 9.4},
    {"name": "Murbad", "lat": 19.2571, "lon": 73.3907,
     "kind": "town", "min_zoom": 9.4},
    {"name": "Vasind", "lat": 19.4065, "lon": 73.2675,
     "kind": "town", "min_zoom": 9.4},
    # BMC localities: the established corridor names plus eastern/southern
    # anchors so the expanded municipal view is oriented across the whole city.
    {"name": "Dahisar", "lat": 19.2494, "lon": 72.8596,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Borivali", "lat": 19.2291, "lon": 72.8574,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Kandivali", "lat": 19.2041, "lon": 72.8517,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Malad", "lat": 19.1867, "lon": 72.8486,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Goregaon", "lat": 19.1649, "lon": 72.8495,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Jogeshwari", "lat": 19.1349, "lon": 72.8488,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Andheri", "lat": 19.1197, "lon": 72.8464,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Vile Parle", "lat": 19.0999, "lon": 72.8440,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Santacruz", "lat": 19.0842, "lon": 72.8410,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Khar", "lat": 19.0710, "lon": 72.8390,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Bandra", "lat": 19.0550, "lon": 72.8402,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Mulund", "lat": 19.1721, "lon": 72.9567,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Bhandup", "lat": 19.1428, "lon": 72.9377,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Powai", "lat": 19.1187, "lon": 72.9073,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Ghatkopar", "lat": 19.0857, "lon": 72.9084,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Kurla", "lat": 19.0653, "lon": 72.8794,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Chembur", "lat": 19.0548, "lon": 72.8980,
     "kind": "locality", "min_zoom": 9.8},
    {"name": "Sion", "lat": 19.0434, "lon": 72.8616,
     "kind": "locality", "min_zoom": 10.1},
    {"name": "Wadala", "lat": 19.0269, "lon": 72.8759,
     "kind": "locality", "min_zoom": 10.1},
    {"name": "Dadar", "lat": 19.0192, "lon": 72.8428,
     "kind": "locality", "min_zoom": 10.1},
    {"name": "Worli", "lat": 19.0117, "lon": 72.8179,
     "kind": "locality", "min_zoom": 10.1},
    {"name": "Byculla", "lat": 18.9766, "lon": 72.8331,
     "kind": "locality", "min_zoom": 10.1},
    {"name": "Fort", "lat": 18.9345, "lon": 72.8354,
     "kind": "locality", "min_zoom": 10.1},
    {"name": "Colaba", "lat": 18.9151, "lon": 72.8260,
     "kind": "locality", "min_zoom": 10.1},
    # Finer BMC context. These labels are intentionally held back until the
    # user moves closer than the municipal overview, so the established area
    # rail stays readable at the default BMC camera.
    {"name": "Aksa", "lat": 19.1786195, "lon": 72.7972052,
     "kind": "locality", "min_zoom": 10.85},
    {"name": "Antop Hill", "lat": 19.0239461, "lon": 72.8685474,
     "kind": "locality", "min_zoom": 10.85},
    {"name": "Bandra Kurla Complex", "lat": 19.0671150, "lon": 72.8657245,
     "kind": "locality", "min_zoom": 10.75},
    {"name": "Chandivali", "lat": 19.1091482, "lon": 72.8945793,
     "kind": "locality", "min_zoom": 10.75},
    {"name": "Charkop", "lat": 19.2141193, "lon": 72.8258652,
     "kind": "locality", "min_zoom": 10.75},
    {"name": "Churchgate", "lat": 18.9319334, "lon": 72.8271436,
     "kind": "locality", "min_zoom": 10.9},
    {"name": "Cuffe Parade", "lat": 18.9157866, "lon": 72.8189036,
     "kind": "locality", "min_zoom": 10.9},
    {"name": "Deonar", "lat": 19.0475502, "lon": 72.9051895,
     "kind": "locality", "min_zoom": 10.75},
    {"name": "Dharavi", "lat": 19.0444630, "lon": 72.8586177,
     "kind": "locality", "min_zoom": 10.6},
    {"name": "Girgaon", "lat": 18.9543165, "lon": 72.8179082,
     "kind": "locality", "min_zoom": 10.85},
    {"name": "Gorai", "lat": 19.2283752, "lon": 72.8258655,
     "kind": "locality", "min_zoom": 10.75},
    {"name": "Juhu", "lat": 19.1070215, "lon": 72.8275275,
     "kind": "locality", "min_zoom": 10.6},
    {"name": "Kalina", "lat": 19.0792730, "lon": 72.8612672,
     "kind": "locality", "min_zoom": 10.75},
    {"name": "Lalbaug", "lat": 18.9914237, "lon": 72.8364587,
     "kind": "locality", "min_zoom": 10.85},
    {"name": "Lokhandwala Complex", "lat": 19.1430985, "lon": 72.8246055,
     "kind": "locality", "min_zoom": 10.9},
    {"name": "Lower Parel", "lat": 19.0029881, "lon": 72.8303219,
     "kind": "locality", "min_zoom": 10.75},
    {"name": "Madh", "lat": 19.1375285, "lon": 72.7918857,
     "kind": "locality", "min_zoom": 10.85},
    {"name": "Mahim", "lat": 19.0423145, "lon": 72.8398344,
     "kind": "locality", "min_zoom": 10.6},
    {"name": "Malabar Hill", "lat": 18.9581616, "lon": 72.8033665,
     "kind": "locality", "min_zoom": 10.85},
    {"name": "Mankhurd", "lat": 19.0520835, "lon": 72.9339158,
     "kind": "locality", "min_zoom": 10.65},
    {"name": "Marve", "lat": 19.1946967, "lon": 72.7992026,
     "kind": "locality", "min_zoom": 10.85},
    {"name": "Mazgaon", "lat": 18.9697337, "lon": 72.8406201,
     "kind": "locality", "min_zoom": 10.85},
    {"name": "Nahur", "lat": 19.1591292, "lon": 72.9464400,
     "kind": "locality", "min_zoom": 10.75},
    {"name": "Nariman Point", "lat": 18.9270890, "lon": 72.8235859,
     "kind": "locality", "min_zoom": 10.9},
    {"name": "Oshiwara", "lat": 19.1502437, "lon": 72.8342294,
     "kind": "locality", "min_zoom": 10.75},
    {"name": "Parel", "lat": 19.0084268, "lon": 72.8425050,
     "kind": "locality", "min_zoom": 10.65},
    {"name": "Prabhadevi", "lat": 19.0148811, "lon": 72.8279556,
     "kind": "locality", "min_zoom": 10.65},
    {"name": "Saki Naka", "lat": 19.1082060, "lon": 72.8828404,
     "kind": "locality", "min_zoom": 10.65},
    {"name": "Sewri", "lat": 19.0014180, "lon": 72.8542575,
     "kind": "locality", "min_zoom": 10.75},
    {"name": "Tardeo", "lat": 18.9722351, "lon": 72.8203423,
     "kind": "locality", "min_zoom": 10.85},
    {"name": "Vakola", "lat": 19.0813635, "lon": 72.8538930,
     "kind": "locality", "min_zoom": 10.75},
    {"name": "Versova", "lat": 19.1309837, "lon": 72.8187345,
     "kind": "locality", "min_zoom": 10.6},
    # MMRDA-only towns and neighbourhoods. The metropolitan parent labels
    # hand off to these anchors around zoom 9.5 rather than competing with
    # them at the regional overview scale.
    {"name": "Virar", "lat": 19.4497996, "lon": 72.8120613,
     "kind": "town", "min_zoom": 9.45},
    {"name": "Vasai", "lat": 19.3855134, "lon": 72.8306748,
     "kind": "town", "min_zoom": 9.45},
    {"name": "Ghodbunder", "lat": 19.2882132, "lon": 72.8955745,
     "kind": "locality", "min_zoom": 9.65},
    {"name": "Mumbra", "lat": 19.1885360, "lon": 73.0215253,
     "kind": "locality", "min_zoom": 9.55},
    {"name": "Diva", "lat": 19.1869207, "lon": 73.0418444,
     "kind": "locality", "min_zoom": 9.65},
    {"name": "Shahad", "lat": 19.2458783, "lon": 73.1573918,
     "kind": "locality", "min_zoom": 9.65},
    {"name": "Digha", "lat": 19.1795780, "lon": 72.9964178,
     "kind": "locality", "min_zoom": 9.7},
    {"name": "Airoli", "lat": 19.1582719, "lon": 72.9967088,
     "kind": "locality", "min_zoom": 9.55},
    {"name": "Ghansoli", "lat": 19.1193307, "lon": 72.9995096,
     "kind": "locality", "min_zoom": 9.65},
    {"name": "Vashi", "lat": 19.0757840, "lon": 72.9952364,
     "kind": "locality", "min_zoom": 9.55},
    {"name": "Sanpada", "lat": 19.0607337, "lon": 73.0116775,
     "kind": "locality", "min_zoom": 9.65},
    {"name": "Juinagar", "lat": 19.0494859, "lon": 73.0158114,
     "kind": "locality", "min_zoom": 9.75},
    {"name": "Kharghar", "lat": 19.0525298, "lon": 73.0735111,
     "kind": "locality", "min_zoom": 9.55},
    {"name": "Kalamboli", "lat": 19.0358309, "lon": 73.1034796,
     "kind": "locality", "min_zoom": 9.65},
    {"name": "Taloja", "lat": 19.0754826, "lon": 73.0921912,
     "kind": "locality", "min_zoom": 9.65},
    {"name": "Ulwe", "lat": 18.9707794, "lon": 73.0215336,
     "kind": "locality", "min_zoom": 9.65},
    {"name": "Jasai", "lat": 18.9317805, "lon": 73.0188301,
     "kind": "locality", "min_zoom": 9.75},
    {"name": "Rasayani", "lat": 18.8978821, "lon": 73.1803608,
     "kind": "town", "min_zoom": 9.55},
    {"name": "Chowk", "lat": 18.9005462, "lon": 73.2397866,
     "kind": "locality", "min_zoom": 9.7},
    {"name": "Apta", "lat": 18.8540736, "lon": 73.1283053,
     "kind": "locality", "min_zoom": 9.7},
    {"name": "Vangani", "lat": 19.0891300, "lon": 73.2928848,
     "kind": "locality", "min_zoom": 9.65},
    {"name": "Kashimira", "lat": 19.2755399, "lon": 72.8841851,
     "kind": "locality", "min_zoom": 9.65},
)

# Pixel-space placement keeps direct labels outside the densest road ink. This
# follows the established WEH treatment: western localities form a left rail,
# eastern localities a right rail, and metropolitan labels are nudged away from
# the Thane/Kalyan/Navi Mumbai cluster. Anchor values are deck.gl TextLayer
# semantics (start/end relative to the geographic point plus pixel offset).
AREA_LABEL_LAYOUT: dict[str, tuple[list[int], str]] = {
    "Mumbai": ([-38, 10], "end"),
    "Vasai-Virar": ([-10, -10], "end"),
    "Mira-Bhayandar": ([-24, 2], "end"),
    "Thane": ([20, -18], "start"),
    "Bhiwandi": ([18, -12], "start"),
    "Kalyan-Dombivli": ([22, 12], "start"),
    "Navi Mumbai": ([22, 10], "start"),
    "Panvel": ([18, 10], "start"),
    "Uran": ([-16, 8], "end"),
    "Karjat": ([16, -6], "start"),
    "Khopoli": ([16, 8], "start"),
    "Pen": ([-12, 8], "end"),
    "Alibag": ([-12, 4], "end"),
    "Kalyan": ([18, -8], "start"),
    "Dombivli": ([-18, 8], "end"),
    "Ulhasnagar": ([18, -3], "start"),
    "Ambernath": ([18, 5], "start"),
    "Badlapur": ([18, 8], "start"),
    "Matheran": ([16, 8], "start"),
    "Neral": ([16, -5], "start"),
    "Titwala": ([16, -8], "start"),
    "Dahisar": ([-34, -2], "end"),
    "Borivali": ([-34, 0], "end"),
    "Kandivali": ([-34, 0], "end"),
    "Malad": ([-34, 0], "end"),
    "Goregaon": ([-34, 0], "end"),
    "Jogeshwari": ([-34, 0], "end"),
    "Andheri": ([-34, -2], "end"),
    "Vile Parle": ([-34, 0], "end"),
    "Santacruz": ([-34, -2], "end"),
    "Khar": ([-34, 0], "end"),
    "Bandra": ([-34, 3], "end"),
    "Mulund": ([44, -9], "start"),
    "Bhandup": ([44, 8], "start"),
    "Powai": ([44, -6], "start"),
    "Ghatkopar": ([44, 6], "start"),
    "Kurla": ([44, -6], "start"),
    "Chembur": ([44, 8], "start"),
    "Sion": ([-38, -8], "end"),
    "Wadala": ([38, -6], "start"),
    "Dadar": ([-38, 5], "end"),
    "Worli": ([-38, 12], "end"),
    "Byculla": ([38, -3], "start"),
    "Fort": ([38, 6], "start"),
    "Colaba": ([38, 14], "start"),
}


class CoverageError(RuntimeError):
    """Raised when source geometry cannot satisfy the coverage contract."""


def _require_geospatial():
    """Import the heavy optional stack only when coverage work is requested."""
    try:
        import osmnx as ox
        from shapely.geometry import LineString, Point, mapping, shape
        from shapely.ops import unary_union
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise CoverageError(
            "Coverage generation requires the project geospatial dependencies. "
            "Install requirements.txt (osmnx and shapely) first."
        ) from exc
    return ox, LineString, Point, mapping, shape, unary_union


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _flatten(value: Any) -> list[Any]:
    """Return an OSM/GraphML scalar-or-list attribute as a flat value list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v not in (None, "")]
    # OSMnx normally restores list types when loading GraphML.  This fallback
    # also supports GraphML readers that leave Python-list strings untouched.
    if isinstance(value, str) and value[:1] in "[(" and value[-1:] in ")]":
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, (list, tuple, set)):
            return [v for v in parsed if v not in (None, "")]
    return [value]


def _first(value: Any, default: str = "") -> str:
    values = _flatten(value)
    return str(values[0]).strip() if values else default


def _base_highway(value: Any) -> str:
    """Return the highest-order supported class carried by an OSM edge."""
    bases = {str(v).strip().lower().removesuffix("_link") for v in _flatten(value)}
    for road_class in MAJOR_HIGHWAYS:
        if road_class in bases:
            return road_class
    return "other"


def _road_names(data: Mapping[str, Any]) -> set[str]:
    names = {str(v).strip() for v in _flatten(data.get("name")) if str(v).strip()}
    if not names:
        names = {str(v).strip() for v in _flatten(data.get("ref")) if str(v).strip()}
    return names


def _way_ids(data: Mapping[str, Any]) -> set[str]:
    return {str(v).strip() for v in _flatten(data.get("osmid")) if str(v).strip()}


def _node_sort_key(value: Any) -> tuple[int, Any]:
    try:
        return 0, int(value)
    except (TypeError, ValueError):
        return 1, str(value)


def _edge_path(graph, u: Any, v: Any, data: Mapping[str, Any]) -> list[list[float]]:
    """Return an edge polyline as [lon, lat], oriented u to v."""
    geometry = data.get("geometry")
    coordinates = None
    if geometry is not None:
        try:
            coordinates = list(geometry.coords)
        except (AttributeError, NotImplementedError):
            # OSMnx edges are normally LineStrings.  Tolerate a fixture or
            # imported cache carrying a MultiLineString by joining its parts.
            parts = getattr(geometry, "geoms", ())
            joined: list[tuple[float, float]] = []
            for part in parts:
                part_coordinates = list(part.coords)
                if joined and part_coordinates and joined[-1] == part_coordinates[0]:
                    part_coordinates = part_coordinates[1:]
                joined.extend(part_coordinates)
            coordinates = joined or None
    if coordinates:
        points = [[round(float(x), 6), round(float(y), 6)] for x, y in coordinates]
    else:
        points = [
            [round(float(graph.nodes[u]["x"]), 6), round(float(graph.nodes[u]["y"]), 6)],
            [round(float(graph.nodes[v]["x"]), 6), round(float(graph.nodes[v]["y"]), 6)],
        ]
    if len(points) < 2:
        return []

    ux, uy = float(graph.nodes[u]["x"]), float(graph.nodes[u]["y"])
    if ((points[0][0] - ux) ** 2 + (points[0][1] - uy) ** 2) > (
        (points[-1][0] - ux) ** 2 + (points[-1][1] - uy) ** 2
    ):
        points.reverse()
    return points


def _canonical_path(path: Sequence[Sequence[float]]) -> tuple[tuple[float, float], ...]:
    forward = tuple((round(float(p[0]), 6), round(float(p[1]), 6)) for p in path)
    reverse = tuple(reversed(forward))
    return min(forward, reverse)


def _stable_link_id(data: Mapping[str, Any], path: Sequence[Sequence[float]]) -> str:
    signature = "|".join(
        [
            ",".join(sorted(_way_ids(data))),
            _base_highway(data.get("highway")),
            repr(_canonical_path(path)),
        ]
    )
    return "osm-link-" + hashlib.sha1(signature.encode("utf-8")).hexdigest()[:14]


def _bounds_dict(geometry) -> dict[str, float]:
    west, south, east, north = geometry.bounds
    return {
        "west": round(float(west), 6),
        "south": round(float(south), 6),
        "east": round(float(east), 6),
        "north": round(float(north), 6),
    }


def _view_for_bounds(bounds: Mapping[str, float]) -> dict[str, float | int]:
    width = max(float(bounds["east"]) - float(bounds["west"]), 1e-6)
    height = max(float(bounds["north"]) - float(bounds["south"]), 1e-6)
    span = max(width, height)
    # A deterministic initial Web Mercator-like zoom estimate.  The UI can
    # still fit exact bounds after it mounts.
    # The map shares the viewport with a 384px analysis panel, so a world-width
    # estimate with an extra subtraction leaves the selected region visually
    # undersized. This step fits BMC/MMRDA to the remaining map canvas.
    zoom = max(7, min(13, int(round(math.log2(360.0 / span)))))
    return {
        "longitude": round((float(bounds["west"]) + float(bounds["east"])) / 2.0, 6),
        "latitude": round((float(bounds["south"]) + float(bounds["north"])) / 2.0, 6),
        "zoom": zoom,
    }


def _feature(scope: str, geometry, relation_ids: Sequence[int], mapping) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "scope": scope,
            "osm_relation_ids": list(relation_ids),
            "source": "OpenStreetMap",
        },
        "geometry": mapping(geometry),
    }


def _load_boundary_cache(path: Path = BOUNDARY_CACHE):
    _, _, _, _, shape, unary_union = _require_geospatial()
    if not path.exists():
        raise FileNotFoundError(
            f"Missing boundary cache: {path}. Run `python -m src.network.coverage --download` first."
        )
    with path.open(encoding="utf-8") as handle:
        source = json.load(handle)

    geometries: dict[str, Any] = {}
    for feature in source.get("features", []):
        scope = feature.get("properties", {}).get("scope")
        if scope in {"bmc", "mmrda"} and feature.get("geometry"):
            geometries[scope] = shape(feature["geometry"])
    if set(geometries) != {"bmc", "mmrda"}:
        raise CoverageError(f"Boundary cache {path} must contain bmc and mmrda features")

    # Re-apply the invariant even for an older or hand-edited cache.
    geometries["mmrda"] = unary_union([geometries["mmrda"], geometries["bmc"]])
    if not geometries["mmrda"].covers(geometries["bmc"]):
        raise CoverageError("MMRDA boundary does not contain the complete BMC boundary")
    return geometries["bmc"], geometries["mmrda"], source.get("metadata", {})


def _download_boundaries(path: Path = BOUNDARY_CACHE):
    ox, _, _, mapping, _, unary_union = _require_geospatial()

    def relation_geometry(relation_id: int):
        frame = ox.geocode_to_gdf(f"R{relation_id}", by_osmid=True)
        if frame.empty:
            raise CoverageError(f"OSM relation R{relation_id} returned no boundary geometry")
        geometry = frame.geometry.iloc[0]
        if geometry is None or geometry.is_empty:
            raise CoverageError(f"OSM relation R{relation_id} has empty boundary geometry")
        return geometry

    mmrda_source = relation_geometry(OSM_RELATIONS["mmrda"])
    bmc = unary_union(
        [
            relation_geometry(OSM_RELATIONS["mumbai_city"]),
            relation_geometry(OSM_RELATIONS["mumbai_suburban"]),
        ]
    )
    # Guarantee BMC ⊆ MMRDA despite tiny source-boundary slivers or invalid
    # topology.  The source relation IDs remain explicit in the cache metadata.
    mmrda = unary_union([mmrda_source, bmc])
    if not mmrda.covers(bmc):
        raise CoverageError("Unable to construct an MMRDA geometry containing all of BMC")

    fetched_at = _utc_now()
    payload = {
        "type": "FeatureCollection",
        "metadata": {
            "fetched_at_utc": fetched_at,
            "source": "OpenStreetMap",
            "attribution": OSM_ATTRIBUTION,
            "copyright_url": OSM_COPYRIGHT_URL,
            "mmrda_containment": "effective MMRDA geometry is OSM R13312356 union BMC",
        },
        "features": [
            _feature(
                "bmc",
                bmc,
                [OSM_RELATIONS["mumbai_city"], OSM_RELATIONS["mumbai_suburban"]],
                mapping,
            ),
            _feature("mmrda", mmrda, [OSM_RELATIONS["mmrda"]], mapping),
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    return bmc, mmrda, payload["metadata"]


def download_osm_cache(
    graph_path: Path = GRAPH_CACHE,
    boundary_path: Path = BOUNDARY_CACHE,
):
    """Fetch the current OSM boundaries and major-road network into raw cache."""
    ox, _, _, _, _, _ = _require_geospatial()
    bmc, mmrda, boundary_metadata = _download_boundaries(boundary_path)
    del bmc  # classification happens from the persisted, reproducible cache

    graph = ox.graph_from_polygon(
        mmrda,
        simplify=True,
        retain_all=True,
        truncate_by_edge=True,
        custom_filter=MAJOR_ROAD_FILTER,
    )
    if graph.number_of_nodes() == 0 or graph.number_of_edges() == 0:
        raise CoverageError("OSM returned an empty MMRDA major-road graph")

    graph.graph["coverage_scope"] = "mmrda"
    graph.graph["coverage_filter"] = MAJOR_ROAD_FILTER
    graph.graph["coverage_fetched_at_utc"] = boundary_metadata["fetched_at_utc"]
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(graph, graph_path)
    return graph


def load_cached_graph(path: Path = GRAPH_CACHE):
    ox, _, _, _, _, _ = _require_geospatial()
    if not path.exists():
        raise FileNotFoundError(
            f"Missing OSM major-road cache: {path}. "
            "Run `python -m src.network.coverage --download` first."
        )
    graph = ox.load_graphml(path)
    if graph.number_of_nodes() == 0 or graph.number_of_edges() == 0:
        raise CoverageError(f"Cached OSM graph {path} is empty")
    return graph


def _iter_major_physical_edges(graph) -> Iterable[tuple[Any, Any, Mapping[str, Any], list[list[float]]]]:
    """Yield each physical OSM centreline once, collapsing reciprocal edges."""
    seen: set[tuple[tuple[float, float], ...]] = set()
    for u, v, _key, data in graph.edges(keys=True, data=True):
        if _base_highway(data.get("highway")) not in MAJOR_HIGHWAYS:
            continue
        path = _edge_path(graph, u, v, data)
        if len(path) < 2:
            continue
        signature = _canonical_path(path)
        if signature in seen:
            continue
        seen.add(signature)
        yield u, v, data, path


def _build_links(graph, bmc_polygon, mmrda_polygon) -> list[dict[str, Any]]:
    _, LineString, _, _, _, _ = _require_geospatial()
    links: list[dict[str, Any]] = []
    for _u, _v, data, path in _iter_major_physical_edges(graph):
        line = LineString(path)
        if line.is_empty or not line.intersects(mmrda_polygon):
            continue
        road_class = _base_highway(data.get("highway"))
        in_bmc = bool(line.intersects(bmc_polygon))
        scopes = ["mmrda"]
        if in_bmc:
            scopes.append("bmc")
        links.append(
            {
                "id": _stable_link_id(data, path),
                "p": path,
                "cls": ROAD_CLASSES.index(road_class),
                "name": _first(data.get("name")) or _first(data.get("ref")),
                "scopes": scopes,
                "in_bmc": in_bmc,
            }
        )
    links.sort(key=lambda item: item["id"])
    return links


def _haversine_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    earth_radius_m = 6_371_008.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * earth_radius_m * math.asin(min(1.0, math.sqrt(h)))


class _DisjointSet:
    def __init__(self, values: Iterable[Any]):
        self.parent = {value: value for value in values}

    def find(self, value: Any):
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: Any, right: Any) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        if _node_sort_key(a) <= _node_sort_key(b):
            self.parent[b] = a
        else:
            self.parent[a] = b


def _node_incidence(graph):
    neighbors: dict[Any, set[Any]] = defaultdict(set)
    facts: dict[Any, list[tuple[Any, Mapping[str, Any]]]] = defaultdict(list)
    # MultiDiGraph contains reciprocal directed edges.  Sets later remove their
    # effect on branch, way and road counts.
    for u, v, _key, data in graph.edges(keys=True, data=True):
        if _base_highway(data.get("highway")) not in MAJOR_HIGHWAYS:
            continue
        neighbors[u].add(v)
        neighbors[v].add(u)
        facts[u].append((v, data))
        facts[v].append((u, data))
    return neighbors, facts


def _candidate_nodes(graph, neighbors, facts) -> list[Any]:
    candidates: list[Any] = []
    for node, adjacent in neighbors.items():
        if len(adjacent) < 3:
            continue
        names: set[str] = set()
        ways: set[str] = set()
        classes: set[str] = set()
        for _other, data in facts[node]:
            names.update(_road_names(data))
            ways.update(_way_ids(data))
            classes.add(_base_highway(data.get("highway")))
        # A degree-three node on a single simplified way is generally a ramp
        # split or topology artefact.  Keep it only when there is evidence of a
        # second named road, OSM way, or highway class; clustering performs a
        # stricter physical-junction check below.
        if len(names) >= 2 or len(ways) >= 2 or len(classes) >= 2:
            candidates.append(node)
    return candidates


def _cluster_candidates(graph, candidates: Sequence[Any], facts, radius_m: float) -> list[list[Any]]:
    if not candidates:
        return []
    dsu = _DisjointSet(candidates)
    cell_degrees = radius_m / 111_000.0
    cells: dict[tuple[int, int], list[Any]] = defaultdict(list)
    identities: dict[Any, set[str]] = {}

    for node in candidates:
        lat = float(graph.nodes[node]["y"])
        lon = float(graph.nodes[node]["x"])
        names: set[str] = set()
        ways: set[str] = set()
        for _other, data in facts[node]:
            names.update("name:" + n.casefold() for n in _road_names(data))
            ways.update("way:" + way for way in _way_ids(data))
        identities[node] = names | ways
        cell = (math.floor(lat / cell_degrees), math.floor(lon / cell_degrees))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for other in cells.get((cell[0] + dy, cell[1] + dx), []):
                    # Nearby unrelated urban intersections remain separate.
                    if not (identities[node] & identities[other]):
                        continue
                    other_lat = float(graph.nodes[other]["y"])
                    other_lon = float(graph.nodes[other]["x"])
                    if _haversine_m(lat, lon, other_lat, other_lon) <= radius_m:
                        dsu.union(node, other)
        cells[cell].append(node)

    groups: dict[Any, list[Any]] = defaultdict(list)
    for node in candidates:
        groups[dsu.find(node)].append(node)
    return [sorted(group, key=_node_sort_key) for group in groups.values()]


def _junction_from_cluster(graph, members: Sequence[Any], facts, bmc_polygon):
    _, _, Point, _, _, _ = _require_geospatial()
    member_set = set(members)
    external_neighbors: set[Any] = set()
    names: set[str] = set()
    ways: set[str] = set()
    classes: set[str] = set()

    for node in members:
        for other, data in facts[node]:
            if other not in member_set:
                external_neighbors.add(other)
            names.update(_road_names(data))
            ways.update(_way_ids(data))
            classes.add(_base_highway(data.get("highway")))

    multiple_road_evidence = len(names) >= 2 or len(ways) >= 2 or len(classes) >= 2
    if len(external_neighbors) < 3 or not multiple_road_evidence:
        return None

    lat = sum(float(graph.nodes[node]["y"]) for node in members) / len(members)
    lon = sum(float(graph.nodes[node]["x"]) for node in members) / len(members)
    # A consolidated interchange can straddle a jurisdiction line.  Including
    # it in BMC when any of its real OSM nodes is covered avoids dropping a BMC
    # junction merely because the arithmetic centroid falls a few metres out.
    in_bmc = any(
        bmc_polygon.covers(
            Point(float(graph.nodes[node]["x"]), float(graph.nodes[node]["y"]))
        )
        for node in members
    )
    ordered_roads = sorted(names, key=str.casefold)
    ordered_classes = sorted(classes, key=ROAD_CLASSES.index)
    anchor = min(members, key=_node_sort_key)
    anchor_text = str(anchor).removeprefix("osm-node-")
    name = " × ".join(ordered_roads[:3])
    if not name:
        name = f"OSM junction {anchor_text}"

    scopes = ["mmrda"]
    if in_bmc:
        scopes.append("bmc")
    return {
        "id": f"osm-junction-{anchor_text}",
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "name": name,
        "roads": ordered_roads,
        "classes": ordered_classes,
        "in_bmc": in_bmc,
        "scopes": scopes,
        "status": "awaiting_collection",
    }


def _build_junctions(
    graph,
    bmc_polygon,
    cluster_radius_m: float = DEFAULT_CLUSTER_RADIUS_M,
) -> list[dict[str, Any]]:
    neighbors, facts = _node_incidence(graph)
    candidates = _candidate_nodes(graph, neighbors, facts)
    clusters = _cluster_candidates(graph, candidates, facts, cluster_radius_m)
    junctions = [
        junction
        for members in clusters
        if (junction := _junction_from_cluster(graph, members, facts, bmc_polygon)) is not None
    ]
    junctions.sort(key=lambda item: item["id"])
    return junctions


def _build_area_labels(bmc_polygon, mmrda_polygon) -> list[dict[str, Any]]:
    """Build scoped cartographic labels from curated OSM place anchors."""
    _, _, Point, _, _, _ = _require_geospatial()
    labels: list[dict[str, Any]] = []
    for index, anchor in enumerate(AREA_LABEL_ANCHORS):
        lat, lon = float(anchor["lat"]), float(anchor["lon"])
        point = Point(lon, lat)
        # A place anchor just outside the effective source relation is not part
        # of this coverage view, even if it lies inside the relation's bbox.
        if not mmrda_polygon.covers(point):
            continue
        in_bmc = bool(bmc_polygon.covers(point))
        scopes = ["mmrda"]
        if in_bmc:
            scopes.append("bmc")
        label = {
            "id": f"area-label-{index:02d}",
            "name": str(anchor["name"]),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "kind": str(anchor["kind"]),
            "min_zoom": float(anchor["min_zoom"]),
            "in_bmc": in_bmc,
            "scopes": scopes,
        }
        if anchor.get("max_zoom") is not None:
            label["max_zoom"] = float(anchor["max_zoom"])
        pixel_offset, text_anchor = AREA_LABEL_LAYOUT.get(
            label["name"], ([0, 0], "middle")
        )
        label["pixel_offset"] = pixel_offset
        label["text_anchor"] = text_anchor
        labels.append(label)
    return labels


def _scope_record(
    label: str,
    geometry,
    links: Sequence[Mapping[str, Any]],
    junctions: Sequence[Mapping[str, Any]],
    scope: str,
) -> dict[str, Any]:
    scope_links = sum(scope in item["scopes"] for item in links)
    scope_junctions = sum(scope in item["scopes"] for item in junctions)
    bounds = _bounds_dict(geometry)
    short_label = "BMC" if scope == "bmc" else "MMRDA"
    description = (
        "Municipal Greater Mumbai · a subset of the MMRDA planning area"
        if scope == "bmc"
        else "Metropolitan planning area · includes every BMC junction"
    )
    return {
        "label": label,
        "short_label": short_label,
        "description": description,
        "bounds": bounds,
        "view": _view_for_bounds(bounds),
        "counts": {
            "links": scope_links,
            "junctions": scope_junctions,
            "awaiting_collection": scope_junctions,
        },
    }


def _validate_payload(payload: Mapping[str, Any]) -> None:
    """Enforce the no-synthetic-data and strict-scope-nesting contracts."""
    links = payload.get("links", [])
    junctions = payload.get("junctions", [])
    forbidden_traffic_fields = {
        "flow", "vc", "cap", "capacity", "speed", "current_speed_kph",
        "free_speed_kph", "tti", "delay", "queue", "volume",
    }

    for link in links:
        if "mmrda" not in link.get("scopes", []):
            raise CoverageError(f"Link {link.get('id')} is outside the MMRDA parent scope")
        if link.get("in_bmc") != ("bmc" in link.get("scopes", [])):
            raise CoverageError(f"Link {link.get('id')} has inconsistent BMC membership")
        if not 0 <= int(link.get("cls", -1)) <= 4:
            raise CoverageError(f"Link {link.get('id')} has invalid class index")
        if forbidden_traffic_fields.intersection(link):
            raise CoverageError(f"Coverage link {link.get('id')} contains traffic data")

    bmc_ids: set[str] = set()
    mmrda_ids: set[str] = set()
    for junction in junctions:
        if junction.get("status") != "awaiting_collection":
            raise CoverageError(f"New junction {junction.get('id')} is not awaiting collection")
        if "latest" in junction and junction["latest"] is not None:
            raise CoverageError(f"New junction {junction.get('id')} contains a synthetic latest reading")
        if forbidden_traffic_fields.intersection(junction):
            raise CoverageError(f"Coverage junction {junction.get('id')} contains traffic data")
        scopes = set(junction.get("scopes", []))
        if "mmrda" not in scopes:
            raise CoverageError(f"Junction {junction.get('id')} is outside the MMRDA parent scope")
        junction_id = str(junction["id"])
        mmrda_ids.add(junction_id)
        if junction.get("in_bmc"):
            if "bmc" not in scopes:
                raise CoverageError(f"Junction {junction_id} has inconsistent BMC membership")
            bmc_ids.add(junction_id)
        elif "bmc" in scopes:
            raise CoverageError(f"Junction {junction_id} has inconsistent BMC membership")

    if not bmc_ids.issubset(mmrda_ids):
        raise CoverageError("BMC junctions are not a subset of MMRDA junctions")
    if bmc_ids == mmrda_ids:
        raise CoverageError(
            "MMRDA must be a strict superset of BMC, but the extract has no junction outside BMC"
        )

    label_names: set[str] = set()
    for label in payload.get("area_labels", []):
        name = str(label.get("name", "")).strip()
        if not name or name.casefold() in label_names:
            raise CoverageError(f"Area label has a missing or duplicate name: {name!r}")
        label_names.add(name.casefold())
        scopes_for_label = set(label.get("scopes", []))
        if "mmrda" not in scopes_for_label:
            raise CoverageError(f"Area label {name} is outside the MMRDA parent scope")
        if label.get("in_bmc") != ("bmc" in scopes_for_label):
            raise CoverageError(f"Area label {name} has inconsistent BMC membership")
        if not math.isfinite(float(label.get("min_zoom", float("nan")))):
            raise CoverageError(f"Area label {name} has an invalid minimum zoom")
        if forbidden_traffic_fields.intersection(label):
            raise CoverageError(f"Area label {name} contains traffic data")

    scopes = payload.get("scopes", {})
    expected = {
        "bmc": len(bmc_ids),
        "mmrda": len(mmrda_ids),
    }
    for scope, count in expected.items():
        reported = scopes.get(scope, {}).get("counts", {}).get("junctions")
        if reported != count:
            raise CoverageError(f"{scope} junction count is {reported}; expected {count}")


def build_coverage_payload(
    graph=None,
    *,
    graph_path: Path = GRAPH_CACHE,
    boundary_path: Path = BOUNDARY_CACHE,
    cluster_radius_m: float = DEFAULT_CLUSTER_RADIUS_M,
) -> dict[str, Any]:
    """Build a geometry-only BMC/MMRDA payload from cached OSM source data."""
    if cluster_radius_m <= 0:
        raise ValueError("cluster_radius_m must be positive")
    if graph is None:
        graph = load_cached_graph(graph_path)
    bmc, mmrda, boundary_metadata = _load_boundary_cache(boundary_path)

    links = _build_links(graph, bmc, mmrda)
    junctions = _build_junctions(graph, bmc, cluster_radius_m)
    area_labels = _build_area_labels(bmc, mmrda)
    fetched_at = (
        graph.graph.get("coverage_fetched_at_utc")
        or boundary_metadata.get("fetched_at_utc")
    )
    payload: dict[str, Any] = {
        "metadata": {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": _utc_now(),
            "source_fetched_at_utc": fetched_at,
            "geometry_source": "OpenStreetMap",
            "attribution": OSM_ATTRIBUTION,
            "copyright_url": OSM_COPYRIGHT_URL,
            "traffic_data": "none; junctions are collection targets",
            "road_filter": list(MAJOR_HIGHWAY_TAGS),
            "junction_method": {
                "minimum_external_branches": 3,
                "minimum_road_identities": 2,
                "cluster_radius_m": cluster_radius_m,
            },
            "area_label_source": "OpenStreetMap/Nominatim place and locality centres",
        },
        "classes": ROAD_CLASSES,
        "scopes": {
            "bmc": _scope_record(
                "BMC · Greater Mumbai",
                bmc,
                links,
                junctions,
                "bmc",
            ),
            "mmrda": _scope_record(
                "MMRDA · Mumbai Metropolitan Region",
                mmrda,
                links,
                junctions,
                "mmrda",
            ),
        },
        "links": links,
        "junctions": junctions,
        "area_labels": area_labels,
    }
    _validate_payload(payload)
    return payload


def write_coverage(payload: Mapping[str, Any], output_path: Path = COVERAGE_JSON) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build nested BMC/MMRDA major-road coverage from real OSM geometry."
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Fetch/update the OSM boundaries and major-road GraphML cache before building.",
    )
    parser.add_argument("--graph-cache", type=Path, default=GRAPH_CACHE)
    parser.add_argument("--boundary-cache", type=Path, default=BOUNDARY_CACHE)
    parser.add_argument("--output", type=Path, default=COVERAGE_JSON)
    parser.add_argument(
        "--cluster-radius-m",
        type=float,
        default=DEFAULT_CLUSTER_RADIUS_M,
        help="Maximum distance for consolidating carriageway/ramp topology nodes (default: 90).",
    )
    args = parser.parse_args(argv)

    graph = None
    if args.download:
        graph = download_osm_cache(args.graph_cache, args.boundary_cache)
    payload = build_coverage_payload(
        graph,
        graph_path=args.graph_cache,
        boundary_path=args.boundary_cache,
        cluster_radius_m=args.cluster_radius_m,
    )
    output = write_coverage(payload, args.output)
    bmc = payload["scopes"]["bmc"]["counts"]
    mmrda = payload["scopes"]["mmrda"]["counts"]
    print(f"Wrote {output}")
    print(f"BMC: {bmc['junctions']} junctions, {bmc['links']} links")
    print(f"MMRDA: {mmrda['junctions']} junctions, {mmrda['links']} links")
    print("Traffic values created: 0 (all junctions await real collection)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
