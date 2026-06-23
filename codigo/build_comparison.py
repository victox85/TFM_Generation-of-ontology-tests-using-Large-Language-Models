"""
Build a comparison CSV from a folder that contains:
  - a *terminology*generated* or *generated* .csv  (pipeline output with advises)
  - a *themis_tests* or *tests* .csv               (ground-truth / good tests)
  - optionally, a validator_ontology.py output CSV (columns:
    id, Competency question, Generated test, verdict) with one row per
    individual generated test line and its verdict.

Output: comparison.csv  next to the source files, with columns:
  Requirement identifier | Competency question | Good test | Generated test | Advises | Verdict

The Verdict column lines up 1:1 with the lines in "Generated test" (matched
by requirement id + normalized test text); left blank when no validator
output is found or a line has no matching verdict.

Usage:
    python build_comparison.py                  # opens folder picker
    python build_comparison.py /path/to/folder
"""

import csv
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog


# ── folder picker ─────────────────────────────────────────────────────────────

def pick_folder() -> Path | None:
    root = tk.Tk()
    root.withdraw()
    folder = filedialog.askdirectory(title="Select ontology folder")
    root.destroy()
    return Path(folder) if folder else None


# ── file discovery ────────────────────────────────────────────────────────────

_EXCLUDE = {"comparison", "requirement", "themis"}


def find_generated_csv(folder: Path) -> Path | None:
    """Find the terminology-aligned generated tests CSV (step 2 output).
    Falls back to the plain generated CSV (step 1 output)."""
    for pat in ["*terminology*generated*.csv", "*terminology*.csv",
                "*generated*.csv"]:
        hits = [p for p in sorted(folder.glob(pat))
                if not any(x in p.name.lower() for x in _EXCLUDE)]
        if hits:
            return hits[0]
    return None


def find_validator_csv(folder: Path) -> Path | None:
    """Find a validator_ontology.py output CSV: must have an id-like column,
    a 'Generated test' (or 'title') column and a 'verdict' column."""
    for p in sorted(folder.glob("*.csv")):
        if "comparison" in p.name.lower():
            continue
        try:
            with p.open(encoding="utf-8", errors="replace", newline="") as fh:
                sample = fh.read(4096)
                fh.seek(0)
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                reader = csv.DictReader(fh, dialect=dialect)
                headers = reader.fieldnames or []
        except Exception:
            continue

        has_id   = any(c in headers for c in _ID_COLS)
        has_test = any(c in headers for c in ["Generated test", "title"])
        has_verdict = "verdict" in headers
        if has_id and has_test and has_verdict:
            return p
    return None


def find_csv(folder: Path) -> Path | None:
    """Find the ground-truth tests CSV; skip generated / comparison files."""
    for pat in ["themis_tests*.csv", "tests*.csv", "test*.csv"]:
        hits = [p for p in sorted(folder.glob(pat))
                if "requirement" not in p.name.lower()
                and "generated"   not in p.name.lower()
                and "terminology" not in p.name.lower()
                and "comparison"  not in p.name.lower()]
        if hits:
            return hits[0]
    return None


# ── CSV loaders ───────────────────────────────────────────────────────────────

# Candidate column names for the ground-truth CSV (tried in order)
_ID_COLS   = ["Requirement", "Requirement_ID", "id", "Requirement identifier",
              "requirement_id", "Id", "ID", "Identifier", "identifier"]
_TEST_COLS = ["title", "Test", "RDF_Behaviour", "Themis_Test", "test", "Title"]


def load_good_tests(path: Path) -> dict[str, list[str]]:
    """Returns { req_id: [good_test, ...] } from the ground-truth CSV."""
    result: dict[str, list[str]] = {}
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            reader = csv.DictReader(fh, dialect=dialect)
            headers = reader.fieldnames or []

            id_col   = next((c for c in _ID_COLS   if c in headers), None)
            test_col = next((c for c in _TEST_COLS if c in headers), None)

            if not id_col or not test_col:
                print(f"  [warn] cannot map columns in {path.name}: {headers}")
                return result

            for row in reader:
                req_id = (row.get(id_col) or "").strip()
                test   = (row.get(test_col) or "").strip()
                if req_id and test:
                    result.setdefault(req_id, []).append(test)
    except Exception as exc:
        print(f"  [warn] error reading {path.name}: {exc}")
    return result


def _norm_test(s: str) -> str:
    return " ".join((s or "").split())


def load_verdicts(path: Path) -> dict[str, dict[str, str]]:
    """Returns { req_id: { normalized_test_text: verdict } } from a
    validator_ontology.py output CSV."""
    result: dict[str, dict[str, str]] = {}
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            reader = csv.DictReader(fh, dialect=dialect)
            headers = reader.fieldnames or []

            id_col   = next((c for c in _ID_COLS if c in headers), None)
            test_col = "Generated test" if "Generated test" in headers else "title"

            if not id_col or test_col not in headers or "verdict" not in headers:
                print(f"  [warn] cannot map columns in {path.name}: {headers}")
                return result

            for row in reader:
                req_id  = (row.get(id_col) or "").strip()
                test    = _norm_test(row.get(test_col) or "")
                verdict = (row.get("verdict") or "").strip()
                if req_id and test:
                    result.setdefault(req_id, {})[test] = verdict
    except Exception as exc:
        print(f"  [warn] error reading {path.name}: {exc}")
    return result


def load_generated_tests(path: Path) -> list[dict]:
    """Load the generated/aligned tests CSV produced by the pipeline."""
    rows = []
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append(dict(row))
    except Exception as exc:
        print(f"  [warn] error reading {path.name}: {exc}")
    return rows


# ── assembler ─────────────────────────────────────────────────────────────────

def build_rows(generated: list[dict], good_tests: dict[str, list[str]],
               verdicts: dict[str, dict[str, str]] | None = None) -> list[dict]:
    verdicts = verdicts or {}

    # validator_results.csv has one row per test; group by id so each
    # requirement gets one comparison row (matching the good-tests structure).
    if generated and "verdict" in generated[0]:
        grouped: dict[str, dict] = {}
        for gen in generated:
            req_id = (gen.get("id") or gen.get("Requirement identifier") or "").strip()
            if req_id not in grouped:
                grouped[req_id] = {
                    "id": req_id,
                    "Competency question": (gen.get("Competency question") or "").strip(),
                    "Generated test": [],
                    "verdict": [],
                }
            test = (gen.get("Generated test") or "").strip()
            verdict = (gen.get("verdict") or "").strip()
            if test:
                grouped[req_id]["Generated test"].append(test)
                grouped[req_id]["verdict"].append(verdict)
        generated = [
            {
                "id": v["id"],
                "Competency question": v["Competency question"],
                "Generated test": "\n".join(v["Generated test"]),
                "verdict": "\n".join(v["verdict"]),
            }
            for v in grouped.values()
        ]

    rows = []
    for gen in generated:
        req_id  = (gen.get("id") or gen.get("Requirement identifier") or "").strip()
        cq      = (gen.get("Competency question")    or "").strip()
        gen_str = (gen.get("Generated test")         or "").strip()
        adv_str = (gen.get("Advises")               or "").strip()

        good_list = good_tests.get(req_id, [])
        good_str  = "\n".join(t for t in good_list if t)

        direct_verdict = (gen.get("verdict") or "").strip()
        if direct_verdict:
            verdict_str = direct_verdict
        else:
            req_verdicts = verdicts.get(req_id, {})
            verdict_lines = [req_verdicts.get(_norm_test(line), "")
                              for line in gen_str.splitlines()]
            verdict_str = "\n".join(verdict_lines)

        rows.append({
            "Requirement identifier": req_id,
            "Competency question":    cq,
            "Good test":              good_str,
            "Generated test":         gen_str,
            "Advises":                adv_str,
            "Verdict":                verdict_str,
        })
    return rows


# ── entry point ───────────────────────────────────────────────────────────────

def run(folder: Path) -> None:
    """Build comparison.csv for the given folder. Returns without raising on soft errors."""
    if not folder or not folder.is_dir():
        print(f"No valid folder: {folder}")
        return

    gt_path  = find_csv(folder)
    val_path = find_validator_csv(folder)
    # validator_results.csv is the last CSV produced by the pipeline; use it as
    # the generated-tests source when present so verdicts come from it directly.
    if val_path and val_path != gt_path:
        gen_path = val_path
        val_path = None
    else:
        gen_path = find_generated_csv(folder)
        if val_path in (gen_path, gt_path):
            val_path = None

    if not gen_path:
        print(f"  [skip] No generated tests CSV found in: {folder}")
        return
    if not gt_path:
        print(f"  [skip] No ground-truth tests CSV found in: {folder}")
        return

    print(f"  GEN : {gen_path.name}")
    print(f"  GT  : {gt_path.name}")
    print(f"  VAL : {val_path.name if val_path else '(none found)'}")

    generated  = load_generated_tests(gen_path)
    good_tests = load_good_tests(gt_path)
    verdicts   = load_verdicts(val_path) if val_path else {}
    rows       = build_rows(generated, good_tests, verdicts)

    out_path = folder / "comparison.csv"
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "Requirement identifier", "Competency question",
            "Good test", "Generated test", "Advises", "Verdict",
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"  Done - {len(rows)} rows -> {out_path}")


def main() -> None:
    if len(sys.argv) > 1:
        folder = Path(sys.argv[1])
    else:
        folder = pick_folder()

    run(folder)


if __name__ == "__main__":
    main()
