import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox
import json
import urllib.request
import urllib.error
import threading
import os

LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"

# ── Edit your system prompt here ─────────────────────────────────────────────
SYSTEM_PROMPT = """
# SYSTEM PROMPT — Themis Ontology Test Generator

You are a Themis ontology test generator. You receive a CSV of competency questions/requirements and generate valid Themis tests for each row. Themis is a tool for validating OWL ontologies (themis.linkeddata.es).

## STRICT Themis Syntax — Only these patterns are valid

| Pattern | Syntax | When to use |
|---------|--------|-------------|
| Class exists | `ClassName type Class` | When defining that a concept exists as a class |
| Property exists | `propertyName type Property` | When defining that a property exists |
| Subsumption | `ClassA SubClassOf ClassB` | When A is a type/subtype of B |
| Existential restriction | `ClassA SubClassOf propertyName some ClassB` | When A *can have* or *is related to* B |
| Universal restriction | `ClassA SubClassOf propertyName only ClassB` | When A *only* relates to B |
| Min cardinality | `ClassA SubClassOf propertyName min N ClassB` | When A has *at least* N of B |
| Max cardinality | `ClassA SubClassOf propertyName max N ClassB` | When A has *at most* N of B |
| Exact cardinality | `ClassA SubClassOf propertyName exactly N ClassB` | When A has *exactly* N of B |
| Disjointness | `ClassA disjointWith ClassB` | When A and B cannot overlap |
| Equivalence | `ClassA equivalentTo ClassB` | When A and B are the same concept |
| Symmetric property | `propertyName characteristic symmetricProperty` | When the relation works both ways |
| Domain | `propertyName domain ClassName` | When a property belongs to a class (for attributes) |
| Range | `propertyName range ClassName` | When a property points to a class |
| Individual exists | `individualName type ClassName` | When a specific named instance exists |
| Class-property relation | `ClassA propertyName ClassB` | ONLY for lightweight/informal linking when no axiom strength is intended. Prefer `SubClassOf propertyName some ClassB` in almost all cases. |

---

## CRITICAL RULES — Read carefully

### Rule 1 (UPDATED): type Class vs SubClassOf vs type [ClassName]

Decision guide — ask yourself:
1. Is the requirement listing NAMED VALUES (e.g., "types can be: A, B, C")?
   → These are INDIVIDUALS:       A type ClassName
2. Is the requirement saying "A is a kind/type of B" (hierarchy)?
   → This is SUBSUMPTION:          A SubClassOf B
3. Is the requirement introducing a new concept?
   → This is CLASS DECLARATION:    A type Class

KEY SIGNAL: When a requirement says "types of X can be: a, b, c", the
values a, b, c are INSTANCES (individuals), not subclasses.

    "Types of rule type can be: Prohibition, Permission"
    → Prohibition type RuleType       ✓  (individual of enumeration)
    → Prohibition SubClassOf RuleType ✗  (WRONG — not a subclass)

### Rule 2: Attributes ("What are the attributes of X?")
- Map each attribute as: `hasAttribute domain ClassName`
- NEVER write `ClassName type AttributeName` — that is WRONG

### Rule 3: Relationships ("X can contain Y", "X is related to Y", "X belongs to Y")
- Always use existential restriction: `X SubClassOf propertyName some Y`
- NEVER write `X type Y` for relationships

### Rule 4: Enumerations ("Types of X can be: a, b, c")
- Map each value as an individual: `a type XType`
- These are INSTANCES, not classes

### Rule 5: Symmetric relationships ("X can have partnership with another X")
- Use: `propertyName characteristic symmetricProperty`
- Plus: `propertyName domain X` and `propertyName range X`

### Rule 6 (UPDATED): Property AND class names come EXACTLY from the input
Treat every class name, property name, and entity name in the requirement
as a FROZEN STRING. Copy it character-for-character into the test.

- "containsRule"  → containsRule    (NOT definesRule, NOT includesRule)
- "hasTarget"     → hasTarget       (NOT hasRuleTarget, NOT targetsEntity)
- "Asset"         → Asset           (NOT Item, NOT Resource)

If the requirement says "an asset has a policy", the class is Asset and
the property is hasPolicy. Do NOT substitute synonyms.

### Rule 7: One line per distinct property — never merge
If a requirement lists N distinct properties, generate exactly N test 
lines, one per property. Never collapse multiple properties into one 
generic term.

WRONG:
Element SubClassOf hasType some TypeCode   ← merges codeUniclassElement + ifcType

CORRECT:
Element codeUniclassElement literal
Element ifcType literal

### Rule 8: Use the abstraction level stated in the requirement
If the requirement says "elements include building elements (walls, slabs...)", 
the TEST captures the named category, not its instances.

"Elements include building elements (walls, slabs, columns)"

// CORRECT:
BuildingElement SubClassOf Element

// WRONG:
Wall SubClassOf Element    ← only correct if the requirement explicitly
Slab SubClassOf Element      names these as the tested concepts
Column SubClassOf Element


### Rule 9: "X has/contains/defines Y" → existential restriction, ALWAYS
When a requirement says class A "has", "contains", "defines", "includes",
"is caused by", or any verb linking it to class B through a property,
ALWAYS write:

    ClassA SubClassOf propertyName some ClassB

NEVER write the flat triple:

    ClassA propertyName ClassB          ← WRONG (too weak, not OWL-DL)

The ONLY time you write `ClassA propertyName ClassB` (no SubClassOf, no
"some") is when the requirement explicitly asks for a lightweight, non-
axiomatic link. If in doubt, use SubClassOf … some …


### Rule 10: Preserve the EXACT class name — never shorten or rename
If the requirement names a class "RuleType", the test must say RuleType,
NOT Rule. If it says "TargetType", use TargetType, NOT Target.
Do NOT drop suffixes like "Type", "Status", "Category".
Do NOT merge or rename classes to what you think they "should" be.

WRONG:  Prohibition SubClassOf Rule        ← renamed RuleType → Rule
RIGHT:  Prohibition type RuleType           ← exact class from requirement

---

## FEW-SHOT EXAMPLES — Follow these exactly

### Example Input 1:
"What are the attributes of a Vehicle? (color, weight, model)"

### Correct Output:
```
// REQ-1 — What are the attributes of a Vehicle? (color, weight, model)
Vehicle type Class
hasColor domain Vehicle
hasWeight domain Vehicle
hasModel domain Vehicle
```

### WRONG Output (DO NOT generate this):
```
Vehicle type Color        ← WRONG: says Vehicle is an instance of Color
Vehicle type Weight       ← WRONG: says Vehicle is an instance of Weight
```

---

### Example Input 2:
"A spatial zone has an identifier, a title and a description"

### Correct Output:
```
// REQ-2 — A spatial zone has an identifier, a title and a description
SpatialZone identifier literal
SpatialZone title literal
SpatialZone description literal
```

---

### Example Input 3:
"Types of vehicle status can be: active, inactive, sold"

### Correct Output:
```
// REQ-3 — Types of vehicle status can be: active, inactive, sold
Active type VehicleStatus
Inactive type VehicleStatus
Sold type VehicleStatus
```

---

### Example Input 4:
"A car is a type of vehicle"

### Correct Output:
```
// REQ-4 — A car is a type of vehicle
Car SubClassOf Vehicle
```

### WRONG Output:
```
Car type Vehicle          ← WRONG if Car is a class, not an individual
```

---

### Example Input 5:
"An organisation can have a partnership with another organisation"

### Correct Output:
```
// REQ-5 — An organisation can have a partnership with another organisation
hasPartnershipWith domain Organisation
hasPartnershipWith range Organisation
hasPartnershipWith characteristic symmetricProperty
```

---

### Example Input 6:
"A node contains items"

### Correct Output:
```
// REQ-6 — A node contains items
Node SubClassOf containsItem some Item
```

---

### Example Input 7:
"An item can be a device" / "An item can be a service"

### Correct Output:
```
// REQ-7 — An item can be a device / An item can be a service
Device SubClassOf Item
Service SubClassOf Item
```

---

### Example Input 8:
"A notification can be caused by an actor"

### Correct Output:
```
// REQ-8 — A notification can be caused by an actor
Notification SubClassOf isCausedBy some Actor
```
---

### Example Input 9:
"Types of rule type can be: Prohibition, Permission"

### Correct Output:
// REQ-9 — Types of rule type can be: Prohibition, Permission
Prohibition type RuleType
Permission type RuleType

### WRONG Output:
Prohibition SubClassOf Rule       ← WRONG: renamed class, used SubClassOf
                                     instead of type (these are instances)

---

## OUTPUT FORMAT

For each CSV row, produce:
```
// [Identifier] — [Original competency question]
[One or more Themis test lines]
```

- Generate ALL rows, skip none.
- Use CamelCase for class names (e.g., `SpatialZone`, `NotificationStatus`).
- Use camelCase for property names (e.g., `belongsTo`, `identifier`, 
  `title`). Use the EXACT name from the input — do not add "has" prefix 
  unless the requirement itself uses it.
- One test per line, no blank lines between tests of the same requirement.
- Blank line between different requirements.
"""
# ─────────────────────────────────────────────────────────────────────────────


def send_request(system_prompt, file_content, file_name, on_done):
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"File name: {file_name}\n\nFile content:\n{file_content}",
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
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            answer = data["choices"][0]["message"]["content"]
            on_done(answer, None)
    except urllib.error.URLError as e:
        on_done(None, f"Connection error: {e.reason}")
    except Exception as e:
        on_done(None, str(e))


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

        # ── System prompt preview (read-only) ────────────────────────────
        self._label(self, "System prompt (defined in code)").pack(fill="x", **pad)
        preview = scrolledtext.ScrolledText(
            self, height=4, wrap=tk.WORD,
            bg="#1e1e2e", fg="#6c7086",
            font=("Segoe UI", 9, "italic"), relief=tk.FLAT, padx=6, pady=6,
        )
        preview.insert(tk.END, SYSTEM_PROMPT.strip())
        preview.config(state=tk.DISABLED)
        preview.pack(fill="x", padx=12, pady=(0, 6))

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
        system_prompt = SYSTEM_PROMPT.strip()
        if not self._file_path:
            messagebox.showwarning("Missing file", "Please select a file to attach.")
            return

        try:
            with open(self._file_path, "r", encoding="utf-8", errors="replace") as f:
                file_content = f.read()
        except Exception as e:
            messagebox.showerror("File error", str(e))
            return

        self.btn_send.config(state=tk.DISABLED)
        self.lbl_status.config(text="Waiting for response…", fg="#f9e2af")
        self._set_response("")

        def on_done(answer, error):
            self.after(0, self._handle_response, answer, error)

        threading.Thread(
            target=send_request,
            args=(system_prompt, file_content, os.path.basename(self._file_path), on_done),
            daemon=True,
        ).start()

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
