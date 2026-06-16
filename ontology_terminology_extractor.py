"""
Ontology Terminology Extractor + LM Studio Analyser

Step 1 — Pick an ontology file and click "Extract Terminology".
         The terminology is shown in the preview and auto-saved as
         <ontology_name>_terminology.json/.txt next to the ontology.

Step 2 — Pick the generated-tests file and click "Send to LM Studio".
         The LM receives the extracted terminology + the test file and
         returns a response that is shown and can be saved.

Requires: rdflib  (pip install rdflib)
"""

import csv
import difflib
import io
import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

from rdflib import Graph, RDF, RDFS, OWL, XSD
from rdflib.namespace import SKOS
from rdflib.term import URIRef, BNode




# ── LM Studio settings ────────────────────────────────────────────────────────
LM_STUDIO_URL  = "http://127.0.0.1:1234/v1/chat/completions"
REQUEST_TIMEOUT = 1200   # seconds
CHUNK_SIZE      = 20    # test blocks per request

# ── Edit your system prompt here ──────────────────────────────────────────────
SYSTEM_PROMPT = """
You are a terminology-alignment validator. You receive two inputs:
1. **THEMIS TESTS** — a **CSV file**. It has a header row and exactly three
   columns:
   - `id` — a requirement identifier, already written in comment form
     (e.g. `//priv-1`). It already carries the `//` prefix; do not add a
     second one.
   - `Competency question` — the natural-language requirement that the
     test encodes.
   - `Generated test` — **one or more** Themis test lines. A single cell
     may hold several lines (a multi-line, quoted CSV field). Treat each
     physical line inside the cell as a separate test line.
2. **ONTOLOGY TERMINOLOGY** — a listing of the actual vocabulary of the
   target ontology, organised into sections: Classes, Object Properties,
   Data Properties, Named Individuals. Each entry may include its
   domain/range.

Your job, in two parts:

1. **Silently normalise** every name in the tests to match the
   terminology whenever a confident match exists (exact, case, has-prefix,
   shape, or semantic equivalence). Just rewrite the line. Do not flag
   the change.
2. **Alert** only in two situations:
   - The name has **no plausible counterpart** in the terminology, even
     after an exhaustive semantic search.
   - The name exists in the terminology but is used in a way that
     **conflicts with it structurally** (wrong kind, or swapped
     domain/range).

The goal is a quiet, clean output. Alerts should be the exception, not
the default. **When in doubt between alerting and rewriting, prefer to
rewrite** if a single terminology entry is clearly the best match.

---

## What to check in each test line

Every Themis line mentions one or more of these entity kinds:

| Kind | Examples |
|---|---|
| Class | `Policy`, `Rule`, `Worker`, `Resource` |
| Object property | `definesRule`, `hasAction`, `belongsToType` |
| Data property | `identifier`, `hasDescription`, `costPerHour` |
| Named individual | `Visibility`, `All`, `Friends` |

These Themis tokens are **syntax, never check them against terminology**:
`type`, `Class`, `Property`, `SubClassOf`, `some`, `only`, `min`, `max`,
`exactly`, `disjointWith`, `equivalentTo`, `characteristic`,
`symmetricProperty`, `domain`, `range`.

**Match these syntax keywords case-insensitively.** The generated tests
may write them with different casing than shown above (e.g. `subClassOf`
instead of `SubClassOf`). Recognise them as the same syntax token
regardless of case, and do not check them against the terminology.

These XSD types are **syntax, never check them as classes or flag them**:
`literal`, `string`, `integer`, `float`, `double`, `decimal`, `boolean`,
`date`, `dateTime`, `time`, `anyURI`.

Numbers in cardinality restrictions (`min 2`, `exactly 1`, etc.) are
syntax — ignore.

---

## How to identify what each token is

Use the Themis pattern to know each token's role:

| Pattern | Roles |
|---|---|
| `X type Class` | X is a **class** |
| `x type Property` | x is a **property** |
| `X SubClassOf Y` | X and Y are **classes** |
| `X SubClassOf p some Y` | X, Y are **classes**; p is an **object property** |
| `X SubClassOf p min/max/exactly N Y` | X, Y are **classes**; p is an **object property** |
| `X p Y` (Y is a class) | X, Y are **classes**; p is an **object property** |
| `X p xsdType` | X is a **class**; p is a **data property** |
| `p domain X` | p is a **property**; X is a **class** |
| `p range X` | p is a **property**; X is a **class** |
| `p range xsdType` | p is a **data property** |
| `p characteristic symmetricProperty` | p is an **object property** |
| `i type X` | if the terminology lists `i` as a named individual of `X`, treat as individual assertion; otherwise treat as class/class relation |
| `X disjointWith Y` / `X equivalentTo Y` | X, Y are **classes** |

(Pattern keywords above are matched case-insensitively, per the note in
the previous section.)

---

## Resolution procedure — semantic-first, silent unless genuinely off

**Primary goal: find the correct terminology term semantically.** Before
declaring a token "not found", exhaustively search the terminology for any
entry that expresses the same concept, relationship, or entity — even if
the surface name differs considerably. Only alert when no semantic
counterpart exists.

For each token, walk this list and stop at the first hit:

### 1. EXACT match
Identical string (case-sensitive) to an entry of the right kind.
→ Keep as-is. No change, no alert.

### 2. CASE match
Same string up to letter case.
→ **Silently rewrite** to the canonical casing. No alert.

### 3. HAS-PREFIX match
The test drops or adds a `has` prefix relative to the terminology
(e.g., test `identifier` vs terminology `hasIdentifier`, or vice versa).
→ **Silently rewrite** to the canonical form. No alert.

### 4. SHAPE match
A property in the terminology has the exact domain and range used in
the test, even though its name differs (e.g., test uses `containsRule`
from Policy to Rule; terminology has `definesRule` with domain Policy,
range Rule, and no other property matches that signature).
→ **Silently rewrite** to the terminology's property name. No alert.

### 5. SEMANTIC EQUIVALENCE — the workhorse of alignment

The token is not literally present in the terminology, but an entry
represents the **same concept** in the ontology's domain. This step
must be applied **aggressively and exhaustively** before alerting. Run
through every strategy below before concluding no match exists.

#### Search strategies (try each, in any order, until a candidate emerges)

**a. Stem and lemma.** Strip inflections and affixes. `containing`,
`contains`, `contained`, `containment` all reduce to the root `contain`.
Match roots, not surface forms.

**b. Verb ↔ noun derivation.** A test verb may correspond to a
terminology noun form, and vice versa:
- `assigns` ↔ `hasAssignment`, `hasAssignee`, `Assignment`
- `defines` ↔ `Definition`, `definesX`
- `targets` ↔ `hasTarget`, `Target`, `RuleTarget`
- `restricts` ↔ `Restriction`, `hasRestriction`
- `permits` ↔ `Permission`, `hasPermission`

**c. Embedded and compound terms.** Scan terminology names for the test
token appearing as a substring, head noun, or modifier:
- Test `Target` → terminology class `RuleTarget` (token is the head noun)
- Test `hasTarget` → terminology `hasRuleTarget` (token is a sub-phrase)
- Test `Description` → terminology data property `hasDescription`
- Test `Asset` → if terminology has `DigitalAsset`, that may be the match

**d. Synonyms and near-synonyms.** Words with the same meaning in
everyday English **or in this ontology's domain**:
- `Worker` ↔ `Employee`, `Staff`
- `contains` ↔ `defines` (in a policy/rule context)
- `forbidden`, `denies`, `disallows` ↔ `Prohibition`
- `allowed`, `permitted` ↔ `Permission`
- `Device` ↔ `Sensor` (if the ontology is about IoT)

**e. Abbreviation and expansion.** Short forms ↔ long forms:
- `ID` ↔ `Identifier`
- `Org` ↔ `Organization`
- `Desc` ↔ `Description`
- `Qty` ↔ `Quantity`

**f. Hypernym / hyponym.** The test may use a more general or more
specific term than the terminology. Accept when the broader/narrower
term is the **only plausible match** and the surrounding context
(domain, range, sibling tokens) makes the mapping unambiguous. Do NOT
force a mapping when the test term is clearly a sibling concept, not a
parent or child.

**g. Domain conventions.** If the ontology's domain uses a particular
word as a standard synonym (e.g., "concept" for "class" in OWL,
"agent" for "actor" in some upper ontologies), apply that convention.

#### Converging evidence — your strongest signal

When **semantic similarity coincides with structural signals** — the
candidate property's domain and range match the test's usage, OR the
candidate class appears as the range of another property the test also
mentions — the match is **confirmed even if string similarity is low**.
Converging evidence overrides weak lexical competitors.

Example of convergence: test has `Policy controlsRule Rule`. The
terminology has no `controlsRule`, but `definesRule` (domain Policy,
range Rule) is the unique property with this shape, and `controls` and
`defines` are domain-synonymous. Both signals point to the same target
→ silent rewrite.

#### Tie-breaking when multiple candidates remain

1. Prefer the candidate whose **domain/range matches** the test usage.
2. Prefer the candidate that shares more **morphological roots** with
   the test token.
3. Prefer the candidate that other (already-resolved) tokens on the
   same line point to (line-internal consistency).
4. Prefer the candidate whose **kind** matches what the Themis pattern
   demands (object property vs data property vs class).
5. If still tied: do **not** guess. Fall through to rule 6.

→ **Silently rewrite** to the semantically equivalent term. No alert.

### 6. LEXICAL near-match, ambiguous
High string similarity but multiple plausible candidates, or weak
semantic evidence, and tie-breaking did not pick a winner.
→ **Silently rewrite** to the best candidate if the match is reasonably
clear. Alert with a suggestion only if genuinely ambiguous.

### 7. NONE
After exhaustive search through strategies (a)–(g) and the convergence
check, nothing in the terminology represents this concept.
→ **Alert**: `⚠ <kind> `<n>` not in terminology`.

---

## Structural checks — always alert (never silently "fix")

These are conceptual errors, not naming issues. Do not try to
auto-correct them. Keep the line as written (after any silent
normalisations from the resolution procedure above) and attach an alert.

- **KIND MISMATCH** — a name exists in the terminology but under a
  different kind. E.g., the test uses `Visibility` as a class, but
  terminology lists it as a named individual of Action. The fix is
  semantic, not notational — the user must decide.
  → `⚠ `<n>` is a <actual kind> in terminology, not a <assumed kind>`

- **DOMAIN/RANGE MISMATCH** — the property exists in the terminology,
  but the test uses it with a domain or range that conflicts (most
  commonly: the roles are swapped).
  → `⚠ property `<n>` used with domain <D>, range <R>; terminology has domain <D'>, range <R'>`

Do **not** alert on `xsdType` breadth differences (e.g., terminology
`range: literal` vs test `string`). Accept the test's type silently.

---

## Alert format

All alerts are **inline end-of-line comments** on the same line as the
(possibly rewritten) test:

```
<test line>    // ⚠ <alert>
```

Multiple issues on one line are separated by `; `.
There is only one marker: `⚠`. No soft-notice marker.

---

## FEW-SHOT EXAMPLES

> **How to read these examples in CSV terms.** Each example below shows a
> test in the old line-oriented shape: a `// …` comment header followed by
> one or more test lines. Under the CSV input this maps directly:
> - the `// …` header corresponds to a row's `id` + `Competency question`
>   (rendered as `<id> — <Competency question>`), and
> - the test line(s) correspond to that row's `Generated test` cell (one
>   row's cell may contain several lines).
>
> The resolution logic the examples teach is identical; only where the
> text comes from changes.

For all examples below, assume the terminology is:

```
Classes: Action, Item, Permission (⊑ Rule), Policy, Prohibition (⊑ Rule),
         Rule, RuleTarget
Object Properties:
  definesRule    domain: Policy  range: Rule
  hasAction      domain: Rule    range: Action
  hasPolicy      domain: Item    range: Policy
  hasRuleTarget  domain: Rule    range: RuleTarget
Data Properties:
  hasDescription range: literal
  hasName        range: literal
  hasIdentifier  range: string
Named Individuals:
  Accessibility : Action
  All           : RuleTarget
  Friends       : RuleTarget
  None          : RuleTarget
  Visibility    : Action
```

### Example A — Exact match (no change, no alert)

The simplest case: every token already matches the terminology
verbatim. Pass through untouched.

Input:
```
// REQ-1 — Every policy declares one or more rules
Policy definesRule Rule
```
Output:
```
// REQ-1 — Every policy declares one or more rules
Policy definesRule Rule
```

---

### Example B — Semantic equivalence reinforced by shape (silent rewrite)

The test uses `containsRule`, which is not in the terminology. Two
independent signals point to `definesRule`: (1) "contains" and "defines"
express the same Policy→Rule relationship in this domain, and (2)
`definesRule` is the **only** property with domain Policy and range
Rule. Converging evidence → silent rewrite, no alert.

Input:
```
// REQ-2 — A policy contains one or more rules
Policy containsRule Rule
```
Output:
```
// REQ-2 — A policy contains one or more rules
Policy definesRule Rule
```

---

### Example C — Missing `has` prefix on a data property (silent rewrite)

The test writes the bare noun `description`; the terminology uses the
conventional `hasDescription`. Silent normalisation.

Input:
```
// REQ-3 — Every item carries a description
Item description literal
```
Output:
```
// REQ-3 — Every item carries a description
Item hasDescription literal
```

---

### Example D — XSD type narrowed silently (no alert)

The terminology declares `hasName range: literal` (the broadest text
type); the test narrows it to `string`. This is a refinement, not a
conflict — accept it without comment.

Input:
```
// REQ-4 — Every item has a textual name
Item hasName string
```
Output:
```
// REQ-4 — Every item has a textual name
Item hasName string
```

---

### Example E — Kind mismatch: individual used as a class (alert)

The test treats `Visibility` as a class (`SubClassOf Action`), but the
terminology lists `Visibility` as a named individual of Action. This
is a conceptual decision the validator must not silently "fix" by
inventing a class.

Input:
```
// REQ-5 — Visibility is a kind of action
Visibility SubClassOf Action
```
Output:
```
// REQ-5 — Visibility is a kind of action
Visibility SubClassOf Action    // ⚠ `Visibility` is a named individual in terminology, not a class
```

---

### Example F — Individual assertion that matches the terminology (no alert)

The Themis pattern `i type X` is ambiguous between "individual `i` is
of class `X`" and a class-level assertion. Because `Friends` is listed
in the terminology as a named individual of `RuleTarget`, this resolves
cleanly to an individual assertion.

Input:
```
// REQ-6 — Friends is one of the predefined rule targets
Friends type RuleTarget
```
Output:
```
// REQ-6 — Friends is one of the predefined rule targets
Friends type RuleTarget
```

---

### Example G — Class genuinely missing; sibling token silently fixed (alert)

Two issues, one silent and one alerted: `description` → `hasDescription`
is a routine has-prefix fix (silent), but `Resource` has no plausible
counterpart in the terminology — neither `Item` nor any other class is
semantically close enough to commit to. Alert only the class.

Input:
```
// REQ-7 — A resource has a description
Resource description literal
```
Output:
```
// REQ-7 — A resource has a description
Resource hasDescription literal    // ⚠ class `Resource` not in terminology
```

---

### Example H — Domain and range swapped (alert)

The property `hasPolicy` exists, but its declared shape is
`domain: Item, range: Policy`. The test inverts this, using
`Policy hasPolicy Item`. The validator must not silently swap operands
— this is a semantic error for the author to resolve.

Input:
```
// REQ-8 — A policy has an item it applies to
Policy hasPolicy Item
```
Output:
```
// REQ-8 — A policy has an item it applies to
Policy hasPolicy Item    // ⚠ property `hasPolicy` used with domain Policy, range Item; terminology has domain Item, range Policy
```

---

### Example I — Embedded-compound match (silent rewrite)

`hasTarget` is not in the terminology, but `hasRuleTarget` is — and it
is the unique property with domain Rule, range RuleTarget. The test
token is a substring of the canonical name (strategy 5c: embedded
terms), and the shape matches. Silent rewrite.

Input:
```
// REQ-9 — A rule has a target audience
Rule hasTarget RuleTarget
```
Output:
```
// REQ-9 — A rule has a target audience
Rule hasRuleTarget RuleTarget
```

---

### Example J — Multiple unrelated tokens missing (multiple alerts)

Neither `Resource` (class) nor `status` (data property) maps to
anything in the terminology, even after a thorough semantic sweep.
Both are alerted on the same line.

Input:
```
// REQ-10 — A resource has a status
Resource status string
```
Output:
```
// REQ-10 — A resource has a status
Resource status string    // ⚠ class `Resource` not in terminology; ⚠ data property `status` not in terminology
```

---

### Example K — Abbreviation expansion + verb-to-noun derivation (silent rewrite)

Two strategies work together. The test uses `id` (abbreviation) where
the terminology has `hasIdentifier` (strategy 5e). At the same time,
the Item/Policy connection uses `policy` as a verb-like property,
which the terminology expresses as the noun-prefixed `hasPolicy`
(strategy 5b combined with 5c). Both resolutions are unambiguous.

Input:
```
// REQ-11 — An item has a policy and an identifier
Item policy Policy
Item id string
```
Output:
```
// REQ-11 — An item has a policy and an identifier
Item hasPolicy Policy
Item hasIdentifier string
```

---

## Processing instructions

1. Parse the terminology first. Build an internal index by kind:
   - classes (set of names)
   - object properties (map: name → {domain, range})
   - data properties (map: name → {range})
   - named individuals (map: name → class)
2. Parse the THEMIS TESTS **CSV**. Skip the header row
   (`id,Competency question,Generated test`). Then process the data rows
   top to bottom. For each row:
   a. Read the three fields: `id`, `Competency question`, `Generated test`.
   b. Split `Generated test` into individual test lines on embedded
      newlines — a single cell may contain more than one test line.
   c. Trim surrounding whitespace from each test line; skip empty lines.
3. For each test line, identify the kind of each token using the
   pattern table (matching syntax keywords case-insensitively).
4. For each token, run the resolution procedure in order, applying
   strategies (a)–(g) of step 5 aggressively before considering an
   alert. Prefer converging-evidence matches over weak lexical hits.
5. Emit output as described in OUTPUT FORMAT below: one block per CSV
   row, each test line possibly rewritten and followed by any alerts.
6. Process every data row in the CSV; do not drop or reorder rows.

---

## OUTPUT FORMAT

Emit **one block per CSV row**, in the original row order. For each row:

1. A comment header that combines the row's identifier and its
   competency question:
   ```
   <id> — <Competency question>
   ```
   The `id` already carries the `//` comment prefix (e.g. `//priv-3`), so
   do not add another. The result looks like
   `//priv-3 — A policy contains rules`.
2. One line per test line in that row's `Generated test` cell, each
   possibly rewritten, each followed by any inline `⚠` alerts.
3. A blank line separating consecutive blocks.

Example shape:

```
//priv-3 — A policy contains rules
Policy definesRule Rule

//priv-7 — There are two types of action: Visibility and Accessibility
Visibility SubClassOf Action    // ⚠ `Visibility` is a named individual in terminology, not a class
Accessibility SubClassOf Action    // ⚠ `Accessibility` is a named individual in terminology, not a class

```

Counting conventions:

- "Fully matched" counts test lines that needed no rewrite AND had no
  alert.
- "Silently normalised" counts test lines that were rewritten but had no
  alert.
- If a line has both a rewrite (e.g., fixing the property) AND an alert
  (e.g., the class is missing), count it under the alert category only.

"""
# ─────────────────────────────────────────────────────────────────────────────


# ── Response parser (mirrors build_comparison.parse_txt on a string) ──────────

_HEADER_RE = re.compile(r'^(?://\s+)?(\S+)\s+[—–-]+\s*(.+)$')


def _parse_lm_response(text: str) -> list[dict]:
    """Parse LM output into blocks of {id, cq, tests:[{line, advise}]}."""
    blocks: list[dict] = []
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```") or "─── SUMMARY" in line:
            current = None
            continue
        m = _HEADER_RE.match(line)
        if m:
            req_id = m.group(1).strip()
            cq = re.split(r'\s*[►▶»>]\s*', m.group(2).strip())[0].strip()
            current = {"id": req_id, "cq": cq, "tests": []}
            blocks.append(current)
            continue
        if not line.strip():
            continue
        if current is not None and not line.lstrip().startswith("//"):
            if "//" in line:
                code_part, advice_part = line.split("//", 1)
                code, advise = code_part.strip(), advice_part.strip()
            else:
                code, advise = line.strip(), ""
            if code:
                current["tests"].append({"line": code, "advise": advise})
    return blocks


def _blocks_to_csv(blocks: list[dict]) -> str:
    """Serialise parsed blocks to CSV with Advises column."""
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["id", "Competency question", "Generated test", "Advises"],
        lineterminator="\r\n",
    )
    writer.writeheader()
    for block in blocks:
        writer.writerow({
            "id":                     block["id"].lstrip("/").strip(),
            "Competency question":    block["cq"],
            "Generated test":         "\n".join(t["line"] for t in block["tests"]),
            "Advises":                "\n".join(t["advise"] for t in block["tests"] if t["advise"]),
        })
    return buf.getvalue()


def response_to_csv(text: str) -> str:
    """Convert raw LM response text to a CSV string (for use by pipeline)."""
    return _blocks_to_csv(_parse_lm_response(text))


# ── OWL constants ─────────────────────────────────────────────────────────────
OWL_CLASS           = OWL.Class
OWL_OBJPROP         = OWL.ObjectProperty
OWL_DATAPROP        = OWL.DatatypeProperty
OWL_ANNOTPROP       = OWL.AnnotationProperty
OWL_NAMEDINDIVIDUAL = OWL.NamedIndividual

XSD_TYPE_MAP = {
    str(XSD.string):   "string",
    str(XSD.integer):  "integer",
    str(XSD.int):      "integer",
    str(XSD.float):    "float",
    str(XSD.double):   "float",
    str(XSD.boolean):  "boolean",
    str(XSD.date):     "date",
    str(XSD.dateTime): "dateTime",
    str(XSD.anyURI):   "string",
    str(XSD.decimal):  "float",
    str(XSD.long):     "integer",
}


# ── RDF helpers ───────────────────────────────────────────────────────────────

def local_name(uri: URIRef) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            return s.rsplit(sep, 1)[-1]
    return s


def best_label(g: Graph, uri: URIRef) -> str:
    for pred in (RDFS.label, SKOS.prefLabel, SKOS.altLabel):
        for obj in g.objects(uri, pred):
            if str(obj).strip():
                return str(obj).strip()
    return local_name(uri)


def xsd_label(uri) -> str:
    if uri is None:
        return "literal"
    s = str(uri)
    return XSD_TYPE_MAP.get(s, s.rsplit("#", 1)[-1] if "#" in s else "literal")


# ── Core extraction ───────────────────────────────────────────────────────────

def extract_terminology(ontology_path: str) -> dict:
    g = Graph()
    with open(ontology_path, "rb") as _f:
        _header = _f.read(512).lstrip()
    if _header.startswith(b"<?xml") or _header.startswith(b"<rdf:"):
        _fmt = "xml"
    elif _header.startswith(b"{"):
        _fmt = "json-ld"
    else:
        _fmt = None  # let rdflib detect from extension
    g.parse(ontology_path, format=_fmt)

    # Classes
    classes = {}
    for uri in g.subjects(RDF.type, OWL_CLASS):
        if isinstance(uri, BNode):
            continue
        ln = local_name(uri)
        parents  = [local_name(p) for p in g.objects(uri, RDFS.subClassOf) if isinstance(p, URIRef)]
        disjoint = [local_name(d) for d in g.objects(uri, OWL.disjointWith) if isinstance(d, URIRef)]
        classes[ln] = {"uri": str(uri), "label": best_label(g, uri),
                       "parents": parents, "disjoint": disjoint}

    for uri in g.subjects(RDF.type, RDFS.Class):
        if isinstance(uri, BNode):
            continue
        ln = local_name(uri)
        if ln not in classes:
            classes[ln] = {"uri": str(uri), "label": best_label(g, uri),
                           "parents": [], "disjoint": []}

    for s, _, o in g.triples((None, RDFS.subClassOf, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef):
            s_ln, o_ln = local_name(s), local_name(o)
            if s_ln not in classes:
                classes[s_ln] = {"uri": str(s), "label": best_label(g, s),
                                 "parents": [o_ln], "disjoint": []}
            elif o_ln not in classes[s_ln]["parents"]:
                classes[s_ln]["parents"].append(o_ln)
            if o_ln not in classes:
                classes[o_ln] = {"uri": str(o), "label": best_label(g, o),
                                 "parents": [], "disjoint": []}

    # Object Properties
    object_properties = {}
    for uri in g.subjects(RDF.type, OWL_OBJPROP):
        if isinstance(uri, BNode):
            continue
        ln = local_name(uri)
        object_properties[ln] = {
            "uri":       str(uri),
            "label":     best_label(g, uri),
            "domain":    [local_name(d) for d in g.objects(uri, RDFS.domain) if isinstance(d, URIRef)],
            "range":     [local_name(r) for r in g.objects(uri, RDFS.range)  if isinstance(r, URIRef)],
            "symmetric": (uri, RDF.type, OWL.SymmetricProperty) in g,
            "inverseOf": [local_name(c) for c in g.objects(uri, OWL.inverseOf) if isinstance(c, URIRef)],
        }

    # Data Properties
    data_properties = {}
    for uri in g.subjects(RDF.type, OWL_DATAPROP):
        if isinstance(uri, BNode):
            continue
        ln = local_name(uri)
        ranges = [xsd_label(r) for r in g.objects(uri, RDFS.range) if isinstance(r, URIRef)]
        data_properties[ln] = {
            "uri":    str(uri),
            "label":  best_label(g, uri),
            "domain": [local_name(d) for d in g.objects(uri, RDFS.domain) if isinstance(d, URIRef)],
            "range":  ranges if ranges else ["literal"],
        }

    # Annotation Properties
    annotation_properties = {}
    for uri in g.subjects(RDF.type, OWL_ANNOTPROP):
        if isinstance(uri, BNode):
            continue
        ln = local_name(uri)
        annotation_properties[ln] = {"uri": str(uri), "label": best_label(g, uri)}

    # Named Individuals
    individuals = {}
    for uri in g.subjects(RDF.type, OWL_NAMEDINDIVIDUAL):
        if isinstance(uri, BNode):
            continue
        ln = local_name(uri)
        types = [local_name(t) for t in g.objects(uri, RDF.type)
                 if isinstance(t, URIRef) and t != OWL_NAMEDINDIVIDUAL]
        individuals[ln] = {"uri": str(uri), "label": best_label(g, uri), "types": types}

    # Ontology metadata
    ontology_uri = ontology_title = None
    for s in g.subjects(RDF.type, OWL.Ontology):
        ontology_uri = str(s)
        for pred in (RDFS.label, SKOS.prefLabel):
            for obj in g.objects(s, pred):
                if str(obj).strip():
                    ontology_title = str(obj).strip()
                    break

    return {
        "ontology_uri":          ontology_uri,
        "ontology_title":        ontology_title,
        "classes":               classes,
        "object_properties":     object_properties,
        "data_properties":       data_properties,
        "annotation_properties": annotation_properties,
        "individuals":           individuals,
    }


# ── Text serialiser ───────────────────────────────────────────────────────────

def terminology_to_text(term: dict) -> str:
    lines = []

    if term["ontology_uri"]:
        title = term["ontology_title"] or term["ontology_uri"]
        lines += [f"# Ontology: {title}", ""]

    lines.append("## Classes")
    for name, info in sorted(term["classes"].items()):
        label    = f' ("{info["label"]}")' if info["label"] != name else ""
        row      = f"- {name}{label}"
        if info["parents"]:
            row += f"\n  subClassOf: {', '.join(info['parents'])}"
        if info["disjoint"]:
            row += f"\n  disjointWith: {', '.join(info['disjoint'])}"
        lines.append(row)
    lines.append("")

    lines.append("## Object Properties")
    for name, info in sorted(term["object_properties"].items()):
        label  = f' ("{info["label"]}")' if info["label"] != name else ""
        domain = ", ".join(info["domain"]) or "—"
        rng    = ", ".join(info["range"])  or "—"
        sym    = "  symmetric" if info["symmetric"] else ""
        inv    = f'  inverseOf: {", ".join(info["inverseOf"])}' if info["inverseOf"] else ""
        lines.append(f"- {name}{label}  domain: {domain}  range: {rng}{sym}{inv}")
    lines.append("")

    lines.append("## Data Properties")
    for name, info in sorted(term["data_properties"].items()):
        label  = f' ("{info["label"]}")' if info["label"] != name else ""
        domain = ", ".join(info["domain"]) or "—"
        rng    = ", ".join(info["range"])
        lines.append(f"- {name}{label}  domain: {domain}  range: {rng}")
    lines.append("")

    if term["individuals"]:
        lines.append("## Named Individuals")
        for name, info in sorted(term["individuals"].items()):
            label = f' ("{info["label"]}")' if info["label"] != name else ""
            types = f'  type: {", ".join(info["types"])}' if info["types"] else ""
            lines.append(f"- {name}{label}{types}")
        lines.append("")

    return "\n".join(lines)


# ── Test-file chunker ─────────────────────────────────────────────────────────

def split_test_chunks(tests_content: str, chunk_size: int = CHUNK_SIZE) -> list:
    """
    Split a Themis test file into chunks of `chunk_size` blocks.
    A block starts with a `// …` comment line (e.g. `// LIFT-19 —`) and is
    followed by one or more test lines. Block boundaries are detected by the
    leading `//`, so blank lines between blocks are optional.
    """
    blocks = []
    current = []

    for line in tests_content.splitlines():
        if line.startswith("//") and current:
            block = "\n".join(current).strip()
            if block:
                blocks.append(block)
            current = [line]
        else:
            current.append(line)

    if current:
        block = "\n".join(current).strip()
        if block:
            blocks.append(block)

    chunks = []
    for i in range(0, len(blocks), chunk_size):
        chunks.append("\n\n".join(blocks[i : i + chunk_size]))

    return chunks if chunks else [tests_content]


# ── Similarity pre-processing ─────────────────────────────────────────────────

SIMILARITY_THRESHOLD = 0.82   # tokens scoring above this are silently replaced

_THEMIS_KEYWORDS = {
    "type", "Class", "Property", "SubClassOf", "some", "only",
    "min", "max", "exactly", "disjointWith", "equivalentTo",
    "characteristic", "symmetricProperty", "domain", "range",
    "literal", "string", "integer", "float", "double", "decimal",
    "boolean", "date", "dateTime", "time", "anyURI",
}


def _term_candidates(terminology: dict) -> list[tuple[str, str]]:
    """All (canonical_name, kind) pairs from a terminology dict."""
    out = []
    for n in terminology["classes"]:
        out.append((n, "class"))
    for n in terminology["object_properties"]:
        out.append((n, "object_property"))
    for n in terminology["data_properties"]:
        out.append((n, "data_property"))
    for n in terminology["individuals"]:
        out.append((n, "individual"))
    return out


def _str_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _best_term_match(token: str, candidates: list[tuple[str, str]],
                     threshold: float) -> str | None:
    """Return the canonical name whose similarity to *token* exceeds *threshold*.

    Returns None when the token already matches exactly or no candidate
    clears the threshold.
    """
    for name, _ in candidates:
        if name == token:
            return None  # exact match — nothing to replace
    best_name, best_score = None, threshold
    for name, _ in candidates:
        score = _str_similarity(token, name)
        if score > best_score:
            best_score, best_name = score, name
    return best_name


def preprocess_tests_with_similarity(tests_content: str, terminology: dict,
                                     threshold: float = SIMILARITY_THRESHOLD) -> str:
    """Replace tokens in test lines with the closest terminology name when the
    string similarity exceeds *threshold*.  Comment lines and Themis keywords
    are left untouched."""
    candidates = _term_candidates(terminology)
    result = []
    for line in tests_content.splitlines():
        if line.startswith("//") or not line.strip():
            result.append(line)
            continue
        tokens = line.split()
        new_tokens = []
        for tok in tokens:
            if tok in _THEMIS_KEYWORDS:
                new_tokens.append(tok)
                continue
            try:
                float(tok)
                new_tokens.append(tok)
                continue
            except ValueError:
                pass
            replacement = _best_term_match(tok, candidates, threshold)
            new_tokens.append(replacement if replacement else tok)
        result.append(" ".join(new_tokens))
    return "\n".join(result)


# ── LM Studio call ────────────────────────────────────────────────────────────

def send_to_lm(terminology_text: str, tests_content: str, tests_filename: str):
    """Send terminology + tests to LM Studio. Returns (answer, error)."""
    user_message = (
        f"## ONTOLOGY TERMINOLOGY\n\n{terminology_text}\n\n"
        f"## GENERATED TESTS (file: {tests_filename})\n\n{tests_content}"
    )
    payload = json.dumps({
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.strip()},
            {"role": "user",   "content": user_message},
        ],
        "temperature": 0.2,
        "stream": False,
    }).encode("utf-8")

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


def process_ontology_and_tests(ontology_path: str, tests_path: str = None,
                                *, tests_content: str = None) -> str:
    """Extract terminology from ontology, validate tests, return merged LM response.

    Either *tests_path* (file path) or *tests_content* (raw text) must be supplied.
    """
    terminology = extract_terminology(ontology_path)
    terminology_txt = terminology_to_text(terminology)

    base = os.path.splitext(ontology_path)[0]
    with open(base + "_terminology.json", "w", encoding="utf-8") as f:
        json.dump(terminology, f, indent=2, ensure_ascii=False)

    if tests_content is None:
        with open(tests_path, "r", encoding="utf-8", errors="replace") as f:
            tests_content = f.read()

    tests_content = preprocess_tests_with_similarity(tests_content, terminology)

    tests_filename = os.path.basename(tests_path) if tests_path else "test_generated.csv"
    chunks = split_test_chunks(tests_content)
    total = len(chunks)

    results = []
    for i, chunk in enumerate(chunks, 1):
        print(f"  Chunk {i}/{total}…")
        answer, error = send_to_lm(terminology_txt, chunk, tests_filename)
        if error:
            raise RuntimeError(f"LM Studio error on chunk {i}: {error}")
        results.append(answer)

    return "\n\n".join(results)


# ── GUI ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Ontology Terminology Extractor + LM Analyser")
        self.geometry("860x480")
        self.configure(bg="#1e1e2e")
        self._ont_path   = None
        self._tests_path = None
        self._terminology     = None
        self._terminology_txt = None
        self._build_ui()

    # ── Widget helpers ────────────────────────────────────────────────────────

    def _label(self, parent, text):
        return tk.Label(parent, text=text, bg="#1e1e2e", fg="#cdd6f4",
                        font=("Segoe UI", 10, "bold"), anchor="w")

    def _separator(self):
        tk.Frame(self, bg="#45475a", height=1).pack(fill="x", padx=12, pady=8)

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        pad = {"padx": 12, "pady": 5}

        # ── SECTION 1: Extraction ─────────────────────────────────────────────
        self._label(self, "① Ontology file").pack(fill="x", padx=12, pady=(12, 2))

        ont_row = tk.Frame(self, bg="#1e1e2e")
        ont_row.pack(fill="x", **pad)
        self.lbl_ont = tk.Label(ont_row, text="No file selected",
                                bg="#1e1e2e", fg="#6c7086",
                                font=("Segoe UI", 10), anchor="w")
        self.lbl_ont.pack(side="left", padx=(0, 8))
        tk.Button(ont_row, text="Browse…", command=self._pick_ontology,
                  bg="#89b4fa", fg="#1e1e2e", font=("Segoe UI", 9, "bold"),
                  relief=tk.FLAT, padx=10, cursor="hand2").pack(side="right")

        ext_row = tk.Frame(self, bg="#1e1e2e")
        ext_row.pack(fill="x", **pad)
        self.btn_extract = tk.Button(
            ext_row, text="Extract & Save Terminology",
            command=self._extract,
            bg="#a6e3a1", fg="#1e1e2e", font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=16, pady=6, cursor="hand2")
        self.btn_extract.pack(side="left")
        self.lbl_ext_status = tk.Label(ext_row, text="", bg="#1e1e2e", fg="#f38ba8",
                                       font=("Segoe UI", 9))
        self.lbl_ext_status.pack(side="left", padx=12)

        self._separator()

        # ── SECTION 2: LM Studio ──────────────────────────────────────────────
        self._label(self, "② Generated tests file").pack(fill="x", padx=12, pady=(4, 2))

        tests_row = tk.Frame(self, bg="#1e1e2e")
        tests_row.pack(fill="x", **pad)
        self.lbl_tests = tk.Label(tests_row, text="No file selected",
                                  bg="#1e1e2e", fg="#6c7086",
                                  font=("Segoe UI", 10), anchor="w")
        self.lbl_tests.pack(side="left", padx=(0, 8))
        tk.Button(tests_row, text="Browse…", command=self._pick_tests,
                  bg="#89b4fa", fg="#1e1e2e", font=("Segoe UI", 9, "bold"),
                  relief=tk.FLAT, padx=10, cursor="hand2").pack(side="right")

        lm_row = tk.Frame(self, bg="#1e1e2e")
        lm_row.pack(fill="x", **pad)
        self.btn_send = tk.Button(
            lm_row, text="Send to LM Studio",
            command=self._on_send,
            bg="#fab387", fg="#1e1e2e", font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=16, pady=6, cursor="hand2")
        self.btn_send.pack(side="left")
        self.lbl_lm_status = tk.Label(lm_row, text="", bg="#1e1e2e", fg="#f38ba8",
                                      font=("Segoe UI", 9))
        self.lbl_lm_status.pack(side="left", padx=12)

        self._label(self, "LM response").pack(fill="x", padx=12, pady=(6, 2))
        self.txt_resp = scrolledtext.ScrolledText(
            self, height=14, wrap=tk.WORD,
            bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
            font=("Segoe UI", 9), state=tk.DISABLED, relief=tk.FLAT,
            padx=6, pady=6)
        self.txt_resp.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        save_row = tk.Frame(self, bg="#1e1e2e")
        save_row.pack(fill="x", padx=12, pady=(0, 12))
        tk.Button(save_row, text="Save response…", command=self._save_response,
                  bg="#cba6f7", fg="#1e1e2e", font=("Segoe UI", 9, "bold"),
                  relief=tk.FLAT, padx=14, pady=5, cursor="hand2").pack(side="right")

    # ── Ontology section ──────────────────────────────────────────────────────

    def _pick_ontology(self):
        path = filedialog.askopenfilename(
            title="Select ontology file",
            filetypes=[("OWL/RDF files", "*.owl *.rdf *.ttl *.n3 *.nt *.jsonld *.xml"),
                       ("All files", "*.*")])
        if path:
            self._ont_path = path
            self.lbl_ont.config(text=os.path.basename(path), fg="#cdd6f4")

    def _extract(self):
        if not self._ont_path:
            messagebox.showwarning("No file", "Please select an ontology file first.")
            return
        try:
            self.lbl_ext_status.config(text="Parsing…", fg="#f9e2af")
            self.update()
            self._terminology     = extract_terminology(self._ont_path)
            self._terminology_txt = terminology_to_text(self._terminology)

            # Auto-save next to the ontology
            base   = os.path.splitext(self._ont_path)[0]
            j_path = base + "_terminology.json"
            t_path = base + "_terminology.txt"
            with open(j_path, "w", encoding="utf-8") as f:
                json.dump(self._terminology, f, indent=2, ensure_ascii=False)
            with open(t_path, "w", encoding="utf-8") as f:
                f.write(self._terminology_txt)

            n_cls = len(self._terminology["classes"])
            n_op  = len(self._terminology["object_properties"])
            n_dp  = len(self._terminology["data_properties"])
            n_ind = len(self._terminology["individuals"])
            self.lbl_ext_status.config(
                text=(f"Saved — {n_cls} classes, {n_op} obj props, "
                      f"{n_dp} data props, {n_ind} individuals"),
                fg="#a6e3a1")
        except Exception as e:
            self.lbl_ext_status.config(text=f"Error: {e}", fg="#f38ba8")

    # ── LM Studio section ─────────────────────────────────────────────────────

    def _pick_tests(self):
        path = filedialog.askopenfilename(
            title="Select generated tests file",
            filetypes=[("Text files", "*.txt *.md"), ("All files", "*.*")])
        if path:
            self._tests_path = path
            self.lbl_tests.config(text=os.path.basename(path), fg="#cdd6f4")

    def _on_send(self):
        if not self._terminology_txt:
            messagebox.showwarning("No terminology",
                                   "Extract terminology first (Step 1).")
            return
        if not self._tests_path:
            messagebox.showwarning("No tests file",
                                   "Please select the generated tests file.")
            return

        try:
            with open(self._tests_path, "r", encoding="utf-8", errors="replace") as f:
                tests_content = f.read()
        except Exception as e:
            messagebox.showerror("File error", str(e))
            return

        if self._terminology:
            tests_content = preprocess_tests_with_similarity(
                tests_content, self._terminology)

        tests_filename = os.path.basename(self._tests_path)
        chunks = split_test_chunks(tests_content)
        total  = len(chunks)

        self.btn_send.config(state=tk.DISABLED)
        self.lbl_lm_status.config(text=f"Processing chunk 1/{total}…", fg="#f9e2af")
        self._set_widget_text(self.txt_resp, "")

        def worker():
            results = []
            for i, chunk in enumerate(chunks, 1):
                self.after(0, lambda i=i: self.lbl_lm_status.config(
                    text=f"Processing chunk {i}/{total}…", fg="#f9e2af"
                ))
                answer, error = send_to_lm(
                    self._terminology_txt, chunk, tests_filename)
                if error:
                    self.after(0, self._handle_lm_response, None, error)
                    return
                results.append(answer)

            merged = "\n\n".join(results)
            self.after(0, self._handle_lm_response, merged, None)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_lm_response(self, answer, error):
        self.btn_send.config(state=tk.NORMAL)
        if error:
            self.lbl_lm_status.config(text=f"Error: {error}", fg="#f38ba8")
        else:
            self.lbl_lm_status.config(text="Done.", fg="#a6e3a1")
            self._set_widget_text(self.txt_resp, answer)

    def _save_response(self):
        text = self.txt_resp.get("1.0", tk.END).strip()
        if not text:
            messagebox.showinfo("Nothing to save", "The response is empty.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("Text files", "*.txt"),
                       ("All files", "*.*")],
            title="Save LM response")
        if path:
            if path.lower().endswith(".csv"):
                blocks = _parse_lm_response(text)
                content = _blocks_to_csv(blocks)
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
            messagebox.showinfo("Saved", f"Response saved to:\n{path}")

    # ── Shared helper ─────────────────────────────────────────────────────────

    def _set_widget_text(self, widget, text):
        widget.config(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        if text:
            widget.insert(tk.END, text)
        widget.config(state=tk.DISABLED)


if __name__ == "__main__":
    app = App()
    app.mainloop()
