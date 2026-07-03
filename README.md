# Generación de pruebas de ontologías a partir de requisitos mediante modelos de lenguaje

Trabajo Fin de Máster — Máster en Ciencia de Datos, Universidad Politécnica de Madrid (ETSI Informáticos).

**Autor:** Víctor González Bacelar
**Tutores:** Raúl García Castro y María Poveda Villalón

Este repositorio contiene el código y el corpus de evaluación de un método que usa un modelo de lenguaje (LLM) desplegado en local para generar automáticamente pruebas de verificación de ontologías en la sintaxis de [Themis](https://themis.linkeddata.es), a partir de los requisitos textuales de la ontología (preguntas de competencia y requisitos declarativos).

> ⚠️ **Nota sobre este README:** generado a partir de la memoria del TFM, de la estructura del repositorio verificada el 3 de julio de 2026, y de la información que me confirmaste directamente (Python 3.12.0, librerías reales incluyendo `difflib`, y licencia).

## Estructura del repositorio

```
.
├── Corpus_of_tests/     # Corpus de 8 ontologías de evaluación (requisitos + pruebas de referencia)
├── Ontologia/           # Todas las ontologías y sus preguntas de competencia (CQ) usadas para pruebas manuales
├── Presentations/       # Ontología sencilla de ejemplo para probar el flujo completo en Themis
├── codigo/              # Scripts Python de la tubería (pipeline) y prompts de sistema
└── analysis_report.txt  # Informe de análisis agregado sobre el corpus completo
```

La carpeta `codigo/` contiene:

| Script | Etapa | Función |
|---|---|---|
| `pipeline.py` | — | Orquestador; interfaz gráfica integrada para ejecutar todas las etapas sobre el corpus |
| `requirements.py` | Etapa 1 | Clasificación de requisitos (declarativos/interrogativos) y generación de pruebas |
| `terminology.py` | Etapa 2 | Extracción del vocabulario de la ontología y alineación terminológica |
| `tests.py` | Etapa 3 | Validación de las pruebas generadas contra los tests derivables de la ontología |
| `build_comparison.py` | Etapa 4 | Construcción del fichero de comparación (`comparison.csv`) |
| `analyze_tests.py` | — | Cálculo de métricas de evaluación (precisión, F1, Jaccard, etc.) |
| `filter_csv.py` | — | Utilidad para filtrar ficheros CSV de cara a pruebas manuales en Themis |
| `ontology_test_generator.py` | — | Extracción de tests derivables directamente de los axiomas de la ontología |
| `prompts/system_prompt_declarative.txt` | — | Prompt de sistema para requisitos declarativos |
| `prompts/system_prompt_interrogative.txt` | — | Prompt de sistema para preguntas de competencia |

## Requisitos

- **Python 3.12.0**
- **[LM Studio](https://lmstudio.ai/)**, con un modelo compatible descargado (el TFM usa **Gemma 4 E4B-it**, cuantización GGUF Q4_K_M) sirviendo en `http://127.0.0.1:1234`.
- Bibliotecas Python:
  - [`rdflib`](https://rdflib.readthedocs.io/) `==7.6.0` — parseo y consulta de ontologías RDF/OWL.
  - `tkinter` — interfaces gráficas de cada script (en Linux puede requerir instalar el paquete del sistema, p. ej. `sudo apt install python3-tk`; en Windows/macOS ya viene con el intérprete estándar de Python).
  - El resto de dependencias (`csv`, `json`, `re`, `sys`, `pathlib`, `typing`, `threading`, `urllib`, `argparse`, `itertools`, `os`, `io`, `difflib`) son de la biblioteca estándar de Python y no requieren instalación adicional.

Puedes instalar la única dependencia externa con el `requirements.txt` incluido en este repositorio:

```bash
pip install -r requirements.txt
```

## Instalación

```bash
git clone https://github.com/victox85/TFM_Generation-of-ontology-tests-using-Large-Language-Models.git
cd TFM_Generation-of-ontology-tests-using-Large-Language-Models
pip install -r requirements.txt
```

1. Instala [LM Studio](https://lmstudio.ai/) y descarga un modelo compatible con formato de chat (el TFM usa Gemma 4 E4B-it).
2. Inicia el servidor local de LM Studio (por defecto en `http://127.0.0.1:1234`).

## Uso

### Opción A: pipeline integrado (recomendado para procesar el corpus completo)

```bash
python codigo/pipeline.py
```

Permite elegir entre procesar una sola carpeta de ontología o todas las subcarpetas de una raíz, y seleccionar qué etapas ejecutar (la Etapa 1 es obligatoria; las etapas 2-4 son opcionales y dependen de las anteriores).

### Opción B: scripts individuales (para depurar una etapa concreta)

```bash
python codigo/requirements.py     # Etapa 1: generación de pruebas
python codigo/terminology.py      # Etapa 2: alineación terminológica
python codigo/tests.py            # Etapa 3: validación contra la ontología
python codigo/build_comparison.py # Etapa 4: construcción del fichero de comparación
python codigo/analyze_tests.py    # Cálculo de métricas de evaluación
```

Cada script individual muestra el prompt de sistema en modo solo lectura, permite adjuntar los ficheros de entrada necesarios y ofrece un botón de envío al modelo local.

### Estructura esperada de cada carpeta de ontología

```
Corpus_of_tests/<nombre_ontologia>/
├── <ontologia>.ttl        # Fichero de la ontología (Turtle, u OWL/RDF/JSON-LD/XML)
├── requirements.csv       # Requisitos textuales (declarativos e interrogativos)
└── <tests_de_referencia>.csv   # Pruebas de referencia (gold standard) en sintaxis Themis
```

## Corpus de evaluación

El corpus incluye 8 ontologías de los proyectos DELTA, AURORAL y COGITO, y de las extensiones SAREF (saref4lift, saref4watr). Los detalles de recopilación, depuración y composición de cada carpeta están documentados en la memoria del TFM (sección 3.1).

## Citación

Si usas este trabajo, por favor cita:

> González Bacelar, V. (2026). *Generación de pruebas de ontologías a partir de requisitos mediante modelos de lenguaje* [Trabajo Fin de Máster, Universidad Politécnica de Madrid].

## Licencia

Este proyecto se distribuye bajo licencia [MIT](https://choosealicense.com/licenses/mit/) (ver fichero `LICENSE`). Es la licencia de código abierto más simple y permisiva: permite a cualquiera usar, copiar, modificar y distribuir libremente este código (incluso en proyectos privados o comerciales), con la única condición de conservar el aviso de copyright. Se ha elegido por ser la opción más "cómoda" de uso libre que pediste; si en algún momento este trabajo tiene continuidad en un proyecto de investigación con posible interés en patentes, coméntalo con tus tutores, ya que en ese caso podría convenir más [Apache-2.0](https://choosealicense.com/licenses/apache-2.0/) (misma permisividad, pero con cláusula explícita de concesión de patentes).
