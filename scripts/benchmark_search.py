"""Reproducible synthetic snapshot search benchmark; no host config or model calls."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1 or any(size < 1 for size in args.sizes):
        parser.error("sizes and repeats must be positive")
    source = Path(__file__).resolve().parents[1] / "skills/manage-agent-skills/scripts/skillctl.py"
    spec = importlib.util.spec_from_file_location("skillctl_benchmark", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    reports = []
    with tempfile.TemporaryDirectory(prefix="skillctl-benchmark-") as temporary:
        path = Path(temporary) / "index.json"
        for size in args.sizes:
            catalog = {"schema_version": 1, "generated_at": "2026-10-23T00:00:00+00:00",
                       "evidence_scope": "synthetic snapshot", "errors": [], "skills": [
                           asdict(module.Skill("codex", f"skill-{i:05}", f"/synthetic/{i}/SKILL.md",
                                              "test", "synthetic", "off",
                                              description="Extract invoice totals from documents 发票识别"))
                           for i in range(size)]}
            path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
            durations = []
            for _ in range(args.repeats):
                start = time.perf_counter()
                result = module.search_catalog(module.read_index(path), "invoice totals", 5)
                durations.append((time.perf_counter() - start) * 1000)
            assert result["total"] == size and len(result["results"]) == min(5, size)
            reports.append({"skills": size, "repeats": args.repeats,
                            "p50_ms": round(statistics.median(durations), 2),
                            "p95_ms": round(sorted(durations)[math.ceil(0.95 * len(durations)) - 1], 2),
                            "returned": len(result["results"]), "total": result["total"],
                            "output_characters": len(json.dumps(result, ensure_ascii=False))})
    print(json.dumps({"scope": "snapshot file read + validation + lexical ranking; excludes Python startup, host scans and model tokens",
                      "results": reports}, indent=2))


if __name__ == "__main__":
    main()
