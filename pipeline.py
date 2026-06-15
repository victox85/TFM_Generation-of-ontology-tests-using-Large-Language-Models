"""
Pipeline GUI: configure and run ontology test generation steps.

Steps:
  1 (mandatory) lm_file_tool                — requirements.csv → test_generated.csv
  2 (optional)  ontology_terminology_extractor — ontology + step-1 text → terminology+test_generated.csv
  3 (optional)  build_comparison             — generated CSV + ground-truth CSV → comparison.csv
"""

import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

import lm_file_tool
import ontology_terminology_extractor
import build_comparison


ONTOLOGY_EXTS = {".ttl", ".owl", ".rdf", ".n3", ".nt", ".jsonld", ".xml"}


# ── stdout redirect ───────────────────────────────────────────────────────────

class _TextRedirect:
    def __init__(self, widget: tk.Text) -> None:
        self.widget = widget

    def write(self, text: str) -> None:
        self.widget.configure(state="normal")
        self.widget.insert(tk.END, text)
        self.widget.see(tk.END)
        self.widget.configure(state="disabled")

    def flush(self) -> None:
        pass


# ── pipeline logic ────────────────────────────────────────────────────────────

def find_ontology(folder: Path) -> Path | None:
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.suffix.lower() in ONTOLOGY_EXTS:
            return f
    return None


def process_folder(folder: Path, run_step2: bool, run_step3: bool) -> None:
    print(f"\n{'='*60}")
    print(f"Processing: {folder.name}")
    print(f"{'='*60}")

    req_path = folder / "requirements.csv"
    if not req_path.exists():
        print("  [skip] No requirements.csv found.")
        return

    ontology_path = find_ontology(folder)
    if not ontology_path and run_step2:
        print("  [skip] No ontology file found (required for Step 2).")
        return

    # Step 1 — always runs
    test_generated_csv = folder / "test_generated.csv"
    print(f"\n[Step 1] Generating tests from {req_path.name}…")
    try:
        step1_text = lm_file_tool.process_csv_file(str(req_path))
        csv_content = lm_file_tool.response_to_csv(step1_text)
        with test_generated_csv.open("w", encoding="utf-8", newline="") as fh:
            fh.write(csv_content)
        print(f"  → Saved: {test_generated_csv.name}")
    except Exception as e:
        print(f"  [error] Step 1 failed: {e}")
        return

    # Step 2 — optional
    if run_step2:
        terminology_csv = folder / "terminology+test_generated.csv"
        print(f"\n[Step 2] Aligning with terminology from {ontology_path.name}…")
        try:
            step2_text = ontology_terminology_extractor.process_ontology_and_tests(
                str(ontology_path), tests_content=step1_text
            )
            csv_content2 = ontology_terminology_extractor.response_to_csv(step2_text)
            with terminology_csv.open("w", encoding="utf-8", newline="") as fh:
                fh.write(csv_content2)
            print(f"  → Saved: {terminology_csv.name}")
        except Exception as e:
            print(f"  [error] Step 2 failed: {e}")
            return
    else:
        print("\n[Step 2] Skipped.")

    # Step 3 — optional
    if run_step3:
        print("\n[Step 3] Building comparison.csv…")
        try:
            build_comparison.run(folder)
        except Exception as e:
            print(f"  [error] Step 3 failed: {e}")
    else:
        print("\n[Step 3] Skipped.")


# ── GUI ───────────────────────────────────────────────────────────────────────

class PipelineApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Ontology Test Pipeline")
        self.resizable(True, False)
        self.minsize(520, 0)
        self._build_ui()

    def _build_ui(self) -> None:
        pad = {"padx": 14, "pady": 6}

        # ── Folder mode ───────────────────────────────────────────────────────
        mode_frame = ttk.LabelFrame(self, text="Folder mode", padding=10)
        mode_frame.pack(fill="x", **pad)

        self.folder_mode = tk.StringVar(value="all")
        ttk.Radiobutton(
            mode_frame,
            text="All subfolders inside a root folder",
            variable=self.folder_mode,
            value="all",
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            mode_frame,
            text="Single folder",
            variable=self.folder_mode,
            value="single",
        ).pack(anchor="w", pady=2)

        # ── Steps ─────────────────────────────────────────────────────────────
        steps_frame = ttk.LabelFrame(self, text="Steps to run", padding=10)
        steps_frame.pack(fill="x", **pad)

        self.step1_var = tk.BooleanVar(value=True)
        cb1 = ttk.Checkbutton(
            steps_frame,
            text="Step 1 — lm_file_tool  (mandatory)",
            variable=self.step1_var,
            state="disabled",
        )
        cb1.pack(anchor="w", pady=2)

        self.step2_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            steps_frame,
            text="Step 2 — ontology_terminology_extractor",
            variable=self.step2_var,
        ).pack(anchor="w", pady=2)

        self.step3_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            steps_frame,
            text="Step 3 — build_comparison",
            variable=self.step3_var,
        ).pack(anchor="w", pady=2)

        # ── Run button ────────────────────────────────────────────────────────
        self.run_btn = ttk.Button(self, text="Run pipeline", command=self._start)
        self.run_btn.pack(pady=10)

        # ── Output log ────────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self, text="Output", padding=6)
        log_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        self.log = tk.Text(
            log_frame,
            height=18,
            state="disabled",
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="#d4d4d4",
            font=("Consolas", 9),
            wrap="word",
            relief="flat",
        )
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # ── actions ───────────────────────────────────────────────────────────────

    def _start(self) -> None:
        self.run_btn.configure(state="disabled")
        self.log.configure(state="normal")
        self.log.delete("1.0", tk.END)
        self.log.configure(state="disabled")

        mode = self.folder_mode.get()
        run_step2 = self.step2_var.get()
        run_step3 = self.step3_var.get()

        title = "Select root corpus folder" if mode == "all" else "Select ontology folder"
        folder = filedialog.askdirectory(title=title)
        if not folder:
            self.run_btn.configure(state="normal")
            return

        target = Path(folder)

        def run() -> None:
            old_stdout = sys.stdout
            sys.stdout = _TextRedirect(self.log)
            try:
                if mode == "all":
                    subfolders = sorted(
                        p for p in target.iterdir()
                        if p.is_dir() and not p.name.startswith(".")
                    )
                    if not subfolders:
                        print(f"No subfolders found in: {target}")
                    else:
                        print(f"Found {len(subfolders)} subfolder(s) in: {target}")
                        for subfolder in subfolders:
                            process_folder(subfolder, run_step2, run_step3)
                else:
                    process_folder(target, run_step2, run_step3)
                print("\nPipeline complete.")
            except Exception as e:
                print(f"\n[fatal] {e}")
            finally:
                sys.stdout = old_stdout
                self.after(0, lambda: self.run_btn.configure(state="normal"))

        threading.Thread(target=run, daemon=True).start()


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = PipelineApp()
    app.mainloop()


if __name__ == "__main__":
    main()
