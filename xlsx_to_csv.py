"""
Converts all .xlsx files in a folder tree to .csv, next to each source file.
Usage:
    python xlsx_to_csv.py                        # uses current directory
    python xlsx_to_csv.py /path/to/folder
"""

import csv
import sys
from pathlib import Path

import openpyxl


def xlsx_to_csv(xlsx_path: Path) -> Path:
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active
    csv_path = xlsx_path.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in ws.iter_rows(values_only=True):
            writer.writerow(["" if v is None else v for v in row])
    wb.close()
    return csv_path


def main(root: Path) -> None:
    xlsx_files = list(root.rglob("*.xlsx"))
    if not xlsx_files:
        print(f"No .xlsx files found under {root}")
        return
    for xlsx in xlsx_files:
        try:
            out = xlsx_to_csv(xlsx)
            print(f"  OK  {xlsx.relative_to(root)}  →  {out.name}")
        except Exception as e:
            print(f"  ERR {xlsx.relative_to(root)}: {e}")
    print(f"\nDone — {len(xlsx_files)} file(s) processed.")


if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    if not folder.is_dir():
        print(f"Not a directory: {folder}")
        sys.exit(1)
    main(folder)
