"""
Pipeline: iterate over every subfolder of a selected root folder and run:
  1. lm_file_tool     — requirements.csv → test_generated.txt
  2. ontology_terminology_extractor — ontology + test_generated.txt → test_generated+terminology.txt
  3. build_comparison — test_generated+terminology.txt + ground-truth CSV → comparison.csv

Usage:
    python pipeline.py                  # opens folder picker
    python pipeline.py /path/to/folder
"""

import sys
from pathlib import Path
import tkinter as tk
from tkinter import filedialog

import lm_file_tool
import ontology_terminology_extractor
import build_comparison


ONTOLOGY_EXTS = {".ttl", ".owl", ".rdf", ".n3", ".nt", ".jsonld", ".xml"}


def pick_folder() -> Path | None:
    root = tk.Tk()
    root.withdraw()
    folder = filedialog.askdirectory(title="Select root corpus folder")
    root.destroy()
    return Path(folder) if folder else None


def find_ontology(folder: Path) -> Path | None:
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.suffix.lower() in ONTOLOGY_EXTS:
            return f
    return None


def process_folder(folder: Path) -> None:
    print(f"\n{'='*60}")
    print(f"Processing: {folder.name}")
    print(f"{'='*60}")

    req_path = folder / "requirements.csv"
    if not req_path.exists():
        print("  [skip] No requirements.csv found.")
        return

    ontology_path = find_ontology(folder)
    if not ontology_path:
        print("  [skip] No ontology file found.")
        return

    # Step 1: generate tests from requirements
    test_generated_path = folder / "test_generated.txt"
    print(f"\n[Step 1] Generating tests from {req_path.name}…")
    try:
        result = lm_file_tool.process_csv_file(str(req_path))
        test_generated_path.write_text(result, encoding="utf-8")
        print(f"  → Saved: {test_generated_path.name}")
    except Exception as e:
        print(f"  [error] Step 1 failed: {e}")
        return

    # Step 2: align with ontology terminology
    test_plus_path = folder / "test_generated+terminology.txt"
    print(f"\n[Step 2] Aligning with terminology from {ontology_path.name}…")
    try:
        result = ontology_terminology_extractor.process_ontology_and_tests(
            str(ontology_path), str(test_generated_path)
        )
        test_plus_path.write_text(result, encoding="utf-8")
        print(f"  → Saved: {test_plus_path.name}")
    except Exception as e:
        print(f"  [error] Step 2 failed: {e}")
        return

    # Step 3: build comparison CSV
    print(f"\n[Step 3] Building comparison.csv…")
    try:
        build_comparison.run(folder)
    except Exception as e:
        print(f"  [error] Step 3 failed: {e}")


def main() -> None:
    if len(sys.argv) > 1:
        root_folder = Path(sys.argv[1])
    else:
        root_folder = pick_folder()

    if not root_folder or not root_folder.is_dir():
        print("No valid folder selected.")
        return

    subfolders = sorted(
        p for p in root_folder.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )

    if not subfolders:
        print(f"No subfolders found in: {root_folder}")
        return

    print(f"Found {len(subfolders)} subfolder(s) in: {root_folder}")
    for folder in subfolders:
        process_folder(folder)

    print("\nPipeline complete.")


if __name__ == "__main__":
    main()
