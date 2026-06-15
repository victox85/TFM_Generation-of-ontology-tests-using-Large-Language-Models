#!/usr/bin/env python3
"""
Validador de tests THEMIS.

Pipeline híbrido:
  1) Determinista (resuelve casing, inversión sujeto/objeto, type<->SubClassOf,
     contra el conjunto de REFERENCIA extraído de la ontología).
  2) LM local (gemma-4-e4b-it) SOLO para el residuo difuso:
     ¿candidato ausente pero plausible (ontología incompleta) o malformado?

REFERENCIA  = tests extraídos de la ontología (TXT)  -> verdad de base / esquema
CANDIDATOS  = tests generados desde las CQ (CSV, columna 'title')

El cliente LM es real (endpoint OpenAI-compatible). Si no hay endpoint
disponible cae a una heurística determinista, dejándolo explícito en _source.
"""
import csv
import json
import sys
import urllib.request

# Palabras clave estructurales: NO cuentan como vocabulario de dominio.
STRUCT_KW = {"type", "SubClassOf", "only"}


# ----------------------------- Normalización ---------------------------------
def collapse(s: str) -> str:
    return " ".join(s.split())


def canon(s: str) -> str:
    """Canonicaliza SOLO las palabras clave estructurales (case-insensitive).
    Nombres de clases/individuos/propiedades quedan intactos (case-sensitive)."""
    out = []
    for t in s.split():
        low = t.lower()
        if low == "subclassof":
            out.append("SubClassOf")
        elif low == "type":
            out.append("type")
        else:
            out.append(t)
    return " ".join(out)


# ----------------------------- Carga de referencia ---------------------------
def load_reference(path: str):
    lines = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            s = raw.strip()
            if not s or s.startswith("//"):
                continue
            lines.append(collapse(s))
    raw_set = set(lines)
    canon_map = {}  # canon(line) -> linea canónica original (primera aparición)
    for l in lines:
        canon_map.setdefault(canon(l), l)
    # Vocabulario de dominio: todos los tokens menos las kw estructurales puras.
    known = set()
    for l in lines:
        for t in l.split():
            if t not in STRUCT_KW:
                known.add(t)
    return raw_set, canon_map, known


# ----------------------------- Cliente LM ------------------------------------
SYSTEM_TEMPLATE = """Eres un validador de tests de ontología en sintaxis THEMIS. Tu ÚNICA tarea: comparar UN test CANDIDATO contra el conjunto REFERENCIA y emitir un veredicto en JSON. No charlas. No explicas fuera del JSON.

# Formato de un test (tokens separados por espacio)
- Declaración de clase:    X type Class
- Jerarquía de clases:     X SubClassOf Y
- Individuo (instancia):   X type ClaseDeX        (ClaseDeX ≠ Class)
- Propiedad de objeto:     Dominio prop Rango      (ej: Rule hasAction Action)
- Restricción de datos:    Clase prop string
- Restricción universal:   X SubClassOf prop only Clase   (puede tener >3 tokens)

# REFERENCIA (verdad de base extraída de la ontología; ÚSALA EN EXCLUSIVA)
{{REFERENCIA}}

# Normalización (aplícala ANTES de comparar)
1. Recorta y colapsa espacios.
2. Normaliza SOLO las palabras clave estructurales, ignorando mayúsculas:
   subclassof/subclassOf/SubClassof/... -> SubClassOf ; type -> type
3. Los nombres de clases, individuos y propiedades son SENSIBLES a mayúsculas
   y deben coincidir EXACTAMENTE con la REFERENCIA. No los "corrijas".

# Qué detectar (TODA corrección debe estar respaldada por una línea concreta de REFERENCIA)
- INVERSION: candidato "A prop B" pero en REFERENCIA está "B prop A" (dominio/rango
  intercambiados), o "B q A" con q inversa declarada de prop. Corrige al orden de REFERENCIA.
- TYPE_VS_SUBCLASS: el candidato usa "SubClassOf" pero el sujeto es INDIVIDUO
  (en REFERENCIA aparece "Sujeto type AlgunaClase" y NO "Sujeto type Class");
  o usa "type" cuando el sujeto es CLASE (en REFERENCIA "Sujeto type Class").
  Corrige según lo que diga REFERENCIA.
- CASE: difiere solo en mayúsculas de una palabra clave estructural.

# Veredictos (elige UNO)
- OK_EXACT: coincide con una línea de REFERENCIA salvo espacios.
- OK_NORMALIZED: coincide tras normalizar mayúsculas de la palabra clave.
- FIX_INVERSION: corregido por inversión.
- FIX_TYPE_VS_SUBCLASS: corregido por confusión type/SubClassOf.
- NOT_IN_REFERENCE_PLAUSIBLE: sin coincidencia tras intentarlo todo, PERO usa solo
  vocabulario (clases/propiedades/individuos) presente en REFERENCIA y está bien
  formado. La ontología puede estar incompleta: NO es error; marcar para revisión.
- NOT_IN_REFERENCE_MALFORMED: sin coincidencia y usa términos que NO existen en
  REFERENCIA, o no es un test bien formado.

# Reglas anti-invención (OBLIGATORIO)
- Usa EXCLUSIVAMENTE términos y líneas de REFERENCIA. Nunca inventes clases,
  propiedades, individuos ni tests.
- Si no puedes respaldar una corrección con una línea concreta de REFERENCIA, NO la
  propongas: usa NOT_IN_REFERENCE_PLAUSIBLE o _MALFORMED.
- Ante la duda entre corregir y no poder respaldarlo, elige PLAUSIBLE.
- Devuelve SOLO el JSON. Nada antes ni después.

# Salida (JSON estricto, una sola línea)
{"candidate":"<test tal cual>","verdict":"<veredicto>","corrected":"<forma canónica; = candidate si OK_*; null si MALFORMED>","matched_reference":"<línea exacta de REFERENCIA usada, o null>","reason":"<≤20 palabras citando la línea de REFERENCIA>"}

# Ejemplos
CANDIDATO: Permission subClassOf Rule
{"candidate":"Permission subClassOf Rule","verdict":"OK_NORMALIZED","corrected":"Permission SubClassOf Rule","matched_reference":"Permission SubClassOf Rule","reason":"Solo difiere en mayúsculas de SubClassOf"}

CANDIDATO: Friends SubClassOf RuleTarget
{"candidate":"Friends SubClassOf RuleTarget","verdict":"FIX_TYPE_VS_SUBCLASS","corrected":"Friends type RuleTarget","matched_reference":"Friends type RuleTarget","reason":"Friends es individuo (REFERENCIA: Friends type RuleTarget), no subclase"}

CANDIDATO: Action hasAction Rule
{"candidate":"Action hasAction Rule","verdict":"FIX_INVERSION","corrected":"Rule hasAction Action","matched_reference":"Rule hasAction Action","reason":"Dominio/rango invertidos frente a REFERENCIA"}

CANDIDATO: Rule hasName string
{"candidate":"Rule hasName string","verdict":"NOT_IN_REFERENCE_PLAUSIBLE","corrected":"Rule hasName string","matched_reference":null,"reason":"Vocabulario conocido pero ausente; ontología posiblemente incompleta"}

CANDIDATO: Item containsNode Node
{"candidate":"Item containsNode Node","verdict":"NOT_IN_REFERENCE_MALFORMED","corrected":null,"matched_reference":null,"reason":"containsNode y Node no existen en REFERENCIA"}"""


def classify_residual_with_llm(candidate, reference_lines, known, *,
                               base_url="http://localhost:1234/v1",
                               model="gemma-4-e4b-it", timeout=30):
    """Intenta clasificar con el LM local. Si falla, heurística determinista."""
    system = SYSTEM_TEMPLATE.format(REFERENCIA="\n".join(sorted(reference_lines)))
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"CANDIDATO: {candidate}"},
        ],
    }
    try:
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        content = data["choices"][0]["message"]["content"].strip()
        # Quita posibles vallas de código
        content = content.replace("```json", "").replace("```", "").strip()
        out = json.loads(content)
        out["_source"] = model
        return out
    except Exception as e:
        # ---- Fallback determinista (NO es Gemma; queda marcado) ----
        toks = [t for t in candidate.split() if t not in STRUCT_KW
                and t not in {"string", "Class", "Property"}]
        all_known = all(t in known for t in toks)
        verdict = ("NOT_IN_REFERENCE_PLAUSIBLE" if all_known
                   else "NOT_IN_REFERENCE_MALFORMED")
        return {
            "candidate": candidate,
            "verdict": verdict,
            "corrected": candidate if all_known else None,
            "matched_reference": None,
            "reason": ("vocabulario conocido, ausente de referencia" if all_known
                       else "usa términos ausentes en referencia"),
            "_source": f"heuristica_fallback ({type(e).__name__})",
        }


# ----------------------------- Validación ------------------------------------
def validate(candidate, raw_set, canon_map, known, **llm_kw):
    rc = collapse(candidate)

    # 1) Exacto
    if rc in raw_set:
        return _v(candidate, "OK_EXACT", rc, rc, "coincide con referencia", "deterministic")

    cc = canon(rc)
    # 2) Normalizado (solo difiere en mayúsculas de palabra clave)
    if cc in canon_map:
        ref = canon_map[cc]
        return _v(candidate, "OK_NORMALIZED", ref, ref,
                  "coincide tras normalizar palabra clave", "deterministic")

    toks = cc.split()
    if len(toks) == 3:
        X, kw, Y = toks
        # 3) type <-> SubClassOf
        if kw in ("type", "SubClassOf"):
            other = "type" if kw == "SubClassOf" else "SubClassOf"
            alt = f"{X} {other} {Y}"
            if alt in canon_map:
                ref = canon_map[alt]
                return _v(candidate, "FIX_TYPE_VS_SUBCLASS", ref, ref,
                          f"referencia usa '{other}', no '{kw}'", "deterministic")
        # 4) Inversión sujeto/objeto (misma propiedad)
        inv = canon(f"{Y} {kw} {X}")
        if inv in canon_map:
            ref = canon_map[inv]
            return _v(candidate, "FIX_INVERSION", ref, ref,
                      "dominio/rango invertidos frente a referencia", "deterministic")

    # 5) Residuo -> LM
    return classify_residual_with_llm(candidate, raw_set, known, **llm_kw)


def _v(cand, verdict, corrected, matched, reason, source):
    return {"candidate": collapse(cand), "verdict": verdict, "corrected": corrected,
            "matched_reference": matched, "reason": reason, "_source": source}


def load_candidates_from_csv(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append((row.get("id", ""), collapse(row["title"])))
    return out


# ----------------------------- Main / demo -----------------------------------
def main():
    ref_path = "/mnt/user-data/uploads/auroral-priv_tests.txt"
    csv_path = "/mnt/user-data/uploads/themis_tests_priv.csv"
    raw_set, canon_map, known = load_reference(ref_path)

    print(f"REFERENCIA: {len(raw_set)} tests | vocabulario: {len(known)} términos\n")

    print("=" * 78)
    print("CANDIDATOS REALES (CSV, columna 'title')")
    print("=" * 78)
    counts = {}
    for cid, cand in load_candidates_from_csv(csv_path):
        r = validate(cand, raw_set, canon_map, known)
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        print(f"[{cid:6}] {cand:38} -> {r['verdict']:28} ({r['_source']})")
        if r["verdict"].startswith("FIX"):
            print(f"          corregido: {r['corrected']}")
    print("\nResumen:", dict(sorted(counts.items())))

    print("\n" + "=" * 78)
    print("CANDIDATOS SINTÉTICOS (inyectados para ejercitar cada rama)")
    print("=" * 78)
    synthetic = [
        "Friends SubClassOf RuleTarget",  # type/subclass
        "Action hasAction Rule",          # inversión
        "Rule hasName string",            # residuo -> plausible
        "Item containsNode Node",         # residuo -> malformado
    ]
    for cand in synthetic:
        r = validate(cand, raw_set, canon_map, known)
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()