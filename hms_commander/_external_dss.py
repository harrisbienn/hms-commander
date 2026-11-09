"""Read-only inventory and qualification of declared HMS external DSS inputs."""

import fnmatch
import re
from pathlib import Path

from ._parsing import HmsFileParser

_REFERENCE_KEYS = {
    "Filename",
    "DSS File Name",
    "DSS File",
    "Pathname",
    "DSS Pathname",
    "Data Source Type",
}


def _declarations(content):
    """Retain repeated source values before the generic parser collapses them."""
    result = {}
    for line in content.splitlines():
        if ":" in line:
            key, value = (part.strip() for part in line.split(":", 1))
            if key in _REFERENCE_KEYS:
                result.setdefault(key, []).append(value)
    return result


def reference_fields(attrs):
    """Resolve supported aliases without silently choosing conflicting values."""
    result = []
    for aliases in (
        ("Filename", "DSS File Name", "DSS File"),
        ("Pathname", "DSS Pathname"),
    ):
        values = {attrs[key].strip() for key in aliases if attrs.get(key, "").strip()}
        if len(values) > 1:
            raise ValueError(f"Conflicting external DSS aliases: {', '.join(aliases)}")
        result.append(next(iter(values), ""))
    return tuple(result)


def inventory(project_folder):
    """Inventory all declared gage/grid blocks, including non-selected inputs."""
    references = []
    for suffix, kind in ((".gage", "Gage"), (".grid", "Grid")):
        for path in sorted(Path(project_folder).glob("*" + suffix)):
            content = HmsFileParser.read_file(path)
            blocks = HmsFileParser.find_all_blocks(content, kind)
            for match, name, attrs in blocks:
                declarations = _declarations(match.group(3))
                declared = any(
                    key != "Data Source Type" for key in declarations
                ) or any(
                    "dss" in value.lower()
                    for value in declarations.get("Data Source Type", [])
                )
                if not declared:
                    continue
                record = {
                    "source_file": str(path),
                    "kind": kind.lower(),
                    "name": name,
                    "dss_file": "",
                    "pathname": "",
                    "declarations": declarations,
                    "issues": [],
                }
                try:
                    record["dss_file"], record["pathname"] = reference_fields(attrs)
                    for key, values in declarations.items():
                        if len(set(values)) > 1:
                            raise ValueError(
                                f"Conflicting repeated external DSS key: {key}"
                            )
                    if re.search(
                        rf"^{kind}:", match.group(3), re.MULTILINE | re.IGNORECASE
                    ):
                        raise ValueError(
                            "Malformed external-input block: another block starts before End:"
                        )
                    terminator_start = content.rfind("\n", 0, match.start(4)) + 1
                    if content[terminator_start : match.end(4)].strip() != "End:":
                        raise ValueError("Malformed external-input block terminator")
                    if (
                        len(re.findall(r"^\s*Variant:", match.group(3), re.MULTILINE))
                        > 1
                    ):
                        raise ValueError(
                            "Multiple external-input variants require explicit selection"
                        )
                except ValueError as error:
                    record["issues"].append(str(error))
                references.append(record)
            # A regex for complete blocks cannot establish that omitted source
            # text is harmless. Account for external declarations outside every
            # parsed span so an unterminated input cannot become all([]) == True.
            outside = []
            offset = 0
            for line in content.splitlines(keepends=True):
                if _declarations(line) and not any(
                    match.start() <= offset < match.end() for match, _, _ in blocks
                ):
                    outside.append(line)
                offset += len(line)
            outside_declarations = _declarations("".join(outside))
            if any(key != "Data Source Type" for key in outside_declarations) or any(
                "dss" in value.lower()
                for value in outside_declarations.get("Data Source Type", [])
            ):
                references.append(
                    {
                        "source_file": str(path),
                        "kind": kind.lower(),
                        "name": "<unparsed external input>",
                        "dss_file": "",
                        "pathname": "",
                        "declarations": outside_declarations,
                        "issues": [
                            "Malformed or unterminated external-input block; declarations were not parsed"
                        ],
                    }
                )
    return references


def _matched_paths(selector, catalog):
    if (
        not selector.startswith("/")
        or not selector.endswith("/")
        or selector.count("/") != 7
    ):
        raise ValueError("External DSS selector must have exactly six pathname parts")
    selected = selector[1:-1].upper().split("/")
    matches = []
    for pathname in catalog:
        parts = pathname[1:-1].upper().split("/")
        if len(parts) == 6 and all(
            (index in (3, 4) and pattern == "") or fnmatch.fnmatchcase(part, pattern)
            for index, (part, pattern) in enumerate(zip(parts, selected))
        ):
            matches.append(pathname)
    return sorted(set(matches))


def preflight(project_folder, read_records):
    from .dss.hms_dss import HmsDss

    references = inventory(project_folder)
    catalogs = {}
    for record in references:
        record["matched_pathnames"] = []
        record["records_read"] = 0
        record["status"] = "failed"
        try:
            if record["issues"]:
                continue
            if not record["dss_file"] or not record["pathname"]:
                raise ValueError("External DSS file and pathname must both be declared")
            dss_path = Path(record["dss_file"].replace("\\", "/"))
            if not dss_path.is_absolute():
                dss_path = Path(project_folder) / dss_path
            record["resolved_dss_file"] = str(dss_path.resolve())
            if not dss_path.is_file() or dss_path.stat().st_size == 0:
                raise ValueError("External DSS file is missing or empty")
            _matched_paths(record["pathname"], [])  # Validate even in inventory mode.
            if not read_records:
                record["status"] = "unverified"
                continue
            identity = str(dss_path.resolve())
            if identity not in catalogs:
                catalogs[identity] = HmsDss.get_catalog(dss_path)
            matches = _matched_paths(record["pathname"], catalogs[identity])
            record["matched_pathnames"] = matches
            if not matches:
                raise ValueError("External DSS selector matches no cataloged pathname")
            for pathname in matches:
                if record["kind"] == "grid":
                    from ras_commander.dss import RasDss

                    RasDss.read_grid(dss_path, pathname)
                else:
                    frame = HmsDss.read_timeseries(dss_path, pathname)
                    if frame.empty:
                        raise ValueError(
                            f"External DSS record has no values: {pathname}"
                        )
                record["records_read"] += 1
            record["status"] = "passed"
        except Exception as error:
            # An intentional per-reference boundary preserves every failing
            # identity while allowing the remaining input inventory to finish.
            record["issues"].append(f"{type(error).__name__}: {error}")
    return {
        "scope": "all-declared-gage-and-grid-inputs",
        "read_records": read_records,
        "passed": bool(read_records)
        and all(record["status"] == "passed" for record in references),
        "references": references,
    }
