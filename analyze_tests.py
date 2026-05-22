"""
Ontology Test Generation Quality Analysis Script — Extended

Reads comparison.csv from every subfolder under Corpus_of_tests and
compares the "Good test" column against the "Generated test" column.

CORE classification per assertion pair (1-to-1, positional):
  1. Perfect     - normalized strings are identical
  2. Good syntax - same syntactic type (SubClassOf / property / type)
                   but different values
  3. Wrong       - different syntactic types (confusion tracked)
  4. Bad         - good test exists but nothing was generated

EXTENDED analyses added on top:
  * Set-based matching            (order-independent recovery of correct tests)
  * Precision / Recall / F1       (strict and lenient, per type and overall)
  * Token-level Jaccard           (how close the model got on near-misses)
  * good_syntax sub-classification (same subject? same object? both differ?)
  * Vocabulary analysis           (reused / invented / missed entity names)
  * Extras-by-type                (what kinds of assertions the model invents
                                   when there's no reference for them)
  * Per-question difficulty       (worst-performing questions, top-N)
  * Folder leaderboard            (rank ontologies by strict F1)
  * JSON export of all results    + per-row CSV with the classification of
                                    every (good, generated) pair

Competency questions (text starts with Which/What/Is/Are/How/Who/Where/When ...)
are still reported separately from regular/declarative requirements.

Output: console + Corpus_of_tests/analysis_report.txt
                + Corpus_of_tests/analysis_report.json
                + Corpus_of_tests/per_row_results.csv
"""

import csv
import json
import re
import sys
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Set

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CORPUS_DIR = Path(__file__).parent / "Corpus_of_tests"

# How many of the worst-performing questions to surface in the report.
TOP_N_WORST_QUESTIONS = 15

INTERROGATIVE_WORDS = {
    'which', 'what', 'is', 'are', 'how', 'who', 'where', 'when',
    'can', 'do', 'does', 'did', 'has', 'have',
}

DATATYPES = {
    'literal', 'string', 'integer', 'float', 'boolean', 'datetime',
    'datetimestamp', 'nonnegativeinteger', 'date', 'double',
    'int', 'bool', 'time', 'ttring', 'datatime', 'nonneg',
}

# THEMIS / OWL DL vocabulary that should NOT be counted as ontology entities.
THEMIS_KEYWORDS = {
    'subclassof', 'equivalentto', 'disjointwith', 'sameas', 'differentfrom',
    'type', 'property', 'class', 'thing', 'nothing',
    'and', 'or', 'not',
    'some', 'only', 'min', 'max', 'exactly', 'value', 'self',
    'inverseof', 'symmetric', 'asymmetric', 'transitive', 'reflexive',
    'irreflexive', 'functional', 'inversefunctional',
    'domain', 'range',
    'true', 'false',
}

# Fixed display order for syntax types
TYPES = ('SubClassOf', 'ObjectProperty', 'DataProperty', 'Type')

TYPE_LABELS = {
    'SubClassOf':     'SubClassOf',
    'ObjectProperty': 'ObjectProperty',
    'DataProperty':   'DataProperty',
    'Type':           'Type assertion',
}

# Jaccard buckets — display order
JACCARD_BUCKETS = ('0.00', '0.01-0.25', '0.26-0.50', '0.51-0.75', '0.76-0.99')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_interrogative(question: str) -> bool:
    if not question or not question.strip():
        return False
    first = question.strip().split()[0].lower().rstrip('?.,')
    return first in INTERROGATIVE_WORDS


def normalize(test: str) -> str:
    return ' '.join(test.strip().rstrip(';').lower().split())


def detect_type(test: str) -> str:
    """Return 'SubClassOf' | 'Type' | 'DataProperty' | 'ObjectProperty'."""
    clean = test.strip().rstrip(';')
    lower = clean.lower()
    if re.search(r'\bsubclassof\b', lower):
        return 'SubClassOf'
    if re.search(r'\btype\b', lower):
        return 'Type'
    tokens = clean.split()
    if tokens and tokens[-1].lower().rstrip(';') in DATATYPES:
        return 'DataProperty'
    return 'ObjectProperty'


def split_cell(cell: str) -> List[str]:
    if not cell or not cell.strip():
        return []
    return [line.strip() for line in cell.split('\n') if line.strip()]


def extract_entities(text: str) -> Set[str]:
    """Pull candidate ontology entity names out of a THEMIS test string."""
    if not text:
        return set()
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    out: Set[str] = set()
    for tok in tokens:
        low = tok.lower()
        if low in THEMIS_KEYWORDS or low in DATATYPES:
            continue
        out.add(tok)
    return out


def entity_sequence(text: str) -> List[str]:
    """Ordered entity names (lowercased) — used for subject/object comparison."""
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    seq: List[str] = []
    for tok in tokens:
        low = tok.lower()
        if low in THEMIS_KEYWORDS or low in DATATYPES:
            continue
        seq.append(low)
    return seq


def jaccard(a: str, b: str) -> float:
    """Token-level Jaccard over extracted entities."""
    sa = extract_entities(a)
    sb = extract_entities(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def jaccard_bucket(sim: float) -> str:
    if sim <= 0.0:
        return '0.00'
    if sim <= 0.25:
        return '0.01-0.25'
    if sim <= 0.50:
        return '0.26-0.50'
    if sim <= 0.75:
        return '0.51-0.75'
    return '0.76-0.99'


def classify_good_syntax(good: str, gen: str) -> str:
    """
    For a good_syntax pair (same type, different values), classify where the
    difference lives. Returns one of:
      'same_subject'  - first entity matches, rest differs
      'same_object'   - last entity matches, rest differs
      'same_both_ends'- first and last match, middle differs
      'both_differ'   - neither subject nor object match
    """
    gs = entity_sequence(good)
    es = entity_sequence(gen)
    if not gs or not es:
        return 'both_differ'
    subj_match = gs[0] == es[0]
    obj_match  = gs[-1] == es[-1]
    if subj_match and obj_match:
        return 'same_both_ends'
    if subj_match:
        return 'same_subject'
    if obj_match:
        return 'same_object'
    return 'both_differ'


def set_match_count(goods: List[str], gens: List[str]) -> int:
    """How many goods can be matched (exactly, after normalize) to some gen,
    each gen used at most once. Order-independent."""
    if not goods or not gens:
        return 0
    gen_pool = [normalize(g) for g in gens]
    matched_idx: Set[int] = set()
    count = 0
    for g in goods:
        ng = normalize(g)
        for j, eg in enumerate(gen_pool):
            if j in matched_idx:
                continue
            if eg == ng:
                matched_idx.add(j)
                count += 1
                break
    return count


# ---------------------------------------------------------------------------
# Statistics accumulator
# ---------------------------------------------------------------------------

class Stats:
    """
    Accumulates comparison results with per-type, confusion, Jaccard and
    structural tracking.

    Core counters:
      type_total[T]   - all evaluable pairs whose good test is type T
      type_perfect[T] - perfect matches of type T
      type_gs[T]      - good-syntax matches of type T
      type_wrong[T]   - wrong matches where good was T (generated was something else)
      type_missing[T] - good test of type T had nothing generated

      confusion[(good_T, gen_T)] - count of wrong pairs by type mismatch
      extra_type[T]   - assertions the model generated when no good ref existed,
                        bucketed by the generated type

    Extended:
      gs_subtypes['same_subject' | 'same_object' | 'same_both_ends' | 'both_differ']
      jaccard_buckets[bucket] = count of non-perfect non-missing pairs in that bucket
      jaccard_sum / jaccard_n = avg Jaccard over non-perfect (good_syntax + wrong) pairs

    Set-based:
      set_good   - total good assertions seen (per row, summed)
      set_gen    - total generated assertions seen
      set_match  - count of goods exactly matched somewhere in the row's gens
                   (regardless of position)
    """

    def __init__(self):
        # Aggregate totals
        self.total        = 0
        self.perfect      = 0
        self.good_syntax  = 0
        self.wrong        = 0
        self.missing_good = 0
        self.missing_gen  = 0
        # good_syntax subtotals by type
        self.gs_sub = 0
        self.gs_obj = 0
        self.gs_dat = 0
        self.gs_typ = 0
        # Per-type detail (keyed by good test's type)
        self.type_total   = {t: 0 for t in TYPES}
        self.type_perfect = {t: 0 for t in TYPES}
        self.type_gs      = {t: 0 for t in TYPES}
        self.type_wrong   = {t: 0 for t in TYPES}
        self.type_missing = {t: 0 for t in TYPES}
        # Confusion matrix
        self.confusion: Dict[Tuple[str, str], int] = {}
        # Extras-by-type (what was invented when good was empty)
        self.extra_type = {t: 0 for t in TYPES}
        # good_syntax sub-classification
        self.gs_subtypes = {
            'same_subject':   0,
            'same_object':    0,
            'same_both_ends': 0,
            'both_differ':    0,
        }
        # Jaccard tracking
        self.jaccard_buckets = {b: 0 for b in JACCARD_BUCKETS}
        self.jaccard_sum  = 0.0
        self.jaccard_n    = 0
        # Set-based matching
        self.set_good  = 0
        self.set_gen   = 0
        self.set_match = 0

    # ----- updates --------------------------------------------------------

    def add(self,
            result: str,
            good_type: Optional[str] = None,
            gen_type:  Optional[str] = None,
            similarity: Optional[float] = None,
            gs_subtype: Optional[str] = None):
        self.total += 1

        if result == 'perfect':
            self.perfect += 1
            if good_type in self.type_total:
                self.type_total[good_type]   += 1
                self.type_perfect[good_type] += 1

        elif result == 'good_syntax':
            self.good_syntax += 1
            if good_type == 'SubClassOf':       self.gs_sub += 1
            elif good_type == 'ObjectProperty': self.gs_obj += 1
            elif good_type == 'DataProperty':   self.gs_dat += 1
            elif good_type == 'Type':           self.gs_typ += 1
            if good_type in self.type_total:
                self.type_total[good_type] += 1
                self.type_gs[good_type]    += 1
            if similarity is not None:
                self._record_similarity(similarity)
            if gs_subtype and gs_subtype in self.gs_subtypes:
                self.gs_subtypes[gs_subtype] += 1

        elif result == 'wrong':
            self.wrong += 1
            if good_type in self.type_total:
                self.type_total[good_type] += 1
                self.type_wrong[good_type] += 1
            if good_type and gen_type:
                key = (good_type, gen_type)
                self.confusion[key] = self.confusion.get(key, 0) + 1
            if similarity is not None:
                self._record_similarity(similarity)

        elif result == 'missing_good':
            self.missing_good += 1
            if gen_type in self.extra_type:
                self.extra_type[gen_type] += 1

        elif result == 'missing_gen':
            self.missing_gen += 1
            if good_type in self.type_total:
                self.type_total[good_type]   += 1
                self.type_missing[good_type] += 1

    def _record_similarity(self, sim: float):
        self.jaccard_buckets[jaccard_bucket(sim)] += 1
        self.jaccard_sum += sim
        self.jaccard_n   += 1

    def add_row_counts(self, n_good: int, n_gen: int, n_set_match: int):
        self.set_good  += n_good
        self.set_gen   += n_gen
        self.set_match += n_set_match

    def merge(self, other: 'Stats'):
        self.total        += other.total
        self.perfect      += other.perfect
        self.good_syntax  += other.good_syntax
        self.wrong        += other.wrong
        self.missing_good += other.missing_good
        self.missing_gen  += other.missing_gen
        self.gs_sub += other.gs_sub
        self.gs_obj += other.gs_obj
        self.gs_dat += other.gs_dat
        self.gs_typ += other.gs_typ
        for t in TYPES:
            self.type_total[t]   += other.type_total[t]
            self.type_perfect[t] += other.type_perfect[t]
            self.type_gs[t]      += other.type_gs[t]
            self.type_wrong[t]   += other.type_wrong[t]
            self.type_missing[t] += other.type_missing[t]
            self.extra_type[t]   += other.extra_type[t]
        for key, cnt in other.confusion.items():
            self.confusion[key] = self.confusion.get(key, 0) + cnt
        for k in self.gs_subtypes:
            self.gs_subtypes[k] += other.gs_subtypes[k]
        for b in JACCARD_BUCKETS:
            self.jaccard_buckets[b] += other.jaccard_buckets[b]
        self.jaccard_sum += other.jaccard_sum
        self.jaccard_n   += other.jaccard_n
        self.set_good   += other.set_good
        self.set_gen    += other.set_gen
        self.set_match  += other.set_match

    # ----- derived metrics -------------------------------------------------

    @property
    def base(self) -> int:
        """Evaluable pairs: good reference exists (missing_gen counts as bad)."""
        return self.total - self.missing_good

    def pct(self, n: int) -> float:
        return (n / self.base * 100) if self.base else 0.0

    def type_pct(self, n: int, t: str) -> float:
        tot = self.type_total[t]
        return (n / tot * 100) if tot else 0.0

    # Predictions / references for precision/recall.
    #   references = pairs where good is non-empty   (== base)
    #   predictions = pairs where gen is non-empty   (== total - missing_gen)
    @property
    def predictions(self) -> int:
        return self.total - self.missing_gen

    @property
    def references(self) -> int:
        return self.base

    def _safe_div(self, num: int, den: int) -> float:
        return (num / den) if den else 0.0

    @property
    def precision_strict(self) -> float:
        return self._safe_div(self.perfect, self.predictions)

    @property
    def recall_strict(self) -> float:
        return self._safe_div(self.perfect, self.references)

    @property
    def f1_strict(self) -> float:
        p, r = self.precision_strict, self.recall_strict
        return (2 * p * r / (p + r)) if (p + r) else 0.0

    @property
    def precision_lenient(self) -> float:
        return self._safe_div(self.perfect + self.good_syntax, self.predictions)

    @property
    def recall_lenient(self) -> float:
        return self._safe_div(self.perfect + self.good_syntax, self.references)

    @property
    def f1_lenient(self) -> float:
        p, r = self.precision_lenient, self.recall_lenient
        return (2 * p * r / (p + r)) if (p + r) else 0.0

    @property
    def jaccard_mean(self) -> float:
        return (self.jaccard_sum / self.jaccard_n) if self.jaccard_n else 0.0

    @property
    def set_recall(self) -> float:
        return self._safe_div(self.set_match, self.set_good)

    @property
    def set_precision(self) -> float:
        return self._safe_div(self.set_match, self.set_gen)


# ---------------------------------------------------------------------------
# Per-question tracking
# ---------------------------------------------------------------------------

class QuestionResult:
    """Lightweight record of how one question/requirement scored."""

    __slots__ = ('folder', 'row_idx', 'question', 'is_cq',
                 'goods', 'gens', 'pair_results')

    def __init__(self, folder: str, row_idx: int, question: str, is_cq: bool,
                 goods: List[str], gens: List[str], pair_results: List[str]):
        self.folder       = folder
        self.row_idx      = row_idx
        self.question     = question
        self.is_cq        = is_cq
        self.goods        = goods
        self.gens         = gens
        self.pair_results = pair_results

    @property
    def n_pairs(self) -> int:
        # pairs that count toward this question's "score"
        return sum(1 for r in self.pair_results if r != 'skip' and r != 'missing_good')

    @property
    def n_perfect(self) -> int:
        return sum(1 for r in self.pair_results if r == 'perfect')

    @property
    def accuracy(self) -> float:
        n = self.n_pairs
        return (self.n_perfect / n) if n else 1.0


# ---------------------------------------------------------------------------
# Folder-level container
# ---------------------------------------------------------------------------

class FolderResult:
    def __init__(self, name: str):
        self.name = name
        self.regular = Stats()
        self.cq      = Stats()
        self.entities_good: Set[str] = set()
        self.entities_gen:  Set[str] = set()
        self.questions: List[QuestionResult] = []

    @property
    def combined(self) -> Stats:
        s = Stats()
        s.merge(self.regular)
        s.merge(self.cq)
        return s


# ---------------------------------------------------------------------------
# Pair comparison
# ---------------------------------------------------------------------------

def compare_pair(
    good: Optional[str], gen: Optional[str]
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Returns (result, good_type, gen_type).
      result: 'perfect'|'good_syntax'|'wrong'|'missing_good'|'missing_gen'|'skip'
    """
    g_empty = not good or not good.strip()
    e_empty = not gen  or not gen.strip()

    if g_empty and e_empty:
        return 'skip', None, None
    if g_empty:
        return 'missing_good', None, detect_type(gen)
    if e_empty:
        return 'missing_gen', detect_type(good), None

    if normalize(good) == normalize(gen):
        t = detect_type(good)
        return 'perfect', t, t

    gt = detect_type(good)
    et = detect_type(gen)
    if gt == et:
        return 'good_syntax', gt, et

    return 'wrong', gt, et


# ---------------------------------------------------------------------------
# CSV analysis
# ---------------------------------------------------------------------------

def analyze_csv(path: Path, folder_name: str) -> FolderResult:
    fr = FolderResult(folder_name)
    try:
        with open(path, 'r', encoding='utf-8-sig', newline='') as fh:
            reader = csv.reader(fh)
            next(reader, None)
            for row_idx, row in enumerate(reader, start=1):
                if len(row) < 3:
                    continue
                question  = row[1].strip() if len(row) > 1 else ''
                good_cell = row[2].strip() if len(row) > 2 else ''
                gen_cell  = row[3].strip() if len(row) > 3 else ''
                goods = split_cell(good_cell)
                gens  = split_cell(gen_cell)
                if not goods and not gens:
                    continue

                is_cq = is_interrogative(question)
                target = fr.cq if is_cq else fr.regular

                # Set-based row counts (order-independent matches)
                target.add_row_counts(len(goods), len(gens),
                                      set_match_count(goods, gens))

                # Vocabulary accumulation
                for g in goods:
                    fr.entities_good |= extract_entities(g)
                for e in gens:
                    fr.entities_gen |= extract_entities(e)

                pair_results: List[str] = []
                for i in range(max(len(goods), len(gens))):
                    g = goods[i] if i < len(goods) else None
                    e = gens[i]  if i < len(gens)  else None
                    result, gt, et = compare_pair(g, e)
                    if result == 'skip':
                        pair_results.append('skip')
                        continue

                    sim = None
                    gs_sub = None
                    if result in ('good_syntax', 'wrong') and g and e:
                        sim = jaccard(g, e)
                    if result == 'good_syntax' and g and e:
                        gs_sub = classify_good_syntax(g, e)

                    target.add(result, gt, et, sim, gs_sub)
                    pair_results.append(result)

                fr.questions.append(QuestionResult(
                    folder=folder_name, row_idx=row_idx, question=question,
                    is_cq=is_cq, goods=goods, gens=gens,
                    pair_results=pair_results,
                ))
    except Exception as exc:
        print(f"  [Warning] Could not read {path.name}: {exc}")
    return fr


# ---------------------------------------------------------------------------
# Report formatting helpers
# ---------------------------------------------------------------------------

_W = 42

def _row(label: str, n: int, pct: float, indent: int = 0) -> str:
    pad = '  ' * indent
    return f"{pad}{label:<{_W}}{n:5d}   ({pct:5.1f}%)"


# ---------------------------------------------------------------------------
# Main stats block (unchanged shape — keeps existing output intact)
# ---------------------------------------------------------------------------

def format_stats(s: Stats, title: str) -> str:
    b = s.base
    gs_prop = s.gs_obj + s.gs_dat
    lines = [
        f"  >> {title}",
        f"     {'-' * 58}",
        f"     Total assertions:                   {s.total}",
        f"     No reference (good test absent):    {s.missing_good}  (excluded from evaluation)",
        f"     Evaluation base:                    {b}",
        f"",
        f"     Results  (% of evaluation base = {b}):",
        _row("Perfect (exact match):",         s.perfect,      s.pct(s.perfect),      2),
        _row("Good syntax (correct type):",    s.good_syntax,  s.pct(s.good_syntax),  2),
        _row("  SubClassOf correct:",          s.gs_sub,       s.pct(s.gs_sub),       2),
        _row("  Properties correct (total):",  gs_prop,        s.pct(gs_prop),        2),
        _row("    ObjectProperty correct:",    s.gs_obj,       s.pct(s.gs_obj),       2),
        _row("    DataProperty correct:",      s.gs_dat,       s.pct(s.gs_dat),       2),
        _row("  Type assertion correct:",      s.gs_typ,       s.pct(s.gs_typ),       2),
        _row("Wrong (type mismatch):",         s.wrong,        s.pct(s.wrong),        2),
        _row("Bad (good test not generated):", s.missing_gen,  s.pct(s.missing_gen),  2),
    ]
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Confusion / missing / propensity (existing blocks)
# ---------------------------------------------------------------------------

def format_wrong_breakdown(s: Stats) -> str:
    if s.wrong == 0:
        return ''
    lines = [f"     Wrong pairs - what the model generated instead:"]
    for (gt, et), cnt in sorted(s.confusion.items(), key=lambda x: -x[1]):
        pct = cnt / s.wrong * 100
        label = f"      Good: {TYPE_LABELS[gt]:<18} -> Generated: {TYPE_LABELS[et]:<18}"
        lines.append(f"{label}{cnt:4d}  ({pct:5.1f}% of wrong)")
    return '\n'.join(lines)


def format_missing_breakdown(s: Stats) -> str:
    if s.missing_gen == 0:
        return ''
    lines = [f"     Not-generated by syntax type (each counts as bad):"]
    for t in TYPES:
        n = s.type_missing[t]
        if n == 0:
            continue
        pct_of_base = s.pct(n)
        pct_of_type = s.type_pct(n, t)
        label = f"      {TYPE_LABELS[t]:<22} not generated:"
        lines.append(
            f"{label}{n:4d}  ({pct_of_base:5.1f}% of base | {pct_of_type:5.1f}% of all {TYPE_LABELS[t]})"
        )
    return '\n'.join(lines)


def format_propensity(s: Stats) -> str:
    lines = [
        f"     Propensity to fail - per syntax type:",
        f"     {'Type':<22} {'Total':>7} {'Correct':>9} {'Failed':>9}  {'Fail rate':>10}",
        f"     {'-' * 58}",
    ]
    rates = []
    for t in TYPES:
        tot     = s.type_total[t]
        correct = s.type_perfect[t] + s.type_gs[t]
        failed  = s.type_wrong[t]   + s.type_missing[t]
        if tot == 0:
            continue
        rate = failed / tot * 100
        rates.append((t, tot, correct, failed, rate))
        c_pct = correct / tot * 100
        lines.append(
            f"     {TYPE_LABELS[t]:<22} {tot:>7}  {correct:>5} ({c_pct:5.1f}%)"
            f"  {failed:>5} ({rate:5.1f}%)  {rate:>8.1f}%"
        )
    if rates:
        lines.append(f"     {'-' * 58}")
        worst = max(rates, key=lambda x: x[4])
        best  = min(rates, key=lambda x: x[4])
        lines.append(
            f"     Most error-prone: {TYPE_LABELS[worst[0]]} "
            f"({worst[4]:.1f}% failure rate, {worst[3]} of {worst[1]} failed)"
        )
        lines.append(
            f"     Most reliable:    {TYPE_LABELS[best[0]]} "
            f"({best[4]:.1f}% failure rate, {best[3]} of {best[1]} failed)"
        )
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# NEW: Precision / Recall / F1
# ---------------------------------------------------------------------------

def format_metrics(s: Stats) -> str:
    """Precision/Recall/F1, both strict (perfect only) and lenient (perfect + good_syntax)."""
    lines = [
        f"     Precision / Recall / F1:",
        f"       references  (pairs with a good test):  {s.references}",
        f"       predictions (pairs with generated):    {s.predictions}",
        f"       {'metric':<26} {'P':>7}  {'R':>7}  {'F1':>7}",
        f"       {'-' * 52}",
        f"       {'strict (perfect only)':<26}"
        f"  {s.precision_strict*100:6.1f}% {s.recall_strict*100:6.1f}% {s.f1_strict*100:6.1f}%",
        f"       {'lenient (+ good_syntax)':<26}"
        f"  {s.precision_lenient*100:6.1f}% {s.recall_lenient*100:6.1f}% {s.f1_lenient*100:6.1f}%",
    ]
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# NEW: Set-based (order-independent) matching
# ---------------------------------------------------------------------------

def format_set_based(s: Stats) -> str:
    """How many good tests are recovered when order is ignored."""
    if s.set_good == 0 and s.set_gen == 0:
        return ''
    lines = [
        f"     Set-based matching (order-independent within each row):",
        f"       Total good assertions:       {s.set_good}",
        f"       Total generated assertions:  {s.set_gen}",
        f"       Exact set matches:           {s.set_match}",
        f"       Set recall (matched / good):       {s.set_recall*100:5.1f}%",
        f"       Set precision (matched / gen):     {s.set_precision*100:5.1f}%",
    ]
    # Show the gain over positional perfect — i.e. tests the model got right
    # but mis-positioned.
    extra = s.set_match - s.perfect
    if extra > 0:
        lines.append(
            f"       Recovered by ignoring order: +{extra}  "
            f"(positional perfect was {s.perfect})"
        )
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# NEW: Jaccard similarity for non-perfect pairs
# ---------------------------------------------------------------------------

def format_jaccard(s: Stats) -> str:
    if s.jaccard_n == 0:
        return ''
    lines = [
        f"     Token-level Jaccard for non-perfect pairs (good_syntax + wrong):",
        f"       Pairs scored:        {s.jaccard_n}",
        f"       Mean Jaccard:        {s.jaccard_mean:.3f}",
        f"       Distribution:",
    ]
    for b in JACCARD_BUCKETS:
        n = s.jaccard_buckets[b]
        if n == 0:
            continue
        pct = n / s.jaccard_n * 100
        lines.append(f"         Jaccard {b:<10}  {n:4d}  ({pct:5.1f}%)")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# NEW: good_syntax sub-classification
# ---------------------------------------------------------------------------

def format_gs_subtypes(s: Stats) -> str:
    if s.good_syntax == 0:
        return ''
    total = s.good_syntax
    lines = [
        f"     Good-syntax error localisation (right type, wrong value):",
    ]
    pretty = {
        'same_both_ends': 'Subject and object both match (middle differs)',
        'same_subject':   'Same subject, different object/rest',
        'same_object':    'Same object, different subject/rest',
        'both_differ':    'Neither subject nor object match',
    }
    for k in ('same_both_ends', 'same_subject', 'same_object', 'both_differ'):
        n = s.gs_subtypes[k]
        if n == 0:
            continue
        pct = n / total * 100
        lines.append(f"       {pretty[k]:<50} {n:4d}  ({pct:5.1f}% of good_syntax)")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# NEW: Extras — what the model generated when good was empty
# ---------------------------------------------------------------------------

def format_extras_by_type(s: Stats) -> str:
    if s.missing_good == 0:
        return ''
    lines = [
        f"     Extras (model generated, no good reference) by type:",
        f"       Total extras: {s.missing_good}",
    ]
    for t in TYPES:
        n = s.extra_type[t]
        if n == 0:
            continue
        pct = n / s.missing_good * 100
        lines.append(f"       {TYPE_LABELS[t]:<22} {n:4d}  ({pct:5.1f}% of extras)")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# NEW: Vocabulary analysis
# ---------------------------------------------------------------------------

def format_vocab(fr_list: List[FolderResult]) -> str:
    """Aggregate vocabulary across all folders."""
    good_all: Set[str] = set()
    gen_all:  Set[str] = set()
    for fr in fr_list:
        good_all |= fr.entities_good
        gen_all  |= fr.entities_gen

    if not good_all and not gen_all:
        return ''

    reused   = good_all & gen_all
    invented = gen_all  - good_all
    missed   = good_all - gen_all

    def safe_pct(num: int, den: int) -> float:
        return (num / den * 100) if den else 0.0

    lines = [
        f"  >> Vocabulary Analysis (entity names across all folders)",
        f"     {'-' * 58}",
        f"     Unique entities in good tests:           {len(good_all)}",
        f"     Unique entities in generated tests:      {len(gen_all)}",
        f"     Reused correctly (good ∩ gen):           "
        f"{len(reused):5d}  ({safe_pct(len(reused), len(good_all)):5.1f}% of good vocab)",
        f"     Invented by model (gen − good):          "
        f"{len(invented):5d}  ({safe_pct(len(invented), len(gen_all)):5.1f}% of gen vocab)",
        f"     Missed by model  (good − gen):           "
        f"{len(missed):5d}  ({safe_pct(len(missed), len(good_all)):5.1f}% of good vocab)",
    ]

    # Show a sample of invented and missed entities (most useful for debugging)
    if invented:
        sample = sorted(invented)[:20]
        lines.append(f"     Sample invented entities (up to 20): {', '.join(sample)}")
    if missed:
        sample = sorted(missed)[:20]
        lines.append(f"     Sample missed   entities (up to 20): {', '.join(sample)}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# NEW: Worst-performing questions
# ---------------------------------------------------------------------------

def format_worst_questions(fr_list: List[FolderResult], n: int = TOP_N_WORST_QUESTIONS) -> str:
    all_q: List[QuestionResult] = []
    for fr in fr_list:
        # Only consider questions that actually had reference tests to score
        # AND that had at least one non-perfect pair (otherwise they're not
        # 'worst' — they're fine).
        all_q.extend(
            q for q in fr.questions
            if q.n_pairs > 0 and q.n_perfect < q.n_pairs
        )
    if not all_q:
        return ''

    # Sort by accuracy ascending, then by number of pairs descending (so ties
    # surface the ones with more failed assertions).
    all_q.sort(key=lambda q: (q.accuracy, -q.n_pairs))

    chosen = all_q[:n]
    lines = [
        f"  >> Worst-performing questions (top {len(chosen)} of {len(all_q)} imperfect)",
        f"     {'-' * 58}",
        f"     Ranked by per-question accuracy (perfect / scoreable pairs):",
    ]
    for i, q in enumerate(chosen, 1):
        kind = 'CQ ' if q.is_cq else 'REQ'
        qtext = q.question if q.question else '(no question text)'
        if len(qtext) > 78:
            qtext = qtext[:75] + '...'
        lines.append(
            f"     {i:>2}. [{kind}] {q.folder}/row{q.row_idx}  "
            f"{q.n_perfect}/{q.n_pairs} perfect  ({q.accuracy*100:5.1f}%)"
        )
        lines.append(f"          Q: {qtext}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# NEW: Folder leaderboard
# ---------------------------------------------------------------------------

def format_folder_leaderboard(fr_list: List[FolderResult]) -> str:
    rows = []
    for fr in fr_list:
        comb = fr.combined
        if comb.base == 0:
            continue
        rows.append((
            fr.name,
            comb.base,
            comb.pct(comb.perfect),
            comb.f1_strict * 100,
            comb.f1_lenient * 100,
            comb.jaccard_mean if comb.jaccard_n > 0 else None,
        ))
    if not rows:
        return ''
    rows.sort(key=lambda r: -r[3])  # by strict F1 desc

    lines = [
        f"  >> Folder Leaderboard  (ranked by strict F1)",
        f"     {'-' * 70}",
        f"     {'Rank':<4} {'Folder':<28} {'Base':>5}  {'Perfect%':>9}  "
        f"{'F1 str':>7}  {'F1 len':>7}  {'avgJac':>7}",
        f"     {'-' * 70}",
    ]
    for i, (name, base, perfect_pct, f1s, f1l, jac) in enumerate(rows, 1):
        name_short = name if len(name) <= 28 else name[:25] + '...'
        jac_str = f"{jac:>7.3f}" if jac is not None else f"{'—':>7}"
        lines.append(
            f"     {i:<4} {name_short:<28} {base:>5}  {perfect_pct:>8.1f}%  "
            f"{f1s:>6.1f}%  {f1l:>6.1f}%  {jac_str}"
        )
    lines.append(f"     {'-' * 70}")
    lines.append(f"     avgJac shown as '—' when no non-perfect pairs were scored.")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Full section (stats + breakdowns + propensity + new analyses)
# ---------------------------------------------------------------------------

def format_full_section(s: Stats, title: str, *, extended: bool = True) -> List[str]:
    out = [format_stats(s, title)]

    wrong_block = format_wrong_breakdown(s)
    if wrong_block:
        out += ['', wrong_block]

    missing_block = format_missing_breakdown(s)
    if missing_block:
        out += ['', missing_block]

    out += ['', format_propensity(s)]

    if extended:
        out += ['', format_metrics(s)]

        set_block = format_set_based(s)
        if set_block:
            out += ['', set_block]

        jac_block = format_jaccard(s)
        if jac_block:
            out += ['', jac_block]

        gs_block = format_gs_subtypes(s)
        if gs_block:
            out += ['', gs_block]

        extras_block = format_extras_by_type(s)
        if extras_block:
            out += ['', extras_block]
    return out


# ---------------------------------------------------------------------------
# JSON / CSV exports
# ---------------------------------------------------------------------------

def stats_to_dict(s: Stats) -> dict:
    return {
        'total':        s.total,
        'perfect':      s.perfect,
        'good_syntax':  s.good_syntax,
        'wrong':        s.wrong,
        'missing_good': s.missing_good,
        'missing_gen':  s.missing_gen,
        'base':         s.base,
        'predictions':  s.predictions,
        'references':   s.references,
        'gs_subtotals': {
            'SubClassOf':     s.gs_sub,
            'ObjectProperty': s.gs_obj,
            'DataProperty':   s.gs_dat,
            'Type':           s.gs_typ,
        },
        'per_type': {
            t: {
                'total':   s.type_total[t],
                'perfect': s.type_perfect[t],
                'good_syntax': s.type_gs[t],
                'wrong':   s.type_wrong[t],
                'missing': s.type_missing[t],
            } for t in TYPES
        },
        'confusion':   {f"{g}->{e}": n for (g, e), n in s.confusion.items()},
        'extra_type':  dict(s.extra_type),
        'gs_subtypes': dict(s.gs_subtypes),
        'jaccard': {
            'mean':    s.jaccard_mean,
            'n':       s.jaccard_n,
            'buckets': dict(s.jaccard_buckets),
        },
        'set_based': {
            'good':       s.set_good,
            'gen':        s.set_gen,
            'matches':    s.set_match,
            'recall':     s.set_recall,
            'precision':  s.set_precision,
        },
        'metrics': {
            'precision_strict':  s.precision_strict,
            'recall_strict':     s.recall_strict,
            'f1_strict':         s.f1_strict,
            'precision_lenient': s.precision_lenient,
            'recall_lenient':    s.recall_lenient,
            'f1_lenient':        s.f1_lenient,
        },
    }


def export_json(fr_list: List[FolderResult],
                g_reg: Stats, g_cq: Stats, g_all: Stats,
                path: Path):
    payload = {
        'corpus_dir': str(CORPUS_DIR),
        'folders': [
            {
                'name':       fr.name,
                'regular':    stats_to_dict(fr.regular),
                'cq':         stats_to_dict(fr.cq),
                'combined':   stats_to_dict(fr.combined),
                'vocab': {
                    'good_unique':     len(fr.entities_good),
                    'gen_unique':      len(fr.entities_gen),
                    'reused':          sorted(fr.entities_good & fr.entities_gen),
                    'invented':        sorted(fr.entities_gen  - fr.entities_good),
                    'missed':          sorted(fr.entities_good - fr.entities_gen),
                },
            }
            for fr in fr_list
        ],
        'global': {
            'regular':  stats_to_dict(g_reg),
            'cq':       stats_to_dict(g_cq),
            'overall':  stats_to_dict(g_all),
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')


def export_per_row_csv(fr_list: List[FolderResult], path: Path):
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow([
            'folder', 'row_idx', 'kind', 'question',
            'assertion_idx', 'good_test', 'generated_test', 'result',
            'good_type', 'gen_type',
        ])
        for fr in fr_list:
            for q in fr.questions:
                # Re-build the per-assertion view to recover types.
                n = max(len(q.goods), len(q.gens))
                for i in range(n):
                    g = q.goods[i] if i < len(q.goods) else ''
                    e = q.gens[i]  if i < len(q.gens)  else ''
                    result = q.pair_results[i] if i < len(q.pair_results) else 'skip'
                    gt = detect_type(g) if g else ''
                    et = detect_type(e) if e else ''
                    w.writerow([
                        fr.name, q.row_idx, 'CQ' if q.is_cq else 'REQ',
                        q.question, i + 1, g, e, result, gt, et,
                    ])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    fr_list: List[FolderResult] = []
    g_reg = Stats()
    g_cq  = Stats()

    for folder in sorted(CORPUS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        csv_path = folder / 'comparison.csv'
        if not csv_path.exists():
            continue
        fr = analyze_csv(csv_path, folder.name)
        fr_list.append(fr)
        g_reg.merge(fr.regular)
        g_cq.merge(fr.cq)

    SEP  = '=' * 70
    THIN = '-' * 70

    out = [
        SEP,
        "  ONTOLOGY TEST GENERATION QUALITY ANALYSIS REPORT",
        SEP,
        f"  Corpus : {CORPUS_DIR}",
        f"  Folders: {len(fr_list)}",
        "",
    ]

    for fr in fr_list:
        out += [THIN, f"  FOLDER: {fr.name}", THIN]
        out += format_full_section(fr.regular, "Regular Requirements (Declarative)")
        out.append("")
        if fr.cq.base > 0:
            out += format_full_section(fr.cq, "Competency Questions (Interrogative)")
            out.append("")

    g_all = Stats()
    g_all.merge(g_reg)
    g_all.merge(g_cq)

    out += [SEP, "  GLOBAL REPORT  -  ALL FOLDERS COMBINED", SEP, ""]
    out += format_full_section(g_reg, "Regular Requirements  -  all folders")
    out.append("")
    if g_cq.base > 0:
        out += format_full_section(g_cq, "Competency Questions  -  all folders")
        out.append("")
    out += format_full_section(g_all, "OVERALL  (Regular + CQ combined)")
    out.append("")

    # Cross-folder analyses
    out += [SEP, "  CROSS-FOLDER ANALYSES", SEP, ""]

    lb = format_folder_leaderboard(fr_list)
    if lb:
        out += [lb, ""]

    vocab = format_vocab(fr_list)
    if vocab:
        out += [vocab, ""]

    worst = format_worst_questions(fr_list)
    if worst:
        out += [worst, ""]

    out += [SEP]

    report = '\n'.join(out)
    print(report)

    txt_path  = CORPUS_DIR / 'analysis_report.txt'
    json_path = CORPUS_DIR / 'analysis_report.json'
    csv_path  = CORPUS_DIR / 'per_row_results.csv'

    txt_path.write_text(report, encoding='utf-8')
    export_json(fr_list, g_reg, g_cq, g_all, json_path)
    export_per_row_csv(fr_list, csv_path)

    print(f"\nReport saved to: {txt_path}")
    print(f"JSON export:     {json_path}")
    print(f"Per-row CSV:     {csv_path}")


if __name__ == '__main__':
    main()