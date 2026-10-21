"""Package-owned polygon preparation; never grants engineering acceptance."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_regions(polygons, names: list[str], *, repair: str, basin_id: str) -> dict:
    """Prepare exact-name polygons, reporting topology changes without thresholds."""
    import shapely
    from pyproj import CRS
    from shapely.geometry import mapping

    if repair not in {"reject", "make_valid"}:
        raise ValueError("repair must be 'reject' or 'make_valid'")
    if not names or any(not isinstance(n, str) or not n.strip() for n in names):
        raise ValueError("Active subbasin names must be nonempty strings")
    if len(set(names)) != len(names):
        raise ValueError("Active subbasin names must be unique")
    if not basin_id or basin_id in names:
        raise ValueError("basin_id must be nonempty and distinct from subbasin names")
    if "name" not in polygons or polygons.crs is None:
        raise ValueError("Subbasin polygons must have names and a declared CRS")
    crs = CRS.from_user_input(polygons.crs)
    if not crs.is_projected or any(
        axis.unit_conversion_factor != 1 for axis in crs.axis_info
    ):
        raise ValueError("Subbasin area audit requires a projected metre CRS")
    selected = polygons.loc[polygons["name"].isin(names)].copy()
    missing = sorted(set(names) - set(selected["name"]))
    if missing:
        raise ValueError(f"Missing active subbasin polygons: {missing}")
    if selected["name"].duplicated().any():
        raise ValueError("Duplicate active subbasin polygon names")
    selected = selected.set_index("name")
    regions, audits, prepared = [], [], []
    for name in sorted(names):
        original = selected.loc[name].geometry
        if (
            original is None
            or original.is_empty
            or original.geom_type not in {"Polygon", "MultiPolygon"}
        ):
            raise ValueError(f"Subbasin {name!r} requires nonempty polygon geometry")
        valid = original.is_valid
        if not valid and repair == "reject":
            raise ValueError(
                f"Invalid subbasin {name!r}: {shapely.is_valid_reason(original)}"
            )
        polygon = shapely.make_valid(original) if not valid else original
        if (
            polygon.geom_type not in {"Polygon", "MultiPolygon"}
            or not polygon.is_valid
            or polygon.is_empty
        ):
            raise ValueError(
                f"Repair did not yield valid polygon-only geometry for {name!r}"
            )
        if not math.isfinite(polygon.area) or polygon.area <= 0:
            raise ValueError(f"Subbasin {name!r} has nonpositive or nonfinite area")
        geometry = mapping(polygon)
        geometry_hash = hashlib.sha256(
            canonical_bytes({"crs": crs.to_string(), "geometry": geometry})
        ).hexdigest()
        regions.append({"id": name, "kind": "subbasin", "geometry": geometry})
        prepared.append(polygon)
        audits.append(
            {
                "id": name,
                "source_valid": bool(valid),
                "source_validity_reason": shapely.is_valid_reason(original),
                "source_area_m2": original.area,
                "area_m2": polygon.area,
                "area_change_m2": polygon.area - original.area,
                "boundary_hausdorff_distance_m": (
                    0.0
                    if valid
                    else original.boundary.hausdorff_distance(polygon.boundary)
                ),
                "geometry_sha256": geometry_hash,
            }
        )
    basin = shapely.union_all(prepared)
    if not basin.is_valid or basin.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Subbasin union is not valid polygon geometry")
    regions.insert(0, {"id": basin_id, "kind": "basin", "geometry": mapping(basin)})
    return {
        "contract_version": "hms-commander/subbasin-regions/0.1.0",
        "crs": crs.to_string(),
        "repair": repair,
        "engineering_review": "required",
        "forecast_eligible": False,
        "regions": regions,
        "partition": {"parent": basin_id, "children": sorted(names)},
        "audit": {
            "selected_count": len(names),
            "excluded_count": len(polygons) - len(selected),
            "repaired_count": sum(not row["source_valid"] for row in audits),
            "basin_area_m2": basin.area,
            "sum_subbasin_area_m2": sum(p.area for p in prepared),
            "overlap_area_m2": max(0.0, sum(p.area for p in prepared) - basin.area),
            "subbasins": audits,
        },
    }


def export_regions(
    sqlite_path: Path, basin_path: Path, output: Path, *, repair: str, basin_id: str
) -> dict:
    from importlib.metadata import version

    from .HmsBasin import HmsBasin
    from .HmsSqlite import HmsSqlite

    if output.resolve() in {sqlite_path.resolve(), basin_path.resolve()}:
        raise ValueError("Output must not replace a model source")
    sources = {
        "sqlite": {"name": sqlite_path.name, "sha256": file_hash(sqlite_path)},
        "basin": {"name": basin_path.name, "sha256": file_hash(basin_path)},
    }
    names = HmsBasin.get_subbasins(basin_path)["name"].tolist()
    payload = prepare_regions(
        HmsSqlite.get_subbasins(sqlite_path), names, repair=repair, basin_id=basin_id
    )
    for key, path in (("sqlite", sqlite_path), ("basin", basin_path)):
        if file_hash(path) != sources[key]["sha256"]:
            raise ValueError(f"Source {key} changed during geometry export")
    payload["sources"] = sources
    payload["implementation"] = {
        "module_sha256": hashlib.sha256(
            Path(__file__).read_text(encoding="utf-8").encode()
        ).hexdigest(),
        "dependencies": {
            name: version(name) for name in ("geopandas", "shapely", "pyproj")
        },
    }
    payload["export_sha256"] = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    content = canonical_bytes(payload) + b"\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.read_bytes() != content:
            raise FileExistsError("Refusing to replace a different geometry export")
    else:
        with output.open("xb") as stream:
            stream.write(content)
    return payload
