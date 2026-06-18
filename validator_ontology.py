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
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
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
    _struct_lower = {k.lower() for k in STRUCT_KW}
    known = set()
    for l in lines:
        for t in l.split():
            if t.lower() not in _struct_lower:
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


# Normalización (aplícala ANTES de comparar)
1. Recorta y colapsa espacios.
2. Normaliza SOLO las palabras clave estructurales, ignorando mayúsculas:
   subclassof/subclassOf/SubClassof/... -> SubClassOf ; type -> type
3. Los nombres de clases, individuos y propiedades son SENSIBLES a mayúsculas
   y deben coincidir EXACTAMENTE con la REFERENCIA. No los "corrijas".

# Qué detectar (TODA corrección debe estar respaldada por una línea concreta de REFERENCIA)
- INVERSION: candidato "A prop B" pero en REFERENCIA está "B prop A" (dominio/rango
  intercambiados), o "B q A" con q inversa declarada de prop. Corrige al orden de REFERENCIA.
  Incluye el caso combinado: "A SubClassOf B" en candidato pero "B type A" en REFERENCIA
  (inversión de sujeto/objeto Y cambio de palabra clave simultáneos).
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
    system = SYSTEM_TEMPLATE.replace("{{REFERENCIA}}", "\n".join(sorted(reference_lines)))
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
        toks = [t for t in candidate.split() if t.lower() not in {k.lower() for k in STRUCT_KW}
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
def _validate_deterministic(candidate, raw_set, canon_map):
    """Heurísticas deterministas (pasos 1-4). Devuelve resultado o None si no resuelve."""
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
        # 3) type <-> SubClassOf (mismos sujeto/objeto, distinta palabra clave)
        if kw in ("type", "SubClassOf"):
            other = "type" if kw == "SubClassOf" else "SubClassOf"
            alt = f"{X} {other} {Y}"
            if alt in canon_map:
                ref = canon_map[alt]
                return _v(candidate, "FIX_TYPE_VS_SUBCLASS", ref, ref,
                          f"referencia usa '{other}', no '{kw}'", "deterministic")
        # 3b) Inversión sujeto/objeto + cambio type<->SubClassOf combinados
        # Ejemplo: CSV "Item SubClassOf Device" / TXT "Device type Item"
        if kw in ("type", "SubClassOf"):
            other = "type" if kw == "SubClassOf" else "SubClassOf"
            inv_other = canon(f"{Y} {other} {X}")
            if inv_other in canon_map:
                ref = canon_map[inv_other]
                return _v(candidate, "FIX_INVERSION", ref, ref,
                          f"invertido y '{kw}'->'{other}' frente a referencia", "deterministic")
        # 4) Inversión sujeto/objeto con misma palabra clave
        # Cubre también: CSV "Item SubClassOf Device" / TXT "Device SubClassOf Item"
        inv = canon(f"{Y} {kw} {X}")
        if inv in canon_map:
            ref = canon_map[inv]
            return _v(candidate, "FIX_INVERSION", ref, ref,
                      "sujeto/objeto invertidos frente a referencia", "deterministic")

    return None  # no resuelto por heurísticas


def validate(candidate, raw_set, canon_map, known, **llm_kw):
    """Validación completa: heurísticas deterministas y, solo si no resuelven, LM."""
    result = _validate_deterministic(candidate, raw_set, canon_map)
    if result is not None:
        return result
    return classify_residual_with_llm(candidate, raw_set, known, **llm_kw)


def _v(cand, verdict, corrected, matched, reason, source):
    return {"candidate": collapse(cand), "verdict": verdict, "corrected": corrected,
            "matched_reference": matched, "reason": reason, "_source": source}


def load_candidates_from_csv(path):
    """Lee candidatos de un CSV. Acepta dos formatos de columna:
    - 'title': una línea de test por fila.
    - 'Generated test': una o más líneas de test por fila (celda multilínea)."""
    out = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        col = "Generated test" if "Generated test" in (reader.fieldnames or []) else "title"
        for row in reader:
            cid = row.get("id", "")
            cell = row.get(col, "") or ""
            for line in cell.splitlines():
                line = collapse(line)
                if line:
                    out.append((cid, line))
    return out


def load_cq_map(path):
    """Lee el CSV de candidatos y devuelve {id: 'Competency question'}."""
    cq_map = {}
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "Competency question" not in (reader.fieldnames or []):
            return cq_map
        for row in reader:
            cid = row.get("id", "")
            if cid and cid not in cq_map:
                cq_map[cid] = row.get("Competency question", "") or ""
    return cq_map


# ----------------------------- API programática ------------------------------
def validate_csv(reference_path: str, candidates_csv_path: str, **llm_kw):
    """Valida los candidatos de *candidates_csv_path* contra la referencia de
    *reference_path* (TXT generado por ontology_test_generator).

    Pipeline de dos fases:
      1) Heurísticas deterministas sobre todos los candidatos.
      2) Solo el residuo no resuelto se envía al LM.
    Los resultados se fusionan respetando el orden original.

    Devuelve (results, cq_map) donde results es una lista de tuplas
    (id, result_dict) y cq_map es {id: 'Competency question'}.
    """
    raw_set, canon_map, known = load_reference(reference_path)
    candidates = load_candidates_from_csv(candidates_csv_path)
    cq_map = load_cq_map(candidates_csv_path)

    # Fase 1: heurísticas deterministas
    results = [None] * len(candidates)
    residual_idx = []
    for i, (cid, cand) in enumerate(candidates):
        r = _validate_deterministic(cand, raw_set, canon_map)
        if r is not None:
            results[i] = (cid, r)
        else:
            residual_idx.append(i)

    # Fase 2: solo el residuo va al LM
    for i in residual_idx:
        cid, cand = candidates[i]
        r = classify_residual_with_llm(cand, raw_set, known, **llm_kw)
        results[i] = (cid, r)

    return results, cq_map


def write_results_csv(results, cq_map, output_path: str) -> None:
    """Escribe *results* (de validate_csv) en un CSV con columnas
    id, Competency question, Generated test, verdict -- el formato que
    build_comparison.py espera para encontrar el veredicto de cada test."""
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "Competency question", "Generated test", "verdict"])
        for cid, r in results:
            cq = cq_map.get(cid, "")
            generated_test = r["corrected"] if r["corrected"] else r["candidate"]
            writer.writerow([cid, cq, generated_test, r["verdict"]])


def run(reference_path: str, candidates_csv_path: str, output_path: str, **llm_kw) -> str:
    """Valida y guarda el resultado. Devuelve la ruta del CSV generado."""
    results, cq_map = validate_csv(reference_path, candidates_csv_path, **llm_kw)
    write_results_csv(results, cq_map, output_path)
    return output_path


# ----------------------------- GUI ---------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Validador de tests THEMIS")
        self.geometry("860x520")
        self.configure(bg="#1e1e2e")
        self._ref_path = None
        self._csv_path = None
        self._results = None
        self._cq_map = {}
        self._build_ui()

    def _label(self, parent, text):
        return tk.Label(parent, text=text, bg="#1e1e2e", fg="#cdd6f4",
                        font=("Segoe UI", 10, "bold"), anchor="w")

    def _separator(self):
        tk.Frame(self, bg="#45475a", height=1).pack(fill="x", padx=12, pady=8)

    def _build_ui(self):
        pad = {"padx": 12, "pady": 5}

        self._label(self, "① Archivo de referencia (TXT)").pack(fill="x", padx=12, pady=(12, 2))
        ref_row = tk.Frame(self, bg="#1e1e2e")
        ref_row.pack(fill="x", **pad)
        self.lbl_ref = tk.Label(ref_row, text="Ningún archivo seleccionado",
                                bg="#1e1e2e", fg="#6c7086", font=("Segoe UI", 10), anchor="w")
        self.lbl_ref.pack(side="left", padx=(0, 8))
        tk.Button(ref_row, text="Elegir…", command=self._pick_ref,
                  bg="#89b4fa", fg="#1e1e2e", font=("Segoe UI", 9, "bold"),
                  relief=tk.FLAT, padx=10, cursor="hand2").pack(side="right")

        self._label(self, "② Archivo de candidatos (CSV, columna 'title')").pack(fill="x", padx=12, pady=(4, 2))
        csv_row = tk.Frame(self, bg="#1e1e2e")
        csv_row.pack(fill="x", **pad)
        self.lbl_csv = tk.Label(csv_row, text="Ningún archivo seleccionado",
                                bg="#1e1e2e", fg="#6c7086", font=("Segoe UI", 10), anchor="w")
        self.lbl_csv.pack(side="left", padx=(0, 8))
        tk.Button(csv_row, text="Elegir…", command=self._pick_csv,
                  bg="#89b4fa", fg="#1e1e2e", font=("Segoe UI", 9, "bold"),
                  relief=tk.FLAT, padx=10, cursor="hand2").pack(side="right")

        self._separator()

        run_row = tk.Frame(self, bg="#1e1e2e")
        run_row.pack(fill="x", **pad)
        self.btn_run = tk.Button(
            run_row, text="Validar", command=self._on_validate,
            bg="#a6e3a1", fg="#1e1e2e", font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=16, pady=6, cursor="hand2")
        self.btn_run.pack(side="left")
        self.lbl_status = tk.Label(run_row, text="", bg="#1e1e2e", fg="#f38ba8",
                                   font=("Segoe UI", 9))
        self.lbl_status.pack(side="left", padx=12)

        self._label(self, "Resultados").pack(fill="x", padx=12, pady=(6, 2))
        self.txt_out = scrolledtext.ScrolledText(
            self, height=18, wrap=tk.WORD,
            bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
            font=("Consolas", 9), state=tk.DISABLED, relief=tk.FLAT,
            padx=6, pady=6)
        self.txt_out.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        save_row = tk.Frame(self, bg="#1e1e2e")
        save_row.pack(fill="x", padx=12, pady=(0, 12))
        tk.Button(save_row, text="Guardar resultados…", command=self._save_results,
                  bg="#cba6f7", fg="#1e1e2e", font=("Segoe UI", 9, "bold"),
                  relief=tk.FLAT, padx=14, pady=5, cursor="hand2").pack(side="right")

    def _pick_ref(self):
        path = filedialog.askopenfilename(
            title="Selecciona el archivo de referencia",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self._ref_path = path
            self.lbl_ref.config(text=os.path.basename(path), fg="#cdd6f4")

    def _pick_csv(self):
        path = filedialog.askopenfilename(
            title="Selecciona el archivo de candidatos",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self._csv_path = path
            self.lbl_csv.config(text=os.path.basename(path), fg="#cdd6f4")

    def _set_widget_text(self, widget, text):
        widget.config(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        if text:
            widget.insert(tk.END, text)
        widget.config(state=tk.DISABLED)

    def _on_validate(self):
        if not self._ref_path:
            messagebox.showwarning("Falta archivo", "Selecciona primero el archivo de referencia.")
            return
        if not self._csv_path:
            messagebox.showwarning("Falta archivo", "Selecciona primero el archivo de candidatos.")
            return

        self.btn_run.config(state=tk.DISABLED)
        self.lbl_status.config(text="Validando…", fg="#f9e2af")
        self._set_widget_text(self.txt_out, "")

        def worker():
            try:
                raw_set, canon_map, known = load_reference(self._ref_path)
                candidates = load_candidates_from_csv(self._csv_path)
                self._cq_map = load_cq_map(self._csv_path)

                lines = [f"REFERENCIA: {len(raw_set)} tests | vocabulario: {len(known)} términos\n"]

                # Fase 1: heurísticas deterministas
                results = [None] * len(candidates)
                residual_idx = []
                for i, (cid, cand) in enumerate(candidates):
                    r = _validate_deterministic(cand, raw_set, canon_map)
                    if r is not None:
                        results[i] = (cid, r)
                    else:
                        residual_idx.append(i)

                n_det = len(candidates) - len(residual_idx)
                lines.append(
                    f"Fase 1 (determinista): {n_det}/{len(candidates)} resueltos"
                    f" | Fase 2 (LM): {len(residual_idx)} candidatos\n"
                )

                # Fase 2: solo el residuo va al LM
                self.after(0, lambda: self.lbl_status.config(
                    text=f"LM: {len(residual_idx)} candidatos…", fg="#f9e2af"))
                for i in residual_idx:
                    cid, cand = candidates[i]
                    r = classify_residual_with_llm(cand, raw_set, known)
                    results[i] = (cid, r)

                counts = {}
                for cid, r in results:
                    counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
                    lines.append(f"[{cid:6}] {r['candidate']:38} -> {r['verdict']:28} ({r['_source']})")
                    if r["verdict"].startswith("FIX"):
                        lines.append(f"          corregido: {r['corrected']}")
                lines.append("\nResumen: " + str(dict(sorted(counts.items()))))

                text = "\n".join(lines)
                self.after(0, self._handle_done, text, results, None)
            except Exception as e:
                self.after(0, self._handle_done, None, None, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_done(self, text, results, error):
        self.btn_run.config(state=tk.NORMAL)
        if error:
            self.lbl_status.config(text=f"Error: {error}", fg="#f38ba8")
            return
        self.lbl_status.config(text="Hecho.", fg="#a6e3a1")
        self._results = results
        self._set_widget_text(self.txt_out, text)

    def _save_results(self):
        if not self._results:
            messagebox.showinfo("Nada que guardar", "Ejecuta primero la validación.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("Text files", "*.txt"), ("All files", "*.*")],
            title="Guardar resultados")
        if not path:
            return
        if path.lower().endswith(".csv"):
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["id", "Competency question", "Generated test", "verdict"])
                for cid, r in self._results:
                    cq = self._cq_map.get(cid, "")
                    generated_test = r["corrected"] if r["corrected"] else r["candidate"]
                    writer.writerow([cid, cq, generated_test, r["verdict"]])
        else:
            with open(path, "w", encoding="utf-8") as f:
                for cid, r in self._results:
                    f.write(json.dumps({"id": cid, **r}, ensure_ascii=False) + "\n")
        messagebox.showinfo("Guardado", f"Resultados guardados en:\n{path}")


if __name__ == "__main__":
    app = App()
    app.mainloop()