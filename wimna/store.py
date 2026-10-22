"""Crash-safe local run/event store and idempotent CSP bridge ingestion."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import hashlib
import zlib
from pathlib import Path
from typing import Iterator

SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
COMPACT_CHUNK_V1 = "WIMNA_COMPACT_JSONL_V1"


def _json(value) -> str:
    def check(item):
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("nonfinite numbers must be stored as null with a quality flag")
        if isinstance(item, dict):
            for member in item.values():
                check(member)
        elif isinstance(item, (list, tuple)):
            for member in item:
                check(member)
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _pack_json(encoded: str) -> bytes:
    """SQLite stores the exact normalized JSON in a compressed BLOB.

    Existing releases stored text in these columns.  `_unpack_json` accepts
    both, so old stores and mixed stores remain readable after an upgrade.
    """
    return zlib.compress(encoded.encode("utf-8"), level=6)


def _unpack_json(value: str | bytes) -> str:
    if isinstance(value, str):
        return value
    try:
        return zlib.decompress(value).decode("utf-8")
    except (zlib.error, UnicodeDecodeError) as exc:
        raise ValueError("stored JSON BLOB is corrupt") from exc


def _compact_chunk_records(raw: bytes) -> list[tuple[int, dict]]:
    """Expand compact Lua samples or pass through older ordinary JSONL.

    The first line of a compact chunk is a channel dictionary.  Each following
    sample carries values, with quality written only when it differs from the
    explicit available/unavailable default.  Source and unit never have to be
    guessed from channel names.  Ordinary records can coexist in the chunk if
    a channel descriptor changed mid-second.
    """
    # Validate the entire small chunk before inserting any row.  A companion
    # poll may catch io.save halfway through its write; an incomplete compact
    # chunk must not contribute partial measurements.
    header = None
    footer_seen = False
    records = []
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError(f"bridge line {line_no} must be an object")
        marker = item.get("$")
        if footer_seen:
            raise ValueError("bridge data follows completed chunk footer")
        if marker == COMPACT_CHUNK_V1:
            if header is not None or line_no != 1:
                raise ValueError("compact channel header must appear once on line 1")
            descriptors = item.get("channels")
            if (not isinstance(descriptors, list) or not descriptors
                    or any(not isinstance(desc, list) or len(desc) != 3
                           or any(not isinstance(part, str) for part in desc)
                           for desc in descriptors)):
                raise ValueError("invalid compact channel descriptors")
            names = [desc[0] for desc in descriptors]
            if len(set(names)) != len(names):
                raise ValueError("duplicate compact channel name")
            if not isinstance(item.get("run_id"), str) or not isinstance(item.get("schema"), int):
                raise ValueError("invalid compact run header")
            header = item
            continue
        if marker == "s":
            if header is None:
                raise ValueError("compact sample has no channel header")
            values = item.get("v")
            descriptors = header["channels"]
            if not isinstance(values, list) or len(values) != len(descriptors):
                raise ValueError(f"compact sample {line_no} has wrong value count")
            overrides = item.get("q", [])
            if not isinstance(overrides, list):
                raise ValueError(f"compact sample {line_no} has invalid quality overrides")
            quality_by_index = {}
            for override in overrides:
                if (not isinstance(override, list) or len(override) != 2
                        or not isinstance(override[0], int) or isinstance(override[0], bool)
                        or not 1 <= override[0] <= len(descriptors)
                        or not isinstance(override[1], str) or override[0] in quality_by_index):
                    raise ValueError(f"compact sample {line_no} has invalid quality override")
                quality_by_index[override[0]] = override[1]
            data = {}
            for index, (name, unit, source) in enumerate(descriptors, 1):
                value = values[index - 1]
                data[name] = {"value": value, "unit": unit, "source": source,
                              "quality": quality_by_index.get(index, "unavailable" if value is None else "available")}
            records.append((line_no, {"schema": header["schema"], "run_id": header["run_id"],
                                      "physical_session_id": item.get("p", header.get("physical_session_id")),
                                      "seq": item.get("s"), "sim_time_s": item.get("t"),
                                      "wall_time_utc": item.get("w"), "kind": "sample", "data": data}))
            continue
        if marker == "e":
            if (header is None or not isinstance(item.get("n"), int)
                    or isinstance(item["n"], bool) or item["n"] != len(records)):
                raise ValueError("invalid compact chunk footer")
            footer_seen = True
            continue
        if marker is not None:
            raise ValueError(f"unsupported bridge line marker at line {line_no}")
        records.append((line_no, item))
    if header is not None and not footer_seen:
        raise ValueError("compact chunk is incomplete: missing footer")
    return records


class Store:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.directory / "wimna.sqlite3"
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL CHECK(mode IN ('MEASURED','SYNTHETIC')),
                physical_session_id TEXT NOT NULL,
                started_wall_utc TEXT,
                manifest_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                kind TEXT NOT NULL,
                sim_time_s REAL,
                wall_time_utc TEXT,
                data_json TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                PRIMARY KEY(run_id, seq),
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS records_kind ON records(kind);
            CREATE TABLE IF NOT EXISTS campaign_state (
                campaign_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_wall_s REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS predictions (
                prediction_id TEXT PRIMARY KEY,
                fit_run_ids_json TEXT NOT NULL,
                validation_run_id TEXT,
                model_json TEXT NOT NULL,
                prediction_json TEXT NOT NULL,
                registered_wall_s REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingested_files (
                relative_path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                records INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lap_annotations (
                run_id TEXT NOT NULL,
                lap_event_seq INTEGER NOT NULL,
                annotation_json TEXT NOT NULL,
                PRIMARY KEY(run_id, lap_event_seq)
            );
            CREATE TABLE IF NOT EXISTS run_assignments (
                run_id TEXT PRIMARY KEY,
                assignment_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def start_run(self, run_id: str, mode: str, manifest: dict) -> None:
        if not SAFE_ID.fullmatch(run_id):
            raise ValueError("invalid run_id")
        if mode not in ("MEASURED", "SYNTHETIC"):
            raise ValueError("mode must be MEASURED or SYNTHETIC")
        physical = manifest.get("physical_session_id")
        if not isinstance(physical, str) or not physical:
            raise ValueError("each game/app start needs an independent physical_session_id")
        encoded = _json(manifest)
        with self.db:
            existing = self.db.execute("SELECT mode,manifest_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                if existing != (mode, encoded):
                    raise ValueError("run_id reused with different physical state or manifest")
                return
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
                            (run_id, mode, physical, manifest.get("started_wall_utc"), encoded))

    def add_record(self, record: dict) -> bool:
        with self.db:
            return self._add_record_uncommitted(record)

    def _add_record_uncommitted(self, record: dict) -> bool:
        """Insert within the caller's transaction, for atomic bridge chunks."""
        run_id = record.get("run_id")
        seq = record.get("seq")
        kind = record.get("kind")
        sim_time = record.get("sim_time_s")
        if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
            raise ValueError("record has invalid run_id")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ValueError("record sequence must be a nonnegative integer")
        if kind not in ("sample", "event", "capability"):
            raise ValueError("record kind must be sample, event or capability")
        if sim_time is not None and (not isinstance(sim_time, (int, float)) or not math.isfinite(sim_time)):
            raise ValueError("sim_time_s must be finite or null")
        if not isinstance(record.get("data"), dict):
            raise ValueError("record data must be an object")
        if self.db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
            raise ValueError("start_run before appending records")
        raw = _json(record)
        data = _json(record["data"])
        try:
            self.db.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?)",
                            (run_id, seq, kind, sim_time, record.get("wall_time_utc"),
                             _pack_json(data), _pack_json(raw)))
        except sqlite3.IntegrityError:
            previous = self.db.execute("SELECT raw_json FROM records WHERE run_id=? AND seq=?", (run_id, seq)).fetchone()[0]
            if _unpack_json(previous) != raw:
                raise ValueError(f"record {run_id}/{seq} changed after ingest")
            return False
        return True

    def save_campaign(self, campaign_id: str, state: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO campaign_state VALUES (?,?,?) ON CONFLICT(campaign_id) DO UPDATE SET state_json=excluded.state_json, updated_wall_s=excluded.updated_wall_s",
                            (campaign_id, _json(state), time.time()))

    def load_campaign(self, campaign_id: str) -> dict | None:
        row = self.db.execute("SELECT state_json FROM campaign_state WHERE campaign_id=?", (campaign_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def iter_campaign_states(self) -> Iterator[dict]:
        for row in self.db.execute("SELECT campaign_id,state_json FROM campaign_state ORDER BY campaign_id"):
            yield {"campaign_id": row[0], "snapshot": json.loads(row[1])}

    def register_prediction(self, prediction_id: str, fit_run_ids: list[str], model: dict, prediction: dict,
                            *, registered_wall_s: float | None = None) -> None:
        with self.db:
            self.db.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?)",
                            (prediction_id, _json(fit_run_ids), None, _json(model), _json(prediction),
                             time.time() if registered_wall_s is None else registered_wall_s))

    def attach_validation(self, prediction_id: str, run_id: str) -> None:
        row = self.db.execute("SELECT fit_run_ids_json,validation_run_id FROM predictions WHERE prediction_id=?", (prediction_id,)).fetchone()
        if row is None:
            raise ValueError("prediction must be frozen before validation")
        if run_id in json.loads(row[0]):
            raise ValueError("validation run cannot also be a fit run")
        if row[1] not in (None, run_id):
            raise ValueError("prediction already linked to another validation run")
        with self.db:
            self.db.execute("UPDATE predictions SET validation_run_id=? WHERE prediction_id=?", (run_id, prediction_id))

    def iter_predictions(self) -> Iterator[dict]:
        for row in self.db.execute("SELECT prediction_id,fit_run_ids_json,validation_run_id,model_json,prediction_json,registered_wall_s FROM predictions ORDER BY prediction_id"):
            yield {"prediction_id": row[0], "fit_run_ids": json.loads(row[1]),
                   "validation_run_id": row[2], "model": json.loads(row[3]),
                   "prediction": json.loads(row[4]), "registered_wall_s": row[5]}

    def annotate_lap(self, run_id: str, lap_event_seq: int, phase: str, eligible: bool,
                     quality_flags: list[str], reason: str) -> None:
        if phase not in ("program", "warmup", "outlap", "pit", "unknown"):
            raise ValueError("invalid lap phase")
        if not isinstance(eligible, bool) or (eligible and phase != "program"):
            raise ValueError("only explicit program laps can be eligible")
        if self.db.execute("SELECT 1 FROM records WHERE run_id=? AND seq=? AND kind='event'", (run_id, lap_event_seq)).fetchone() is None:
            raise ValueError("lap event must exist before annotation")
        annotation = {"run_id": run_id, "lap_event_seq": lap_event_seq, "phase": phase,
                      "eligible": eligible, "quality_flags": sorted(set(quality_flags)), "reason": reason}
        encoded = _json(annotation)
        with self.db:
            existing = self.db.execute("SELECT annotation_json FROM lap_annotations WHERE run_id=? AND lap_event_seq=?",
                                       (run_id, lap_event_seq)).fetchone()
            if existing:
                if existing[0] != encoded:
                    raise ValueError("lap annotation cannot change after recording")
                return
            self.db.execute("INSERT INTO lap_annotations VALUES (?,?,?)", (run_id, lap_event_seq, encoded))

    def iter_lap_annotations(self, run_id: str | None = None) -> Iterator[dict]:
        query = "SELECT annotation_json FROM lap_annotations"
        parameters = ()
        if run_id:
            query += " WHERE run_id=?"
            parameters = (run_id,)
        query += " ORDER BY run_id,lap_event_seq"
        for row in self.db.execute(query, parameters):
            yield json.loads(row[0])

    def assign_run(self, run_id: str, track: str, block: str, assignment: dict | None = None) -> None:
        if self.db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
            raise ValueError("run must exist before assignment")
        if not track or not block:
            raise ValueError("track and block are required for director assignment")
        value = {"run_id": run_id, "track": track, "block": block,
                 "source": "WIMNA director", "details": assignment or {}}
        encoded = _json(value)
        with self.db:
            existing = self.db.execute("SELECT assignment_json FROM run_assignments WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                if existing[0] != encoded:
                    raise ValueError("physical run cannot be reassigned to a different block")
                return
            self.db.execute("INSERT INTO run_assignments VALUES (?,?)", (run_id, encoded))

    def get_run_assignment(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT assignment_json FROM run_assignments WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def iter_run_assignments(self) -> Iterator[dict]:
        for row in self.db.execute("SELECT assignment_json FROM run_assignments ORDER BY run_id"):
            yield json.loads(row[0])

    def iter_runs(self) -> Iterator[dict]:
        for row in self.db.execute("SELECT run_id,mode,manifest_json FROM runs ORDER BY run_id"):
            yield {"run_id": row[0], "mode": row[1], "manifest": json.loads(row[2])}

    def iter_records(self, run_id: str | None = None, kind: str | None = None) -> Iterator[dict]:
        query = "SELECT raw_json FROM records"
        clauses, values = [], []
        if run_id:
            clauses.append("run_id=?")
            values.append(run_id)
        if kind:
            clauses.append("kind=?")
            values.append(kind)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY run_id,seq"
        for row in self.db.execute(query, values):
            yield json.loads(_unpack_json(row[0]))

    def iter_collection_samples(self, run_id: str | None = None) -> Iterator[dict]:
        """Observed driving conditions, including both AI and human samples.

        Each item carries the original immutable record.  Controller and tire
        identities are observed fields; missing readbacks remain null/unknown.
        """
        from .collection_index import scan_collection
        for kind, item in scan_collection(self, run_id):
            if kind == "sample":
                yield item

    def iter_collection_stints(self, run_id: str | None = None) -> Iterator[dict]:
        """Stint boundaries reconstructed from raw events and sample readbacks."""
        from .collection_index import scan_collection
        for kind, item in scan_collection(self, run_id):
            if kind == "stint":
                yield item

    def counts(self) -> dict:
        return {"runs": self.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                "samples": self.db.execute("SELECT COUNT(*) FROM records WHERE kind='sample'").fetchone()[0],
                "events": self.db.execute("SELECT COUNT(*) FROM records WHERE kind='event'").fetchone()[0],
                "capabilities": self.db.execute("SELECT COUNT(*) FROM records WHERE kind='capability'").fetchone()[0]}


def ingest_bridge(bridge_root: str | Path, store: Store) -> dict:
    """Import completed 1-second JSONL chunks; repeated calls are idempotent.

    A run directory is a physical game/app start. A different run directory is
    never treated as a continuation of the prior native tire/fuel state.
    """
    bridge = Path(bridge_root)
    result = {"files": 0, "new_records": 0, "duplicates": 0, "errors": []}
    for folder in sorted((bridge / "runs").glob("*")):
        if not folder.is_dir() or not SAFE_ID.fullmatch(folder.name):
            continue
        run_id = folder.name
        manifest_path = folder / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            manifest = {"physical_session_id": run_id, "started_wall_utc": None,
                        "adapter": "CSP Lua bridge", "capability_status": "unverified"}
        manifest.setdefault("physical_session_id", run_id)
        store.start_run(run_id, "MEASURED", manifest)
        for chunk in sorted(folder.glob("*.jsonl")):
            relative = chunk.relative_to(bridge).as_posix()
            previous = store.db.execute("SELECT sha256,bytes FROM ingested_files WHERE relative_path=?", (relative,)).fetchone()
            if previous and chunk.stat().st_size == previous[1]:
                continue
            result["files"] += 1
            try:
                raw = chunk.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if previous:
                    if previous[0] != digest:
                        raise ValueError("completed bridge chunk changed after ingest")
                    continue
                records = _compact_chunk_records(raw)
                new_records = 0
                duplicates = 0
                with store.db:
                    for line_no, record in records:
                        if record.get("run_id") != run_id:
                            raise ValueError(f"run_id mismatch at line {line_no}")
                        if store._add_record_uncommitted(record):
                            new_records += 1
                        else:
                            duplicates += 1
                    store.db.execute("INSERT INTO ingested_files VALUES (?,?,?,?)",
                                     (relative, digest, len(raw), len(records)))
                result["new_records"] += new_records
                result["duplicates"] += duplicates
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                result["errors"].append({"file": str(chunk), "error": str(exc)})
    return result
