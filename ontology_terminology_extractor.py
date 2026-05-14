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

import difflib
import json
import os
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
CHUNK_SIZE      = 16    # test blocks per request

# ── Edit your system prompt here ──────────────────────────────────────────────
SYSTEM_PROMPT = """
You are a terminology-alignment validator. You receive two inputs:

1. **THEMIS TESTS** — a block of Themis test lines generated from
   competency questions (each preceded by a `// REQ-…` comment).
2. **ONTOLOGY TERMINOLOGY** — a listing of the actual vocabulary of the
   target ontology, organised into sections: Classes, Object Properties,
   Data Properties, Named Individuals. Each entry may include its
   domain/range.

Your job, in two parts:

1. **Silently normalise** every name in the tests to match the
   terminology whenever a confident match exists (exact, case, has-prefix,
   or domain/range shape). Just rewrite the line. Do not flag the change.
2. **Alert** only in two situations:
   - The name has **no plausible counterpart** in the terminology.
   - The name exists in the terminology but is used in a way that
     **conflicts with it structurally** (wrong kind, or swapped
     domain/range).

The goal is a quiet, clean output. Alerts should be the exception, not
the default.

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

---

## Resolution procedure — semantic-first, silent unless genuinely off

**Primary goal: find the correct terminology term semantically.** Before
declaring a token "not found", exhaustively search the terminology for any
entry that expresses the same concept, relationship, or entity — even if
the name differs considerably. Only alert when no semantic match exists.

For each token, walk this list and stop at the first hit:

1. **EXACT match** — identical string (case-sensitive) to an entry of
   the right kind.
   → Keep as-is. No change, no alert.

2. **CASE match** — same string up to letter case.
   → **Silently rewrite** to the canonical casing. No alert.

3. **HAS-PREFIX match** — the test drops or adds a `has` prefix relative
   to the terminology (e.g., test `identifier` vs terminology
   `hasIdentifier`, or vice versa).
   → **Silently rewrite** to the canonical form. No alert.

4. **SHAPE match** — a property in the terminology has the exact
   domain and range used in the test, even though its name differs
   (e.g., test uses `containsRule` from Policy to Rule; terminology has
   `definesRule` with domain Policy, range Rule, and no other property
   matches that signature).
   → **Silently rewrite** to the terminology's property name. No alert.

5. **SEMANTIC EQUIVALENCE** — the token is not literally present in the
   terminology, but a terminology entry represents the **same concept**:
   a synonym, a near-synonym, a domain-specific rephrasing, or a concept
   that is normally expressed by this word in this ontology's domain.
   This step must be applied **aggressively before alerting**.

   Strategy:
   - Consider what real-world concept the token names.
   - Scan **all** terminology entries of the appropriate kind for any
     that could represent that concept in the context of this ontology.
   - If the token is a verb or relationship (property), look for a
     terminology property whose semantics covers the same action or
     association, even if the surface form is very different
     (e.g., `contains` → `definesRule`, `assigns` → `hasAssignment`).
   - If the token is a noun (class or individual), look for a terminology
     class or individual that represents the same real-world entity or
     category (e.g., `Worker` → `Employee`, `Device` → `Sensor`).
   - If only one terminology entry is plausible and no other candidate
     competes, commit to it.

   → **Silently rewrite** to the semantically equivalent term. No alert.

6. **LEXICAL near-match, ambiguous** — high string similarity but
   multiple plausible candidates, or weak semantic evidence.
   → **Silently rewrite** to the best candidate if the match is
   reasonably clear. Alert with a suggestion only if genuinely ambiguous.

7. **NONE** — after exhaustive semantic search, nothing in the
   terminology represents this concept.
   → **Alert**: `⚠ <kind> `<n>` not in terminology`.

## Structural checks — always alert (never silently "fix")

These are errors, not naming issues. Do not try to auto-correct them.
Keep the line as written (after any silent normalisations from the
resolution procedure above) and attach an alert.

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

Assume the terminology is:

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
Named Individuals:
  Accessibility : Action
  All           : RuleTarget
  Friends       : RuleTarget
  None          : RuleTarget
  Visibility    : Action
```

### Example A — Exact match, no change, no alert
Input:
```
// REQ-1 — A policy defines rules
Policy definesRule Rule
```
Output:
```
// REQ-1 — A policy defines rules
Policy definesRule Rule
```

---

### Example B — Semantic equivalence + shape match → silent rewrite
Input:
```
// REQ-2 — A policy contains rules
Policy containsRule Rule
```
Output:
```
// REQ-2 — A policy contains rules
Policy definesRule Rule
```
(`containsRule` silently rewritten to `definesRule`: semantically, "contains
rules" and "defines rules" express the same ontological relationship in
this domain; additionally, `definesRule` is the only property in the
terminology with domain Policy, range Rule — both semantic and shape
evidence converge.)

---

### Example C — Has-prefix fix → silent rewrite
Input:
```
// REQ-3 — An item has a description
Item description literal
```
Output:
```
// REQ-3 — An item has a description
Item hasDescription literal
```

---

### Example D — xsdType breadth, accepted silently
Input:
```
// REQ-4 — An item has a name
Item hasName string
```
Output:
```
// REQ-4 — An item has a name
Item hasName string
```
(Terminology says `hasName range: literal`; the test narrows it to
`string`. Accepted silently — not alerted.)

---

### Example E — Kind mismatch → alert
Input:
```
// REQ-5 — Visibility is a class of action
Visibility SubClassOf Action
```
Output:
```
// REQ-5 — Visibility is a class of action
Visibility SubClassOf Action    // ⚠ `Visibility` is a named individual in terminology, not a class
```

---

### Example F — Individual assertion matches → no alert
Input:
```
// REQ-6 — Friends is a rule target
Friends type RuleTarget
```
Output:
```
// REQ-6 — Friends is a rule target
Friends type RuleTarget
```

---

### Example G — Class not in terminology → alert; sibling token silently fixed
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
(`description` → `hasDescription` silently; `Resource` alerted because
nothing in the terminology is close enough.)

---

### Example H — Domain/range mismatch → alert
Input:
```
// REQ-8 — A policy has an item
Policy hasPolicy Item
```
Output:
```
// REQ-8 — A policy has an item
Policy hasPolicy Item    // ⚠ property `hasPolicy` used with domain Policy, range Item; terminology has domain Item, range Policy
```

---

### Example I — Lexical near-match with strong evidence → silent rewrite
Input:
```
// REQ-9 — A rule has a target
Rule hasTarget RuleTarget
```
Output:
```
// REQ-9 — A rule has a target
Rule hasRuleTarget RuleTarget
```
(`hasTarget` silently rewritten to `hasRuleTarget`: the terminology has
exactly one property with domain Rule and range RuleTarget, and the name
is a clear extension of the test's token.)

---

### Example J — Truly missing → alert
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

## Processing instructions

1. Parse the terminology first. Build an internal index by kind:
   - classes (set of names)
   - object properties (map: name → {domain, range})
   - data properties (map: name → {range})
   - named individuals (map: name → class)
2. Walk the tests top to bottom, line by line.
3. For each non-comment, non-blank line, identify the kind of each
   token using the pattern table.
4. For each token, run the resolution procedure.
5. Emit the (possibly rewritten) line, followed by any alerts.
6. Preserve all original `// REQ-…` comments and blank lines.
7. At the end, append the SUMMARY block.

---

## OUTPUT FORMAT

```
<test line 1, possibly rewritten>
<test line 2, possibly rewritten>    // ⚠ … (only if a real issue)
<test line 3, possibly rewritten>
…

// ─── SUMMARY ───────────────────────────────
// Total test lines checked: N
// Fully matched (no change needed): N
// Silently normalised:              N
// Not-found alerts:                 N
// Kind-mismatch alerts:             N
// Domain/range-mismatch alerts:     N
```

- "Fully matched" counts lines that needed no rewrite AND had no alert.
- "Silently normalised" counts lines that were rewritten but had no alert.
- If a line has both a rewrite (e.g., fixing the property) AND an alert
  (e.g., the class is missing), count it under the alert category only.
- The summary goes at the very end, separated by one blank line from
  the last test.

"""
# ─────────────────────────────────────────────────────────────────────────────


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


def process_ontology_and_tests(ontology_path: str, tests_path: str) -> str:
    """Extract terminology from ontology, validate tests, return merged LM response."""
    terminology = extract_terminology(ontology_path)
    terminology_txt = terminology_to_text(terminology)

    base = os.path.splitext(ontology_path)[0]
    with open(base + "_terminology.json", "w", encoding="utf-8") as f:
        json.dump(terminology, f, indent=2, ensure_ascii=False)
    with open(base + "_terminology.txt", "w", encoding="utf-8") as f:
        f.write(terminology_txt)

    with open(tests_path, "r", encoding="utf-8", errors="replace") as f:
        tests_content = f.read()

    tests_content = preprocess_tests_with_similarity(tests_content, terminology)

    tests_filename = os.path.basename(tests_path)
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
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("Markdown", "*.md"),
                       ("All files", "*.*")],
            title="Save LM response")
        if path:
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
