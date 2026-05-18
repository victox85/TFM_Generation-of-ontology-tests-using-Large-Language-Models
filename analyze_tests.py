"""
Ontology Test Generation Quality Analysis Script

Reads comparison.csv from every subfolder under Corpus_of_tests and
compares the "Good test" column against the "Generated test" column.

Classification per test-assertion pair (1-to-1, positional):
  1. Perfect     - normalized strings are identical
  2. Good syntax - same syntactic type (SubClassOf / property / type)
                   but different values
  3. Wrong       - different syntactic types (confusion tracked)
  4. Bad         - good test exists but nothing was generated

Competency questions whose text starts with an interrogative word
(Which, What, Is, Are, How, Who, Where, When ...) are reported
separately from regular/declarative requirements.

Output: console + Corpus_of_tests/analysis_report.txt
"""

import csv
import re
import sys
from pathlib import Path
from typing import Optional, Tuple, List

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CORPUS_DIR = Path(
    r"c:\Users\victo\Documents\TFM_Generation-of-ontology-tests-using-Large-Language-Models"
    r"\Corpus_of_tests"
)

INTERROGATIVE_WORDS = {
    'which', 'what', 'is', 'are', 'how', 'who', 'where', 'when',
    'can', 'do', 'does', 'did', 'has', 'have',
}

DATATYPES = {
    'literal', 'string', 'integer', 'float', 'boolean', 'datetime',
    'datetimestamp', 'nonnegativeinteger', 'date', 'double',
    'int', 'bool', 'time', 'ttring', 'datatime', 'nonneg',
}

# Fixed display order for syntax types
TYPES = ('SubClassOf', 'ObjectProperty', 'DataProperty', 'Type')

TYPE_LABELS = {
    'SubClassOf':    'SubClassOf',
    'ObjectProperty':'ObjectProperty',
    'DataProperty':  'DataProperty',
    'Type':          'Type assertion',
}

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


# ---------------------------------------------------------------------------
# Statistics accumulator
# ---------------------------------------------------------------------------

class Stats:
    """
    Accumulates comparison results with full per-type and confusion tracking.

    type_total[T]   - all evaluable pairs whose good test is type T
    type_perfect[T] - perfect matches of type T
    type_gs[T]      - good-syntax matches of type T (same type, diff values)
    type_wrong[T]   - wrong matches where good was T (generated was something else)
    type_missing[T] - good test of type T had nothing generated

    confusion[(good_T, gen_T)] - count of wrong pairs by type mismatch
    """

    def __init__(self):
        # Aggregate totals
        self.total       = 0
        self.perfect     = 0
        self.good_syntax = 0
        self.wrong       = 0
        self.missing_good = 0
        self.missing_gen = 0
        # good_syntax subtotals
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
        # Confusion matrix: (good_type, gen_type) -> count
        self.confusion: dict = {}

    def add(self, result: str,
            good_type: Optional[str] = None,
            gen_type:  Optional[str] = None):
        self.total += 1

        if result == 'perfect':
            self.perfect += 1
            if good_type in self.type_total:
                self.type_total[good_type]   += 1
                self.type_perfect[good_type] += 1

        elif result == 'good_syntax':
            self.good_syntax += 1
            if good_type == 'SubClassOf':    self.gs_sub += 1
            elif good_type == 'ObjectProperty': self.gs_obj += 1
            elif good_type == 'DataProperty':   self.gs_dat += 1
            elif good_type == 'Type':            self.gs_typ += 1
            if good_type in self.type_total:
                self.type_total[good_type] += 1
                self.type_gs[good_type]    += 1

        elif result == 'wrong':
            self.wrong += 1
            if good_type in self.type_total:
                self.type_total[good_type] += 1
                self.type_wrong[good_type] += 1
            if good_type and gen_type:
                key = (good_type, gen_type)
                self.confusion[key] = self.confusion.get(key, 0) + 1

        elif result == 'missing_good':
            self.missing_good += 1

        elif result == 'missing_gen':
            self.missing_gen += 1
            if good_type in self.type_total:
                self.type_total[good_type]   += 1
                self.type_missing[good_type] += 1

    def merge(self, other: 'Stats'):
        self.total       += other.total
        self.perfect     += other.perfect
        self.good_syntax += other.good_syntax
        self.wrong       += other.wrong
        self.missing_good += other.missing_good
        self.missing_gen += other.missing_gen
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
        for key, cnt in other.confusion.items():
            self.confusion[key] = self.confusion.get(key, 0) + cnt

    @property
    def base(self) -> int:
        """Evaluable pairs: good reference exists (missing_gen counts as bad)."""
        return self.total - self.missing_good

    def pct(self, n: int) -> float:
        return (n / self.base * 100) if self.base else 0.0

    def type_pct(self, n: int, t: str) -> float:
        tot = self.type_total[t]
        return (n / tot * 100) if tot else 0.0


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

def analyze_csv(path: Path) -> Tuple[Stats, Stats]:
    regular = Stats()
    cq      = Stats()
    try:
        with open(path, 'r', encoding='utf-8-sig', newline='') as fh:
            reader = csv.reader(fh)
            next(reader, None)
            for row in reader:
                if len(row) < 3:
                    continue
                question  = row[1].strip() if len(row) > 1 else ''
                good_cell = row[2].strip() if len(row) > 2 else ''
                gen_cell  = row[3].strip() if len(row) > 3 else ''
                goods = split_cell(good_cell)
                gens  = split_cell(gen_cell)
                if not goods and not gens:
                    continue
                target = cq if is_interrogative(question) else regular
                for i in range(max(len(goods), len(gens))):
                    g = goods[i] if i < len(goods) else None
                    e = gens[i]  if i < len(gens)  else None
                    result, gt, et = compare_pair(g, e)
                    if result != 'skip':
                        target.add(result, gt, et)
    except Exception as exc:
        print(f"  [Warning] Could not read {path.name}: {exc}")
    return regular, cq


# ---------------------------------------------------------------------------
# Report formatting helpers
# ---------------------------------------------------------------------------

_W = 42

def _row(label: str, n: int, pct: float, indent: int = 0) -> str:
    pad = '  ' * indent
    return f"{pad}{label:<{_W}}{n:5d}   ({pct:5.1f}%)"


# ---------------------------------------------------------------------------
# Main stats block
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
        _row("Good syntax (correct type):",     s.good_syntax,  s.pct(s.good_syntax),  2),
        _row("  SubClassOf correct:",           s.gs_sub,       s.pct(s.gs_sub),       2),
        _row("  Properties correct (total):",   gs_prop,        s.pct(gs_prop),        2),
        _row("    ObjectProperty correct:",     s.gs_obj,       s.pct(s.gs_obj),       2),
        _row("    DataProperty correct:",       s.gs_dat,       s.pct(s.gs_dat),       2),
        _row("  Type assertion correct:",       s.gs_typ,       s.pct(s.gs_typ),       2),
        _row("Wrong (type mismatch):",          s.wrong,        s.pct(s.wrong),        2),
        _row("Bad (good test not generated):",  s.missing_gen,  s.pct(s.missing_gen),  2),
    ]
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Wrong-type confusion breakdown
# ---------------------------------------------------------------------------

def format_wrong_breakdown(s: Stats) -> str:
    """Show what type the model generated instead, for each wrong pair."""
    if s.wrong == 0:
        return ''
    lines = [f"     Wrong pairs - what the model generated instead:"]
    # Sort by count descending
    sorted_conf = sorted(s.confusion.items(), key=lambda x: -x[1])
    for (gt, et), cnt in sorted_conf:
        pct = cnt / s.wrong * 100
        label = f"      Good: {TYPE_LABELS[gt]:<18} -> Generated: {TYPE_LABELS[et]:<18}"
        lines.append(f"{label}{cnt:4d}  ({pct:5.1f}% of wrong)")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Not-generated breakdown
# ---------------------------------------------------------------------------

def format_missing_breakdown(s: Stats) -> str:
    """Show which syntax types were not generated at all."""
    if s.missing_gen == 0:
        return ''
    lines = [f"     Not-generated by syntax type (each counts as bad):"]
    for t in TYPES:
        n = s.type_missing[t]
        if n == 0:
            continue
        pct_of_base = s.pct(n)         # % of eval base
        pct_of_type = s.type_pct(n, t)  # % within that type
        label = f"      {TYPE_LABELS[t]:<22} not generated:"
        lines.append(
            f"{label}{n:4d}  ({pct_of_base:5.1f}% of base | {pct_of_type:5.1f}% of all {TYPE_LABELS[t]})"
        )
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Propensity (failure rate per type)
# ---------------------------------------------------------------------------

def format_propensity(s: Stats) -> str:
    """
    For each syntax type: total good-test occurrences, how many were
    correct (perfect + good_syntax) and how many failed (wrong + not-generated).
    Ranks types by failure rate.
    """
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
        f_pct = rate
        lines.append(
            f"     {TYPE_LABELS[t]:<22} {tot:>7}  {correct:>5} ({c_pct:5.1f}%)"
            f"  {failed:>5} ({f_pct:5.1f}%)  {f_pct:>8.1f}%"
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
# Full section (stats + breakdowns + propensity)
# ---------------------------------------------------------------------------

def format_full_section(s: Stats, title: str) -> List[str]:
    out = [format_stats(s, title)]

    wrong_block = format_wrong_breakdown(s)
    if wrong_block:
        out += ['', wrong_block]

    missing_block = format_missing_breakdown(s)
    if missing_block:
        out += ['', missing_block]

    out += ['', format_propensity(s)]
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    g_reg = Stats()
    g_cq  = Stats()
    results = []

    for folder in sorted(CORPUS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        csv_path = folder / 'comparison.csv'
        if not csv_path.exists():
            continue
        reg, cq = analyze_csv(csv_path)
        results.append((folder.name, reg, cq))
        g_reg.merge(reg)
        g_cq.merge(cq)

    SEP  = '=' * 70
    THIN = '-' * 70

    out = [
        SEP,
        "  ONTOLOGY TEST GENERATION QUALITY ANALYSIS REPORT",
        SEP,
        f"  Corpus : {CORPUS_DIR}",
        f"  Folders: {len(results)}",
        "",
    ]

    for name, reg, cq in results:
        out += [THIN, f"  FOLDER: {name}", THIN]
        out += format_full_section(reg, "Regular Requirements (Declarative)")
        out.append("")
        if cq.base > 0:
            out += format_full_section(cq, "Competency Questions (Interrogative)")
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
    out += ["", SEP]

    report = '\n'.join(out)
    print(report)

    out_path = CORPUS_DIR / 'analysis_report.txt'
    out_path.write_text(report, encoding='utf-8')
    print(f"\nReport saved to: {out_path}")


if __name__ == '__main__':
    main()
