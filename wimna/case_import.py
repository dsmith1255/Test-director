"""Strict, read-only extraction from the authoritative Update6 .xlsx workbook.

Values stay tied to workbook cells and source hash. Formula cells require cached
results; this importer never substitutes figures from an older workbook.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .discovery import sha256_file

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

CELL_GROUPS = {
    "04.A": ["B12", "D12", "G12", "H12", "I12", "D11", "D9"],
    "04.C": [*(f"D{row}" for row in range(7, 15)),
             *(f"G{row}" for row in range(7, 15)),
             *(f"I{row}" for row in range(7, 15)),
             *(f"B{row}" for row in range(16, 24)),
             *(f"C{row}" for row in range(16, 24))],
    "04.D": [f"{col}{row}" for row in range(7, 10) for col in "BCDE"],
    "04.E": [f"{col}{row}" for row in range(6, 9) for col in "CG"],
}


def _number_or_text(text: str | None):
    if text is None:
        return None
    try:
        return float(text) if any(char in text for char in ".eE") else int(text)
    except ValueError:
        return text


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in item.iter(f"{{{MAIN}}}t")) for item in root.findall(f"{{{MAIN}}}si")]


def _sheet_paths(archive: zipfile.ZipFile) -> dict[str, str]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {item.attrib["Id"]: item.attrib["Target"] for item in relationships.findall(f"{{{PKG}}}Relationship")}
    result = {}
    for sheet in workbook.findall(f".//{{{MAIN}}}sheet"):
        target = targets[sheet.attrib[f"{{{REL}}}id"]].lstrip("/")
        result[sheet.attrib["name"]] = target if target.startswith("xl/") else "xl/" + target
    return result


def _cells(archive: zipfile.ZipFile, path: str, shared: list[str]) -> dict[str, object]:
    root = ET.fromstring(archive.read(path))
    result: dict[str, object] = {}
    for cell in root.findall(f".//{{{MAIN}}}sheetData/{{{MAIN}}}row/{{{MAIN}}}c"):
        ref = cell.attrib["r"]
        kind = cell.attrib.get("t")
        value = cell.find(f"{{{MAIN}}}v")
        if kind == "inlineStr":
            content = "".join(node.text or "" for node in cell.iter(f"{{{MAIN}}}t"))
        elif value is None:
            content = None
        elif kind == "s":
            content = shared[int(value.text)]
        elif kind in ("str", "e"):
            content = value.text
        elif kind == "b":
            content = value.text == "1"
        else:
            content = _number_or_text(value.text)
        if cell.find(f"{{{MAIN}}}f") is not None and content is None:
            raise ValueError(f"formula has no cached result: {path}!{ref}")
        result[ref] = content
    return result


def _as_float(value, label: str) -> float:
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
        if match:
            return float(match.group())
    raise ValueError(f"expected numeric value at {label}, got {value!r}")


def import_update6(path: str | Path) -> dict:
    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(target)
    lower = target.name.casefold()
    if "forreview" in lower or "update6" not in lower or target.suffix.casefold() != ".xlsx":
        raise ValueError("supply the Update6 .xlsx workbook; ForReview and ambiguous files are rejected")
    with zipfile.ZipFile(target) as archive:
        sheets = _sheet_paths(archive)
        absent = set(CELL_GROUPS) - set(sheets)
        if absent:
            raise ValueError(f"Update6 workbook missing sheets: {sorted(absent)}")
        shared = _shared_strings(archive)
        extracted = {}
        for name, refs in CELL_GROUPS.items():
            all_cells = _cells(archive, sheets[name], shared)
            extracted[name] = {ref: all_cells.get(ref) for ref in refs}
    essential = {"04.A": ["B12", "D12", "G12", "H12", "I12", "D11", "D9"],
                 "04.C": [*(f"D{row}" for row in range(7, 15)),
                          *(f"G{row}" for row in range(7, 15)),
                          *(f"B{row}" for row in range(16, 24)),
                          *(f"C{row}" for row in range(16, 24))],
                 "04.E": [f"{col}{row}" for row in range(6, 9) for col in "CG"]}
    for sheet, refs in essential.items():
        for ref in refs:
            if extracted[sheet][ref] is None:
                raise ValueError(f"missing required Update6 cell {sheet}!{ref}")
    a = extracted["04.A"]
    c = extracted["04.C"]
    temperatures = [_as_float(c[f"D{row}"], f"04.C!D{row}") for row in range(7, 15)]
    if not all(0 <= temp <= 80 for temp in temperatures):
        raise ValueError("track temperature outside plausible C range; inspect workbook units")
    pace = [_as_float(c[f"G{row}"], f"04.C!G{row}") for row in range(7, 15)]
    finishes = [c[f"I{row}"] for row in range(7, 15)]
    def is_finish(value) -> bool:
        return ((isinstance(value, (int, float)) and not isinstance(value, bool)) or
                (isinstance(value, str) and re.fullmatch(r"P?\d+", value.strip(), re.IGNORECASE) is not None))
    if len([value for value in finishes if is_finish(value)]) != 5:
        raise ValueError("Update6 results must contain five numeric finishes across eight races")
    if not all(-10 < value < 10 for value in pace):
        raise ValueError("race pace values outside plausible s/lap range; inspect workbook units")
    if not any(extracted["04.D"][f"B{row}"] is not None for row in range(7, 10)):
        raise ValueError("Update6 retirement classification table is missing")
    expected_checks = {
        "04.A!D12 points": (_as_float(a["D12"], "04.A!D12"), 46.0, 1e-6),
        "04.A!G12 DNFs": (_as_float(a["G12"], "04.A!G12"), 3.0, 1e-6),
        "04.A!H12 average finish": (_as_float(a["H12"], "04.A!H12"), 5.4, 1e-4),
        "04.A!I12 race pace deficit": (_as_float(a["I12"], "04.A!I12"), 0.31125, 1e-5),
        "P5 points gap": (_as_float(a["D11"], "04.A!D11") - _as_float(a["D12"], "04.A!D12"), 22.0, 1e-6),
        "P3 points gap": (_as_float(a["D9"], "04.A!D9") - _as_float(a["D12"], "04.A!D12"), 73.0, 1e-6),
    }
    if str(a["B12"]).strip().casefold() not in {"p6", "6"}:
        raise ValueError("Update6 position does not match supplied P6 reference")
    for label, (observed, expected, tolerance) in expected_checks.items():
        if abs(observed - expected) > tolerance:
            raise ValueError(f"Update6 {label} differs from supplied specification; inspect workbook version")
    if temperatures != [31.0, 34.0, 27.0, 29.0, 36.0, 24.0, 30.0, 33.0]:
        raise ValueError("Update6 track temperatures differ from supplied specification")
    summary = {
        "position": a["B12"], "points": _as_float(a["D12"], "04.A!D12"),
        "dnfs": _as_float(a["G12"], "04.A!G12"),
        "average_finish": _as_float(a["H12"], "04.A!H12"),
        "mean_race_pace_deficit_s_per_lap": _as_float(a["I12"], "04.A!I12"),
        "gap_to_p5_points": _as_float(a["D11"], "04.A!D11") - _as_float(a["D12"], "04.A!D12"),
        "gap_to_p3_points": _as_float(a["D9"], "04.A!D9") - _as_float(a["D12"], "04.A!D12"),
        "historical_track_temperature_c": temperatures,
        "options": {name: {"expected_gain_raw": extracted["04.E"][f"C{row}"],
                           "introduction_raw": extracted["04.E"][f"G{row}"]}
                    for name, row in (("A", 6), ("B", 7), ("C", 8))},
    }
    return {"schema_version": 1, "authority": "Update6 workbook", "evidence": "CASE",
            "source_filename": target.name, "source_sha256": sha256_file(target),
            "cells": extracted, "summary": summary,
            "units": {"mean_race_pace_deficit_s_per_lap": "s/lap",
                      "historical_track_temperature_c": "deg C track"}}
