from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


class PredictionMappingError(ValueError):
    pass


def load_prediction_verified_mapping_file(path: Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        raise PredictionMappingError(f"Mapping file does not exist: {source}")
    suffix = source.suffix.lower()
    if suffix == ".csv":
        with source.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    elif suffix == ".jsonl":
        rows = [
            json.loads(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        payload = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows = payload.get("mappings") or []
        else:
            rows = payload
    if not isinstance(rows, list):
        raise PredictionMappingError("Mapping file must contain a list of mappings")
    return [normalize_prediction_verified_mapping(row) for row in rows]


def normalize_prediction_verified_mapping(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise PredictionMappingError("Each mapping row must be an object")
    output = {
        "venue_a": required_text(row, "venue_a"),
        "market_id_a": required_text(row, "market_id_a"),
        "venue_b": required_text(row, "venue_b"),
        "market_id_b": required_text(row, "market_id_b"),
        "relation_type": text_value(row.get("relation_type")) or "equivalent",
        "status": text_value(row.get("status")) or "active",
        "confidence_score": confidence_value(row.get("confidence_score")),
        "verified_by": text_value(row.get("verified_by")),
        "verified_at": text_value(row.get("verified_at")),
        "notes": text_value(row.get("notes")),
        "rationale": rationale_value(row),
    }
    if output["venue_a"] == output["venue_b"]:
        raise PredictionMappingError("Mapping venues must differ")
    if output["relation_type"] != "equivalent":
        raise PredictionMappingError("Only equivalent trusted mappings are supported")
    return output


def required_text(row: dict[str, Any], key: str) -> str:
    value = text_value(row.get(key))
    if not value:
        raise PredictionMappingError(f"Mapping row is missing {key}")
    return value


def text_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def confidence_value(value: Any) -> float:
    if value is None or str(value).strip() == "":
        return 1.0
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise PredictionMappingError("confidence_score must be numeric") from exc
    return max(0.0, min(1.0, confidence))


def rationale_value(row: dict[str, Any]) -> list[str]:
    value = row.get("rationale")
    if value is None:
        value = row.get("rationale_json")
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [part.strip() for part in text.split("|") if part.strip()]
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [str(parsed).strip()] if str(parsed).strip() else []
