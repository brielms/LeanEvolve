"""Validate and render the hash-bound dead-route research ledger."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from formal.shinka.audit import file_record


LEDGER_FORMAT = "shinka-dead-route-ledger-v1"
LEDGER_RELATIVE_PATH = Path("formal/shinka/context/dead_routes.json")
ALLOWED_STATUSES = {
    "kernel_refuted",
    "finite_counterexample_untrusted",
    "published_counterexample",
    "superseded",
}
# A published refutation is neither a local Lean theorem nor a local finite
# probe.  It is a citation, and it prunes proof search only to the extent that
# the cited construction is trusted.  Require an identifier that a reader can
# resolve, so the entry cannot degrade into an unattributed claim.
CITATION_REQUIRED_STATUSES = {"published_counterexample"}


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_dead_routes(
    repository_root: Path,
    ledger_path: Path | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    root = repository_root.resolve()
    path = (
        ledger_path.resolve()
        if ledger_path is not None
        else root / LEDGER_RELATIVE_PATH
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != LEDGER_FORMAT:
        raise ValueError("unsupported dead-route ledger format")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("dead-route ledger entries must be a nonempty list")

    seen: set[str] = set()
    validated: list[dict[str, object]] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise ValueError(f"dead-route entry {index} is not an object")
        route_id = str(raw.get("route_id", ""))
        if re.fullmatch(r"[a-z][a-z0-9_]+", route_id) is None:
            raise ValueError(f"invalid dead-route id: {route_id!r}")
        if route_id in seen:
            raise ValueError(f"duplicate dead-route id: {route_id}")
        seen.add(route_id)
        status = str(raw.get("status", ""))
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"invalid dead-route status for {route_id}")
        if not str(raw.get("claim", "")).strip():
            raise ValueError(f"dead route {route_id} lacks a claim")
        if not str(raw.get("lesson", "")).strip():
            raise ValueError(f"dead route {route_id} lacks a lesson")

        record = dict(raw)
        if status == "kernel_refuted":
            declaration = str(raw.get("formal_declaration", ""))
            source_relative = str(raw.get("formal_source", ""))
            source = (root / source_relative).resolve()
            if not source.is_relative_to(root) or not source.is_file():
                raise ValueError(
                    f"dead route {route_id} has invalid formal source"
                )
            source_record = file_record(source)
            if source_record["sha256"] != raw.get("formal_source_sha256"):
                raise ValueError(
                    f"dead route {route_id} formal source hash changed"
                )
            short_name = declaration.rsplit(".", 1)[-1]
            source_text = source.read_text(encoding="utf-8")
            if re.search(
                rf"\btheorem\s+{re.escape(short_name)}(?:\s|:)",
                source_text,
            ) is None:
                raise ValueError(
                    f"dead route {route_id} declaration is absent from source"
                )
            record["formal_evidence"] = {
                "declaration": declaration,
                "source": source_relative,
                **source_record,
            }
        elif status in CITATION_REQUIRED_STATUSES:
            citation = raw.get("citation")
            if not isinstance(citation, dict):
                raise ValueError(
                    f"dead route {route_id} lacks a citation object"
                )
            missing = [
                field
                for field in ("reference", "identifier")
                if not str(citation.get(field, "")).strip()
            ]
            if missing:
                raise ValueError(
                    f"dead route {route_id} citation lacks: "
                    + ", ".join(missing)
                )
            record["trust_warning"] = (
                "not a formal refutation; cited published construction, "
                "not reconstructed in Lean here"
            )
        else:
            record["trust_warning"] = (
                "not a formal refutation; discovery evidence only"
            )
        record["entry_sha256"] = _canonical_sha256(raw)
        validated.append(record)

    manifest = {
        "format": LEDGER_FORMAT,
        "ledger": {
            "path": str(path.relative_to(root)),
            **file_record(path),
        },
        "entry_count": len(validated),
        "kernel_refuted_count": sum(
            item["status"] == "kernel_refuted" for item in validated
        ),
        "published_counterexample_count": sum(
            item["status"] == "published_counterexample"
            for item in validated
        ),
        "entries_sha256": _canonical_sha256(validated),
    }
    return manifest, validated


def render_dead_routes(
    repository_root: Path,
    ledger_path: Path | None = None,
) -> str:
    manifest, entries = load_dead_routes(repository_root, ledger_path)
    lines = [
        "# Dead-route ledger",
        "",
        f"Ledger SHA-256: `{manifest['ledger']['sha256']}`",
        f"Validated entries SHA-256: `{manifest['entries_sha256']}`",
        "",
        "`kernel_refuted` means the named counterexample theorem and its "
        "source hash were validated here and are checked by the repository's "
        "Lean gate. `finite_counterexample_untrusted` is only a discovery "
        "warning and must never be cited as a proof. "
        "`published_counterexample` means the literature supplies a "
        "construction refuting the claim; the construction has not been "
        "reconstructed in Lean here, so it prunes search but is never a "
        "proof premise and never earns credit.",
    ]
    for item in entries:
        lines.extend([
            "",
            f"## `{item['route_id']}` — {item['status']}",
            "",
            f"Claim ruled out or challenged: {item['claim']}",
            "",
            f"Lesson: {item['lesson']}",
            "",
            f"Entry SHA-256: `{item['entry_sha256']}`",
        ])
        evidence = item.get("formal_evidence")
        if isinstance(evidence, dict):
            lines.extend([
                "",
                f"Formal declaration: `{evidence['declaration']}`",
                f"Formal source SHA-256: `{evidence['sha256']}`",
            ])
        citation = item.get("citation")
        if isinstance(citation, dict):
            lines.extend([
                "",
                f"Published refutation: {citation['reference']} "
                f"({citation['identifier']})",
            ])
            locator = str(citation.get("locator", "")).strip()
            if locator:
                lines.append(f"Exact location in the source: {locator}")
        if item.get("probe") is not None:
            lines.extend([
                "",
                "Untrusted finite probe:",
                "",
                "```json",
                json.dumps(item["probe"], indent=2, sort_keys=True),
                "```",
            ])
    return "\n".join(lines).rstrip() + "\n"
