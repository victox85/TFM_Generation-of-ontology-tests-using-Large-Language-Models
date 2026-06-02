"""
Build a comparison CSV from a folder that contains:
  - a *test_generated* or *+terminology* .txt  (generated tests + advises)
  - a *themis_tests* or *tests* .csv            (ground-truth / good tests)

Output: comparison.csv  next to the source files, with columns:
  Requirement identifier | Competency question | Good test | Generated test | Advises

Usage:
    python build_comparison.py                  # opens folder picker
    python build_comparison.py /path/to/folder
"""

import csv
import re
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

def find_txt(folder: Path) -> Path | None:
    """Prefer +terminology files; fall back to any test_generated."""
    for pat in ["*+terminology*.txt", "*terminology*.txt",
                "*Test_generated*.txt", "*generated*.txt"]:
        hits = sorted(folder.glob(pat))
        if hits:
            return hits[0]
    return None


def find_csv(folder: Path) -> Path | None:
    """Prefer themis_tests / tests CSVs; skip requirements CSVs."""
    for pat in ["themis_tests*.csv", "tests*.csv", "test*.csv"]:
        hits = [p for p in sorted(folder.glob(pat))
                if "requirement" not in p.name.lower()]
        if hits:
            return hits[0]
    return None


# ── TXT parser ────────────────────────────────────────────────────────────────

# Matches:  // FACI-1 — Competency question text
# Handles em-dash (—), en-dash (–), or plain hyphen after the ID.
_HEADER_RE = re.compile(r'^(?://\s+)?(\S+)\s+[—–-]+\s*(.+)$')


def parse_txt(path: Path) -> list[dict]:
    """
    Returns a list of blocks:
      { 'id': str, 'cq': str, 'tests': [{'line': str, 'advise': str}] }
    """
    blocks: list[dict] = []
    current: dict | None = None

    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip()

            # Skip markdown fences and summary sections
            if line.strip().startswith("```") or "─── SUMMARY" in line:
                current = None  # stop collecting after summary
                continue

            # Header line
            m = _HEADER_RE.match(line)
            if m:
                req_id = m.group(1).strip()
                cq = m.group(2).strip()
                # Drop inline annotations that start with ► or similar
                cq = re.split(r'\s*[►▶»>]\s*', cq)[0].strip()
                current = {"id": req_id, "cq": cq, "tests": []}
                blocks.append(current)
                continue

            # Empty line – just a separator
            if not line.strip():
                continue

            # Test line (must be inside a block and not a bare comment)
            if current is not None and not line.lstrip().startswith("//"):
                if "//" in line:
                    code_part, advice_part = line.split("//", 1)
                    code = code_part.strip()
                    advise = advice_part.strip()
                else:
                    code = line.strip()
                    advise = ""
                if code:
                    current["tests"].append({"line": code, "advise": advise})

    return blocks


# ── CSV loader ────────────────────────────────────────────────────────────────

# Candidate column names (tried in order)
_ID_COLS   = ["Requirement", "Requirement_ID", "id", "Requirement identifier",
              "requirement_id", "Id", "ID", "Identifier", "identifier"]
_TEST_COLS = ["title", "Test", "RDF_Behaviour", "Themis_Test", "test", "Title"]


def load_good_tests(path: Path) -> dict[str, list[str]]:
    """Returns { req_id: [good_test, ...] }."""
    result: dict[str, list[str]] = {}
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as fh:
            # Sniff for comma, semicolon or tab delimiter
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


# ── assembler ─────────────────────────────────────────────────────────────────

def build_rows(blocks: list[dict], good_tests: dict[str, list[str]]) -> list[dict]:
    rows = []
    for block in blocks:
        req_id = block["id"]
        cq     = block["cq"]

        good_list = good_tests.get(req_id, [])
        good_str  = "\n".join(t for t in good_list if t)

        gen_lines = [t["line"]   for t in block["tests"]]
        adv_lines = [t["advise"] for t in block["tests"]]

        gen_str = "\n".join(gen_lines)
        adv_str = "\n".join(a for a in adv_lines if a)

        rows.append({
            "Requirement identifier": req_id,
            "Competency question":    cq,
            "Good test":              good_str,
            "Generated test":         gen_str,
            "Advises":                adv_str,
        })
    return rows


# ── entry point ───────────────────────────────────────────────────────────────

def run(folder: Path) -> None:
    """Build comparison.csv for the given folder. Returns without raising on soft errors."""
    if not folder or not folder.is_dir():
        print(f"No valid folder: {folder}")
        return

    txt_path = find_txt(folder)
    csv_path = find_csv(folder)

    if not txt_path:
        print(f"  [skip] No test_generated / +terminology .txt found in: {folder}")
        return
    if not csv_path:
        print(f"  [skip] No themis_tests / tests .csv found in: {folder}")
        return

    print(f"  TXT : {txt_path.name}")
    print(f"  CSV : {csv_path.name}")

    blocks     = parse_txt(txt_path)
    good_tests = load_good_tests(csv_path)
    rows       = build_rows(blocks, good_tests)

    out_path = folder / "comparison.csv"
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "Requirement identifier", "Competency question",
            "Good test", "Generated test", "Advises",
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"  Done — {len(rows)} rows → {out_path}")


def main() -> None:
    if len(sys.argv) > 1:
        folder = Path(sys.argv[1])
    else:
        folder = pick_folder()

    run(folder)


if __name__ == "__main__":
    main()
