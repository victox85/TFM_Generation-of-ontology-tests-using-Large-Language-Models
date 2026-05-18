import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox
import csv
import io
import json
import re
import urllib.request
import urllib.error
import threading
import os
import subprocess




LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"
CHUNK_SIZE = 20    # rows per request (excluding header)
REQUEST_TIMEOUT = 1200  # seconds to wait for each chunk response

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def _load_prompt(filename: str) -> str:
    path = os.path.join(_PROMPTS_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


SYSTEM_PROMPT_DECLARATIVE = _load_prompt("system_prompt_declarative.txt")
SYSTEM_PROMPT_INTERROGATIVE = _load_prompt("system_prompt_interrogative.txt")


_QUESTION_STARTERS = (
    'what ', 'which ', 'when ', 'how ', 'where ', 'who ', 'why ',
    'is ', 'are ', 'does ', 'do ', 'can ', 'did ', 'was ', 'were ',
)


def is_interrogative(text):
    """Return True if the CQ is a question (interrogative form)."""
    t = text.strip()
    if '?' in t:
        return True
    lower = t.lower()
    return any(lower.startswith(w) for w in _QUESTION_STARTERS)


def _find_requirement_index(header):
    """Return the index of the Requirement column, or -1 if not found."""
    candidates = ('requirement', 'competency question', 'cq', 'question', 'req', 'fact')
    for i, col in enumerate(header):
        if col.strip().lower() in candidates:
            return i
    return len(header) - 1  # fallback: last column


def split_csv_chunks_by_type(file_content, chunk_size=CHUNK_SIZE):
    """
    Parse CSV and split rows into declarative and interrogative groups.
    Returns (declarative_chunks, interrogative_chunks), each a list of CSV strings
    with the original header prepended to every chunk.
    """
    reader = csv.reader(io.StringIO(file_content))
    rows = list(reader)
    if len(rows) < 2:
        return [file_content], []

    header = rows[0]
    data_rows = rows[1:]
    req_idx = _find_requirement_index(header)

    declarative_rows = []
    interrogative_rows = []

    for row in data_rows:
        req_text = row[req_idx] if req_idx < len(row) else (row[-1] if row else '')
        if is_interrogative(req_text):
            interrogative_rows.append(row)
        else:
            declarative_rows.append(row)

    def rows_to_chunks(data):
        chunks = []
        for i in range(0, len(data), chunk_size):
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(header)
            writer.writerows(data[i : i + chunk_size])
            chunks.append(buf.getvalue())
        return chunks

    return rows_to_chunks(declarative_rows), rows_to_chunks(interrogative_rows)


def _fix_lm_output(text: str) -> str:
    """Fix concatenated tokens like 'XSubClassOf B' → 'X SubClassOf B' produced by the LM."""
    # Insert missing space: "XSubClassOf" → "X SubClassOf"
    text = re.sub(r'(?<=\w)SubClassOf', ' SubClassOf', text)
    # Collapse accidental double: "X SubClassOf SubClassOf B" → "X SubClassOf B"
    text = re.sub(r'SubClassOf(\s+SubClassOf)+', 'SubClassOf', text)
    return text


def send_chunk(system_prompt, chunk_content, file_name):
    """Send one chunk to LM Studio synchronously. Returns (answer, error)."""
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"File name: {file_name}\n\nFile content:\n{chunk_content}",
        },
    ]
    payload = json.dumps(
        {"messages": messages, "temperature": 0.2, "stream": False}
    ).encode("utf-8")

    req = urllib.request.Request(
        LM_STUDIO_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"], None
    except urllib.error.URLError as e:
        return None, f"Connection error: {e.reason}"
    except Exception as e:
        return None, str(e)


def process_csv_file(csv_path: str) -> str:
    """Process a requirements CSV and return the merged Themis test output."""
    with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
        file_content = f.read()

    file_name = os.path.basename(csv_path)
    dec_chunks, int_chunks = split_csv_chunks_by_type(file_content)

    work = []
    for i, c in enumerate(dec_chunks, 1):
        work.append((SYSTEM_PROMPT_DECLARATIVE, c, f"declarative {i}/{len(dec_chunks)}"))
    for i, c in enumerate(int_chunks, 1):
        work.append((SYSTEM_PROMPT_INTERROGATIVE, c, f"interrogative {i}/{len(int_chunks)}"))

    if not work:
        return ""

    results = []
    for idx, (sys_prompt, chunk, label) in enumerate(work, 1):
        print(f"  Chunk {idx}/{len(work)} ({label})…")
        answer, error = send_chunk(sys_prompt, chunk, file_name)
        if error:
            raise RuntimeError(f"LM Studio error on chunk {idx}: {error}")
        results.append(answer)

    return _fix_lm_output("\n\n".join(results))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LM Studio File Tool")
        self.resizable(True, True)
        self.geometry("800x680")
        self.configure(bg="#1e1e2e")

        self._file_path = None
        self._build_ui()

    def _label(self, parent, text):
        return tk.Label(
            parent, text=text, bg="#1e1e2e", fg="#cdd6f4",
            font=("Segoe UI", 10, "bold"), anchor="w"
        )

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        # ── System prompt info ────────────────────────────────────────────
        self._label(self, "System prompts (defined in code)").pack(fill="x", **pad)

        prompts_frame = tk.Frame(self, bg="#1e1e2e")
        prompts_frame.pack(fill="x", padx=12, pady=(0, 6))

        for label_text, prompt_text in [
            ("Declarative CQs:", SYSTEM_PROMPT_DECLARATIVE),
            ("Interrogative CQs:", SYSTEM_PROMPT_INTERROGATIVE),
        ]:
            row_frame = tk.Frame(prompts_frame, bg="#1e1e2e")
            row_frame.pack(fill="x", pady=2)
            tk.Label(
                row_frame, text=label_text, bg="#1e1e2e", fg="#89b4fa",
                font=("Segoe UI", 9, "bold"), width=18, anchor="w"
            ).pack(side="left")
            preview = scrolledtext.ScrolledText(
                row_frame, height=2, wrap=tk.WORD,
                bg="#1e1e2e", fg="#6c7086",
                font=("Segoe UI", 9, "italic"), relief=tk.FLAT, padx=6, pady=4,
            )
            preview.insert(tk.END, prompt_text.strip()[:200] + "…")
            preview.config(state=tk.DISABLED)
            preview.pack(fill="x", side="left", expand=True)

        # ── File picker ──────────────────────────────────────────────────
        file_row = tk.Frame(self, bg="#1e1e2e")
        file_row.pack(fill="x", **pad)
        self._label(file_row, "Attached file:").pack(side="left")
        self.lbl_file = tk.Label(
            file_row, text="No file selected", bg="#1e1e2e", fg="#6c7086",
            font=("Segoe UI", 10), anchor="w"
        )
        self.lbl_file.pack(side="left", padx=8)
        tk.Button(
            file_row, text="Browse…", command=self._pick_file,
            bg="#89b4fa", fg="#1e1e2e", font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, padx=10, cursor="hand2"
        ).pack(side="right")

        # ── Send button ──────────────────────────────────────────────────
        btn_row = tk.Frame(self, bg="#1e1e2e")
        btn_row.pack(fill="x", padx=12, pady=6)
        self.btn_send = tk.Button(
            btn_row, text="Send to LM Studio", command=self._on_send,
            bg="#a6e3a1", fg="#1e1e2e", font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=16, pady=6, cursor="hand2"
        )
        self.btn_send.pack(side="left")
        self.lbl_status = tk.Label(
            btn_row, text="", bg="#1e1e2e", fg="#f38ba8",
            font=("Segoe UI", 9)
        )
        self.lbl_status.pack(side="left", padx=12)

        # ── Response ─────────────────────────────────────────────────────
        self._label(self, "Response").pack(fill="x", **pad)
        self.txt_response = scrolledtext.ScrolledText(
            self, height=18, wrap=tk.WORD,
            bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
            font=("Segoe UI", 10), state=tk.DISABLED, relief=tk.FLAT,
            padx=6, pady=6,
        )
        self.txt_response.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        # ── Save button ───────────────────────────────────────────────────
        tk.Button(
            self, text="Save response to file…", command=self._save_response,
            bg="#cba6f7", fg="#1e1e2e", font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, padx=14, pady=5, cursor="hand2"
        ).pack(anchor="e", padx=12, pady=(0, 12))

    # ── Helpers ──────────────────────────────────────────────────────────

    def _pick_file(self):
        path = filedialog.askopenfilename(title="Select a file")
        if path:
            self._file_path = path
            self.lbl_file.config(text=os.path.basename(path), fg="#cdd6f4")

    def _on_send(self):
        if not self._file_path:
            messagebox.showwarning("Missing file", "Please select a file to attach.")
            return

        try:
            with open(self._file_path, "r", encoding="utf-8", errors="replace") as f:
                file_content = f.read()
        except Exception as e:
            messagebox.showerror("File error", str(e))
            return

        dec_chunks, int_chunks = split_csv_chunks_by_type(file_content)
        file_name = os.path.basename(self._file_path)

        # Build ordered work list: (system_prompt, chunk, label)
        work = []
        for i, c in enumerate(dec_chunks, 1):
            work.append((SYSTEM_PROMPT_DECLARATIVE, c, f"declarative {i}/{len(dec_chunks)}"))
        for i, c in enumerate(int_chunks, 1):
            work.append((SYSTEM_PROMPT_INTERROGATIVE, c, f"interrogative {i}/{len(int_chunks)}"))

        total = len(work)
        if total == 0:
            messagebox.showinfo("Empty file", "No data rows found in the CSV.")
            return

        self.btn_send.config(state=tk.DISABLED)
        self.lbl_status.config(
            text=f"Processing chunk 1/{total} ({dec_chunks and 'declarative' or 'interrogative'})…",
            fg="#f9e2af"
        )
        self._set_response("")

        def worker():
            results = []
            for idx, (sys_prompt, chunk, label) in enumerate(work, 1):
                self.after(0, lambda idx=idx, label=label: self.lbl_status.config(
                    text=f"Processing chunk {idx}/{total} ({label})…", fg="#f9e2af"
                ))
                answer, error = send_chunk(sys_prompt, chunk, file_name)
                if error:
                    self.after(0, self._handle_response, None, error)
                    return
                results.append(answer)

            merged = _fix_lm_output("\n\n".join(results))
            self.after(0, self._handle_response, merged, None)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_response(self, answer, error):
        self.btn_send.config(state=tk.NORMAL)
        if error:
            self.lbl_status.config(text=f"Error: {error}", fg="#f38ba8")
        else:
            self.lbl_status.config(text="Done.", fg="#a6e3a1")
            self._set_response(answer)

    def _set_response(self, text):
        self.txt_response.config(state=tk.NORMAL)
        self.txt_response.delete("1.0", tk.END)
        if text:
            self.txt_response.insert(tk.END, text)
        self.txt_response.config(state=tk.DISABLED)

    def _save_response(self):
        text = self.txt_response.get("1.0", tk.END).strip()
        if not text:
            messagebox.showinfo("Nothing to save", "The response is empty.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("Markdown", "*.md"), ("All files", "*.*")],
            title="Save response",
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            messagebox.showinfo("Saved", f"Response saved to:\n{path}")


if __name__ == "__main__":
    app = App()
    app.mainloop()
