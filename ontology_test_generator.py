"""
Ontology → Themis Test Generator
----------------------------------
Automatically generates ALL possible Themis tests from an OWL ontology.
No competency questions required — tests are derived purely from
ontology axioms: class declarations, hierarchy, disjointness, equivalences,
object/data properties (with domain/range), OWL restrictions (existential,
universal, cardinality, hasValue), named individuals, and symmetric properties.

Themis reference: https://themis.linkeddata.es/howto.html

Usage (CLI):
    python ontology_test_generator.py <ontology.ttl> [output.txt]

Usage (GUI):
    python ontology_test_generator.py

Requires: rdflib  (pip install rdflib)
"""

import os
import sys
import argparse
from itertools import combinations

from rdflib import Graph, RDF, RDFS, OWL, XSD
from rdflib.namespace import SKOS
from rdflib.term import URIRef, BNode, Literal

# ── Namespace constants ───────────────────────────────────────────────────────

OWL_CLASS        = OWL.Class
OWL_OBJPROP      = OWL.ObjectProperty
OWL_DATAPROP     = OWL.DatatypeProperty
OWL_ANNOTPROP    = OWL.AnnotationProperty
OWL_NAMEDIND     = OWL.NamedIndividual

# ── Helper utilities ──────────────────────────────────────────────────────────

def local_name(uri: URIRef) -> str:
    """Return the local fragment (after # or last /) of a URI."""
    s = str(uri)
    for sep in ("#", "/", ":"):
        pos = s.rfind(sep)
        if 0 < pos < len(s) - 1:
            return s[pos + 1:]
    return s


def xsd_label(uri: URIRef) -> str:
    """Map an XSD datatype URI to the Themis-friendly label."""
    ln = local_name(uri).lower()
    _map = {
        "string":                 "string",
        "normalizedstring":       "string",
        "token":                  "string",
        "language":               "string",
        "name":                   "string",
        "ncname":                 "string",
        "langstring":             "string",
        "integer":                "integer",
        "int":                    "integer",
        "long":                   "integer",
        "short":                  "integer",
        "byte":                   "integer",
        "nonnegativeinteger":     "integer",
        "positiveinteger":        "integer",
        "nonpositiveinteger":     "integer",
        "negativeinteger":        "integer",
        "unsignedlong":           "integer",
        "unsignedint":            "integer",
        "unsignedshort":          "integer",
        "unsignedbyte":           "integer",
        "decimal":                "float",
        "float":                  "float",
        "double":                 "float",
        "boolean":                "boolean",
        "date":                   "date",
        "datetime":               "dateTime",
        "datetimestamp":          "dateTime",
        "time":                   "dateTime",
        "gyear":                  "date",
        "gyearmonth":             "date",
        "duration":               "duration",
        "anyuri":                 "literal",
        "hexbinary":              "literal",
        "base64binary":           "literal",
    }
    return _map.get(ln, "literal")


def _rdf_list(g: Graph, node) -> list:
    """Traverse an rdf:List (rdf:first / rdf:rest) and return its items."""
    result = []
    visited = set()
    current = node
    while current and current != RDF.nil:
        if current in visited:
            break
        visited.add(current)
        first = g.value(current, RDF.first)
        if first is not None:
            result.append(first)
        current = g.value(current, RDF.rest)
    return result


def _detect_own_prefixes(g: Graph) -> list:
    """
    Return a list of namespace prefixes that belong to the ontology itself
    (i.e. the namespace of the owl:Ontology declaration), excluding standard
    vocabularies (RDF, RDFS, OWL, XSD, SKOS, DC, schema.org …).

    When the list is empty the caller should NOT filter — the file declares
    no Ontology resource, so we accept everything.
    """
    skip = (
        "http://www.w3.org/",
        "http://purl.org/dc/",
        "http://schema.org/",
        "http://xmlns.com/foaf/",
        "http://www.opengis.net/",
        "http://www.geonames.org/",
    )
    own = []
    for s in g.subjects(RDF.type, OWL.Ontology):
        s_str = str(s)
        if any(s_str.startswith(p) for p in skip):
            continue
        # Derive base namespace: everything up to (and including) the last # or /
        for ch in ("#", "/"):
            pos = s_str.rfind(ch)
            if pos != -1:
                candidate = s_str[: pos + 1]
                if candidate not in own:
                    own.append(candidate)
                break
    return own


# ── Main generator ────────────────────────────────────────────────────────────

def generate_tests(ontology_path: str, local_only: bool = True) -> str:
    """
    Parse *ontology_path* and return a string of all possible Themis tests.

    Parameters
    ----------
    local_only : bool
        When True (default) only terms whose URI starts with one of the
        ontology's own namespace prefixes are included for existence tests
        (class / property / individual declarations).  Relationship tests
        (SubClassOf, domain, range, restrictions) are still generated when
        at least one endpoint is local.
        When False every named term found in the file is included.
    """
    g = Graph()
    # Try rdflib auto-detection first; fall back through common formats
    _parsed = False
    for fmt in (None, "xml", "turtle", "n3", "json-ld", "nt"):
        try:
            g = Graph()
            if fmt is None:
                g.parse(ontology_path)
            else:
                g.parse(ontology_path, format=fmt)
            _parsed = True
            break
        except Exception:
            continue
    if not _parsed:
        raise ValueError(f"Could not parse {ontology_path} — unsupported format.")

    own_prefixes = _detect_own_prefixes(g) if local_only else []

    def is_local(uri: URIRef) -> bool:
        if not own_prefixes:
            return True
        s = str(uri)
        return any(s.startswith(p) for p in own_prefixes)

    # ── collect section lines ──────────────────────────────────────────────────
    # Each section = (title: str, lines: list[str])
    # lines are raw Themis lines; blank strings separate test groups.
    sections: list[tuple[str, list[str]]] = []

    # Track emitted test lines to avoid duplicates
    seen: set[str] = set()

    def emit(lines_list: list[str], *test_lines: str) -> None:
        """Append test_lines to lines_list, deduplicating on the non-comment lines."""
        payload = [l for l in test_lines if not l.startswith("//") and l != ""]
        key = "\n".join(payload)
        if key in seen or not payload:
            return
        seen.add(key)
        lines_list.extend(payload)
        lines_list.append("")

    # ── 1. Class declarations ─────────────────────────────────────────────────
    cls_lines: list[str] = []

    # Gather all named classes mentioned in the file
    all_class_uris: set[URIRef] = set()
    for uri in g.subjects(RDF.type, OWL_CLASS):
        if isinstance(uri, URIRef):
            all_class_uris.add(uri)
    for uri in g.subjects(RDF.type, RDFS.Class):
        if isinstance(uri, URIRef):
            all_class_uris.add(uri)
    # Also pick up classes only visible as subClassOf endpoints
    for s, _, o in g.triples((None, RDFS.subClassOf, None)):
        if isinstance(s, URIRef):
            all_class_uris.add(s)
        if isinstance(o, URIRef):
            all_class_uris.add(o)

    for uri in sorted(all_class_uris, key=lambda u: local_name(u).lower()):
        if not is_local(uri):
            continue
        ln = local_name(uri)
        emit(cls_lines,
             f"// [Class] {ln}",
             f"{ln} type Class")

    if cls_lines:
        sections.append(("Class Declarations", cls_lines))

    # ── 2. Class hierarchy (SubClassOf – named class to named class) ──────────
    hier_lines: list[str] = []

    for s, _, o in sorted(g.triples((None, RDFS.subClassOf, None)),
                           key=lambda t: (local_name(t[0]), local_name(t[2]))):
        if not isinstance(s, URIRef) or not isinstance(o, URIRef):
            continue
        if not (is_local(s) or is_local(o)):
            continue
        s_ln, o_ln = local_name(s), local_name(o)
        emit(hier_lines,
             f"// [SubClassOf] {s_ln} → {o_ln}",
             f"{s_ln} SubClassOf {o_ln}")

    if hier_lines:
        sections.append(("Class Hierarchy", hier_lines))

    # ── 3. Equivalent classes ─────────────────────────────────────────────────
    equiv_lines: list[str] = []
    seen_equiv: set[frozenset] = set()

    for s, _, o in g.triples((None, OWL.equivalentClass, None)):
        if not isinstance(s, URIRef) or not isinstance(o, URIRef):
            continue
        pair = frozenset([str(s), str(o)])
        if pair in seen_equiv:
            continue
        seen_equiv.add(pair)
        s_ln, o_ln = local_name(s), local_name(o)
        emit(equiv_lines,
             f"// [Equivalent classes] {s_ln} ≡ {o_ln}",
             f"{s_ln} equivalentTo {o_ln}")

    if equiv_lines:
        sections.append(("Equivalent Classes", equiv_lines))

    # ── 4. Disjoint classes ───────────────────────────────────────────────────
    disj_lines: list[str] = []
    seen_disj: set[frozenset] = set()

    def _add_disjoint(a: URIRef, b: URIRef) -> None:
        pair = frozenset([str(a), str(b)])
        if pair in seen_disj:
            return
        seen_disj.add(pair)
        a_ln, b_ln = local_name(a), local_name(b)
        emit(disj_lines,
             f"// [DisjointWith] {a_ln} ∩ {b_ln} = ∅",
             f"{a_ln} disjointWith {b_ln}")

    for s, _, o in g.triples((None, OWL.disjointWith, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef):
            _add_disjoint(s, o)

    for node in g.subjects(RDF.type, OWL.AllDisjointClasses):
        members_bnode = g.value(node, OWL.members)
        if members_bnode:
            members = [m for m in _rdf_list(g, members_bnode) if isinstance(m, URIRef)]
            for a, b in combinations(members, 2):
                _add_disjoint(a, b)

    if disj_lines:
        sections.append(("Disjoint Classes", disj_lines))

    # ── 5. Object properties ──────────────────────────────────────────────────
    obj_lines: list[str] = []

    for prop in sorted(g.subjects(RDF.type, OWL_OBJPROP),
                       key=lambda u: local_name(u).lower()):
        if isinstance(prop, BNode):
            continue
        if not is_local(prop):
            continue

        p_ln = local_name(prop)
        domains = [local_name(d) for d in g.objects(prop, RDFS.domain)
                   if isinstance(d, URIRef)]
        ranges  = [local_name(r) for r in g.objects(prop, RDFS.range)
                   if isinstance(r, URIRef)]
        is_sym  = (prop, RDF.type, OWL.SymmetricProperty) in g

        # Existence test
        emit(obj_lines,
             f"// [ObjectProperty] {p_ln} — existence",
             f"{p_ln} type Property")

        if is_sym:
            # Symmetric pattern: domain + range + characteristic
            sym_lines_inner: list[str] = [f"// [ObjectProperty] {p_ln} — symmetric"]
            if domains:
                sym_lines_inner.append(f"{p_ln} domain {domains[0]}")
            if ranges:
                sym_lines_inner.append(f"{p_ln} range {ranges[0]}")
            sym_lines_inner.append(f"{p_ln} characteristic symmetricProperty")
            emit(obj_lines, *sym_lines_inner)
        else:
            if domains and ranges:
                for d in domains:
                    for r in ranges:
                        emit(obj_lines,
                             f"// [ObjectProperty] {p_ln} — domain→range",
                             f"{d} {p_ln} {r}")
            elif domains:
                for d in domains:
                    emit(obj_lines,
                         f"// [ObjectProperty] {p_ln} — domain",
                         f"{p_ln} domain {d}")
            elif ranges:
                for r in ranges:
                    emit(obj_lines,
                         f"// [ObjectProperty] {p_ln} — range",
                         f"{p_ln} range {r}")

        # inverseOf
        for inv in g.objects(prop, OWL.inverseOf):
            if isinstance(inv, URIRef):
                inv_ln = local_name(inv)
                # inverseOf is not a Themis pattern per se, but we can
                # represent it as a domain/range swap comment
                emit(obj_lines,
                     f"// [ObjectProperty] {p_ln} — inverseOf {inv_ln}",
                     f"{inv_ln} type Property")

    if obj_lines:
        sections.append(("Object Properties", obj_lines))

    # ── 6. Data properties ────────────────────────────────────────────────────
    data_lines: list[str] = []

    for prop in sorted(g.subjects(RDF.type, OWL_DATAPROP),
                       key=lambda u: local_name(u).lower()):
        if isinstance(prop, BNode):
            continue
        if not is_local(prop):
            continue

        p_ln   = local_name(prop)
        domains = [local_name(d) for d in g.objects(prop, RDFS.domain)
                   if isinstance(d, URIRef)]
        ranges  = [xsd_label(r) for r in g.objects(prop, RDFS.range)
                   if isinstance(r, URIRef)]

        # Existence test
        emit(data_lines,
             f"// [DatatypeProperty] {p_ln} — existence",
             f"{p_ln} type Property")

        if domains and ranges:
            for d in domains:
                for r in ranges:
                    emit(data_lines,
                         f"// [DatatypeProperty] {p_ln} — {d} → {r}",
                         f"{d} {p_ln} {r}")
        elif domains:
            for d in domains:
                emit(data_lines,
                     f"// [DatatypeProperty] {p_ln} — domain",
                     f"{p_ln} domain {d}")
        elif ranges:
            for r in set(ranges):
                emit(data_lines,
                     f"// [DatatypeProperty] {p_ln} — range",
                     f"{p_ln} range {r}")

    if data_lines:
        sections.append(("Data Properties", data_lines))

    # ── 7. OWL restrictions on classes ───────────────────────────────────────
    restr_lines: list[str] = []

    for cls, _, bnode in g.triples((None, RDFS.subClassOf, None)):
        if not isinstance(cls, URIRef) or not isinstance(bnode, BNode):
            continue
        if not is_local(cls):
            continue

        # A BNode restriction must have owl:onProperty
        prop = g.value(bnode, OWL.onProperty)
        if prop is None or not isinstance(prop, URIRef):
            continue

        cls_ln = local_name(cls)
        p_ln   = local_name(prop)

        def filler(node) -> str | None:
            if not isinstance(node, URIRef):
                return None
            # XSD datatypes used as data-range fillers → use Themis XSD label
            if str(node).startswith(str(XSD)):
                return xsd_label(node)
            return local_name(node)

        # someValuesFrom  →  two tests:
        #   1. Flat triple (primary, matches CQ style: "A has B" → A prop B)
        #   2. OWL-DL restriction form (preserves the axiom-level intent)
        svf = g.value(bnode, OWL.someValuesFrom)
        if svf is not None:
            f = filler(svf)
            if f:
                is_xsd_range = str(svf).startswith(str(XSD))
                if is_xsd_range:
                    # Data range restriction → data attribute pattern
                    emit(restr_lines,
                         f"// [Data restriction] {cls_ln} {p_ln} {f}  (someValuesFrom)",
                         f"{cls_ln} {p_ln} {f}")
                else:
                    # Object range → flat triple (primary CQ pattern)
                    emit(restr_lines,
                         f"// [Relation restriction] {cls_ln} {p_ln} {f}  (someValuesFrom)",
                         f"{cls_ln} {p_ln} {f}")
                    # Also emit the OWL-DL form for complete coverage
                    emit(restr_lines,
                         f"// [Existential restriction] {cls_ln} SubClassOf {p_ln} some {f}",
                         f"{cls_ln} SubClassOf {p_ln} some {f}")

        # allValuesFrom   →  flat triple + OWL-DL form
        avf = g.value(bnode, OWL.allValuesFrom)
        if avf is not None:
            f = filler(avf)
            if f:
                is_xsd_range = str(avf).startswith(str(XSD))
                if not is_xsd_range:
                    emit(restr_lines,
                         f"// [Relation restriction] {cls_ln} {p_ln} {f}  (allValuesFrom)",
                         f"{cls_ln} {p_ln} {f}")
                emit(restr_lines,
                     f"// [Universal restriction] {cls_ln} SubClassOf {p_ln} only {f}",
                     f"{cls_ln} SubClassOf {p_ln} only {f}")

        # Cardinality (unqualified)
        for owl_pred, keyword in (
            (OWL.minCardinality, "min"),
            (OWL.maxCardinality, "max"),
            (OWL.cardinality,    "exactly"),
        ):
            n = g.value(bnode, owl_pred)
            if n is not None:
                emit(restr_lines,
                     f"// [Cardinality {keyword}] {cls_ln} SubClassOf {p_ln} {keyword} {int(n)}",
                     f"{cls_ln} SubClassOf {p_ln} {keyword} {int(n)} Thing")

        # Qualified cardinality
        for owl_pred, keyword in (
            (OWL.minQualifiedCardinality, "min"),
            (OWL.maxQualifiedCardinality, "max"),
            (OWL.qualifiedCardinality,    "exactly"),
        ):
            n = g.value(bnode, owl_pred)
            if n is not None:
                on_class = g.value(bnode, OWL.onClass) or g.value(bnode, OWL.onDataRange)
                f = filler(on_class) if on_class else "Thing"
                # Flat triple first (primary CQ pattern)
                if f and f != "Thing" and not (on_class and str(on_class).startswith(str(XSD))):
                    emit(restr_lines,
                         f"// [Relation restriction] {cls_ln} {p_ln} {f}  ({keyword} {int(n)})",
                         f"{cls_ln} {p_ln} {f}")
                emit(restr_lines,
                     f"// [Cardinality {keyword}] {cls_ln} SubClassOf {p_ln} {keyword} {int(n)} {f}",
                     f"{cls_ln} SubClassOf {p_ln} {keyword} {int(n)} {f}")

        # hasValue  →  flat class-property-individual assertion
        hv = g.value(bnode, OWL.hasValue)
        if hv is not None and isinstance(hv, URIRef):
            hv_ln = local_name(hv)
            emit(restr_lines,
                 f"// [HasValue restriction] {cls_ln} {p_ln} {hv_ln}",
                 f"{cls_ln} {p_ln} {hv_ln}")

    if restr_lines:
        sections.append(("OWL Restrictions", restr_lines))

    # ── 8. Named individuals ──────────────────────────────────────────────────
    ind_lines: list[str] = []

    for ind in sorted(g.subjects(RDF.type, OWL_NAMEDIND),
                      key=lambda u: local_name(u).lower()):
        if isinstance(ind, BNode):
            continue
        if not is_local(ind):
            continue

        ind_ln = local_name(ind)
        types = sorted(
            local_name(t)
            for t in g.objects(ind, RDF.type)
            if isinstance(t, URIRef) and t not in (OWL_NAMEDIND, OWL_CLASS, RDFS.Class)
        )
        if types:
            for t in types:
                emit(ind_lines,
                     f"// [Individual] {ind_ln} : {t}",
                     f"{ind_ln} type {t}")
        else:
            emit(ind_lines,
                 f"// [Individual] {ind_ln}",
                 f"{ind_ln} type Thing")

    if ind_lines:
        sections.append(("Named Individuals", ind_lines))

    # ── 9. Symmetric properties (declared via rdf:type owl:SymmetricProperty) ─
    sym_lines: list[str] = []

    for prop in sorted(g.subjects(RDF.type, OWL.SymmetricProperty),
                       key=lambda u: local_name(u).lower()):
        if isinstance(prop, BNode):
            continue
        if not is_local(prop):
            continue
        # Already handled inside section 5 — skip to avoid duplication
        # (only add here props that were NOT declared as owl:ObjectProperty)
        if (prop, RDF.type, OWL_OBJPROP) in g:
            continue

        p_ln    = local_name(prop)
        domains = [local_name(d) for d in g.objects(prop, RDFS.domain)
                   if isinstance(d, URIRef)]
        ranges  = [local_name(r) for r in g.objects(prop, RDFS.range)
                   if isinstance(r, URIRef)]

        sym_inner = [f"// [SymmetricProperty] {p_ln}"]
        if domains:
            sym_inner.append(f"{p_ln} domain {domains[0]}")
        if ranges:
            sym_inner.append(f"{p_ln} range {ranges[0]}")
        sym_inner.append(f"{p_ln} characteristic symmetricProperty")
        emit(sym_lines, *sym_inner)

    if sym_lines:
        sections.append(("Symmetric Properties", sym_lines))

    # ── Assemble final output ─────────────────────────────────────────────────
    ont_label = os.path.basename(ontology_path)
    total_tests = sum(
        1 for _, ls in sections
        for l in ls
        if l and not l.startswith("//")
    )

    header = [
        f"// Ontology → Themis Test Generator",
        f"// Source  : {ont_label}",
        f"// Sections: {len(sections)}",
        f"// Tests   : {total_tests}",
        "",
    ]

    body: list[str] = []
    for title, lines in sections:
        bar = "=" * 60
        body.append(f"// {bar}")
        body.append(f"// {title.upper()}")
        body.append(f"// {bar}")
        body.append("")
        body.extend(lines)

    return "\n".join(header + body)


# ── CLI entry point ───────────────────────────────────────────────────────────

def main_cli() -> None:
    parser = argparse.ArgumentParser(
        description="Generate all possible Themis tests from an OWL ontology."
    )
    parser.add_argument(
        "ontology",
        help="Path to the ontology file (.ttl, .owl, .rdf, .n3, …)",
    )
    parser.add_argument(
        "output",
        nargs="?",
        help="Output path (default: <ontology_stem>_tests.txt)",
    )
    parser.add_argument(
        "--all",
        dest="include_external",
        action="store_true",
        default=False,
        help="Include terms from external/imported vocabularies as well",
    )
    args = parser.parse_args()

    if not os.path.exists(args.ontology):
        print(f"Error: file not found: {args.ontology}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {args.ontology} …")
    tests = generate_tests(args.ontology, local_only=not args.include_external)

    out_path = args.output or (os.path.splitext(args.ontology)[0] + "_tests.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(tests)

    n = sum(1 for l in tests.splitlines() if l and not l.startswith("//"))
    print(f"Generated {n} test lines -> {out_path}")


# ── GUI entry point ───────────────────────────────────────────────────────────

def main_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext
    import threading

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Ontology → Themis Test Generator")
            self.geometry("980x640")
            self.configure(bg="#1e1e2e")
            self._ont_path: str | None = None
            self._tests: str = ""
            self._build_ui()

        # ── UI construction ───────────────────────────────────────────────────
        def _build_ui(self) -> None:
            BG  = "#1e1e2e"
            BTN = {"bg": "#45475a", "fg": "#cdd6f4",
                   "relief": "flat", "padx": 10, "pady": 4, "cursor": "hand2",
                   "activebackground": "#585b70", "activeforeground": "#cdd6f4"}
            LBL = {"bg": BG, "fg": "#cdd6f4"}

            top = tk.Frame(self, bg=BG)
            top.pack(fill="x", padx=12, pady=8)

            self._path_lbl = tk.Label(top, text="No ontology selected",
                                      anchor="w", **LBL)
            self._path_lbl.pack(side="left", fill="x", expand=True)

            tk.Button(top, text="Open Ontology",
                      command=self._pick_file, **BTN).pack(side="right", padx=3)
            tk.Button(top, text="Generate Tests",
                      command=self._generate, **BTN).pack(side="right", padx=3)
            tk.Button(top, text="Save",
                      command=self._save, **BTN).pack(side="right", padx=3)

            # Option: include external terms
            self._ext_var = tk.BooleanVar(value=False)
            tk.Checkbutton(
                top, text="Include external terms",
                variable=self._ext_var,
                bg=BG, fg="#a6adc8", selectcolor="#313244",
                activebackground=BG, activeforeground="#cdd6f4",
            ).pack(side="right", padx=8)

            self._txt = scrolledtext.ScrolledText(
                self,
                bg="#181825", fg="#cdd6f4",
                font=("Consolas", 10),
                insertbackground="white",
                wrap="none",
            )
            self._txt.pack(fill="both", expand=True, padx=12, pady=(0, 6))

            self._status = tk.Label(self, text="Ready.", anchor="w", **LBL)
            self._status.pack(fill="x", padx=12, pady=(0, 4))

        # ── Actions ───────────────────────────────────────────────────────────
        def _pick_file(self) -> None:
            path = filedialog.askopenfilename(
                title="Select ontology file",
                filetypes=[
                    ("OWL/RDF files", "*.ttl *.owl *.rdf *.n3 *.xml *.jsonld"),
                    ("All files", "*.*"),
                ],
            )
            if path:
                self._ont_path = path
                self._path_lbl.config(text=os.path.basename(path))
                self._status.config(text=f"Loaded: {path}")

        def _generate(self) -> None:
            if not self._ont_path:
                messagebox.showwarning("No file", "Please open an ontology file first.")
                return
            self._status.config(text="Generating …")
            self._txt.delete("1.0", "end")
            local_only = not self._ext_var.get()

            def _worker():
                try:
                    tests = generate_tests(self._ont_path, local_only=local_only)
                    self._tests = tests
                    self._txt.insert("1.0", tests)
                    n = sum(
                        1 for l in tests.splitlines()
                        if l and not l.startswith("//")
                    )
                    self.after(0, lambda: self._status.config(
                        text=f"Done — {n} test assertions generated."
                    ))
                except Exception as exc:
                    msg = str(exc)
                    self.after(0, lambda: messagebox.showerror("Error", msg))
                    self.after(0, lambda: self._status.config(text="Error."))

            threading.Thread(target=_worker, daemon=True).start()

        def _save(self) -> None:
            if not self._tests:
                messagebox.showwarning("Nothing to save", "Generate tests first.")
                return
            stem = os.path.splitext(os.path.basename(self._ont_path or "tests"))[0]
            path = filedialog.asksaveasfilename(
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                initialfile=f"{stem}_tests.txt",
            )
            if path:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(self._tests)
                self._status.config(text=f"Saved → {path}")

    App().mainloop()


# ── Dispatch ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        main_cli()
    else:
        main_gui()
