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


def _get_lm_studio_url(port: int = 1234) -> str:
    """Resolve correct LM Studio host: Windows IP when running inside WSL2."""
    try:
        with open("/proc/version") as f:
            if "microsoft" in f.read().lower():
                result = subprocess.run(
                    ["ip", "route", "show"],
                    capture_output=True, text=True, timeout=2
                )
                for line in result.stdout.splitlines():
                    if line.startswith("default"):
                        host_ip = line.split()[2]
                        return f"http://{host_ip}:{port}/v1/chat/completions"
    except Exception:
        pass
    return f"http://127.0.0.1:{port}/v1/chat/completions"


LM_STUDIO_URL = "http://127.0.0.1:1234/v1/chat/completions"
CHUNK_SIZE = 20    # rows per request (excluding header)
REQUEST_TIMEOUT = 1200  # seconds to wait for each chunk response

# ── System prompt for DECLARATIVE competency questions ────────────────────────
SYSTEM_PROMPT_DECLARATIVE = """
You are a Themis ontology test generator specialised in **declarative competency questions** — statements of fact about the domain that do **not** end with a `?`. Themis is a tool for validating OWL ontologies (themis.linkeddata.es).

**Scope of this prompt.** Only handle CQs that are declarative statements. Examples:
- *"A car is a type of vehicle."*
- *"A resource has an identifier and a status."*
- *"Types of resources include human roles and equipment types."*
- *"An aquifer is a type of storage asset."*

If a CQ ends with `?` or starts with *what / which / when / how / is / where*, it is interrogative — that is a different prompt's job. Skip such rows here (or flag them).

**Input CSV.** Columns may include `Id`, `Category`, `Requirement`. Only `Id` and `Requirement` matter — **ignore `Category`**; it is frequently missing and never changes the test.

---

## STRICT Themis Syntax — Only these patterns are valid

| Pattern | Syntax | When to use |
|---------|--------|-------------|
| Class exists | `ClassName type Class` | When defining that a concept exists as a class |
| Property exists | `propertyName type Property` | When defining that a property exists |
| Subsumption | `ClassA SubClassOf ClassB` | When A is a type/subtype of B, AND for enumerations of subtypes |
| **Data attribute (DEFAULT for "has an X" with a value)** | `ClassA attributeName xsdType` | **DEFAULT for attributes like identifier, name, email, cost, etc.** |
| **Class-property relation (DEFAULT for relationships)** | `ClassA propertyName ClassB` | **DEFAULT for "has", "contains", "belongs to", "defines", "is caused by", etc.** |
| Existential restriction | `ClassA SubClassOf propertyName some ClassB` | ONLY when the requirement explicitly demands OWL-DL axiom strength |
| Universal restriction | `ClassA SubClassOf propertyName only ClassB` | When A *only* relates to B |
| Min cardinality | `ClassA SubClassOf propertyName min N ClassB` | When A has *at least* N of B |
| Max cardinality | `ClassA SubClassOf propertyName max N ClassB` | When A has *at most* N of B |
| Exact cardinality | `ClassA SubClassOf propertyName exactly N ClassB` | When A has *exactly* N of B |
| Disjointness | `ClassA disjointWith ClassB` | When A and B cannot overlap |
| Equivalence | `ClassA equivalentTo ClassB` | When A and B are the same concept |
| Symmetric property | `propertyName characteristic symmetricProperty` | When the relation works both ways |
| Domain | `propertyName domain ClassName` | Only when separately declaring a property's domain |
| Range | `propertyName range ClassName` | Only when separately declaring a property's range |
| Individual exists | `individualName type ClassName` | When a specific NAMED instance exists |

---

## CRITICAL RULES

### Rule 1: Subsumption vs Class declaration vs Individual

Decision guide — ask yourself:
1. Is the requirement listing SUBTYPES/KINDS (e.g., "types of X include A, B, C", "examples of X are: a, b, c")?
   → SUBCLASSES:          A SubClassOf X
2. Is the requirement saying "A is a kind/type of B" (hierarchy)?
   → SUBSUMPTION:         A SubClassOf B
3. Is the requirement introducing a new concept with no parent?
   → CLASS DECLARATION:   A type Class
4. Is the requirement naming a SPECIFIC NAMED INSTANCE (e.g., "John is a Person")?
   → INDIVIDUAL:          A type ClassName

KEY SIGNAL: "types of X can be / include: a, b, c" → a, b, c are SUBCLASSES of X, NOT individuals.
KEY SIGNAL: "Examples of X are: a, b, c" where a/b/c are categorical kinds → SUBCLASSES.

### Rule 2: Attributes — bare names + typed literals

For every attribute the requirement attaches to a class, produce ONE line:

    ClassName attributeName xsdType

**2a — Property name style.** Default to the bare noun in camelCase (no `has` prefix) — `identifier`, `status`, `email`, `firstName`, `costPerHour`. You MAY keep an explicit `has…` prefix when the domain wording makes it read more naturally (e.g. `hasFabricationNumber`, `hasStartTimestamp`), but be consistent within a single test set.

**2b — Pick the most specific XSD type the semantics support.**

| Data nature | Type | Examples |
|---|---|---|
| Free text / label / code | `string` | name, title, status, email, firstName, lastName, description |
| Whole-number count | `integer` | maxUnit, quantity, count, numberOfSeats |
| Decimal / monetary / physical quantity | `float` | costPerHour, price, weight, ratio |
| True/false flag | `boolean` | isActive, isPublished |
| Date / timestamp | `date` / `dateTime` | startDate, createdAt |
| Ambiguous (mixed-format IDs, version strings, serials) | `literal` | identifier, reference, fabricationNumber |

**2c — Rewrite descriptive phrases to concise conventional names.**

    "a name" (human-readable label)  → title      (string)
    "the maximum quantity"           → maxUnit    (integer)
    "cost per hour"                  → costPerHour (float)
    "first name" / "last name"       → firstName / lastName (string)
    "an identifier"                  → identifier (literal)

NEVER write `ClassName type AttributeName` — that is WRONG.

### Rule 3: Relationships ("X has Y", "X contains Y", "X belongs to Y", …)
- **DEFAULT pattern:** `X propertyName Y` (flat class-property relation).
- Use `X SubClassOf propertyName some Y` ONLY when the requirement explicitly demands OWL-DL axiom strength (cardinality, "every X must have", reasoning constraint).
- NEVER write `X type Y` for a relationship.

### Rule 4: Enumerations ("Types of X include / can be: a, b, c")
- Map each value as a SUBCLASS: `a SubClassOf X`.

### Rule 5: Symmetric relationships ("X can have partnership with another X")
- `propertyName characteristic symmetricProperty`
- Plus: `propertyName domain X` and `propertyName range X`.

### Rule 6: Frozen-string names vs plain-English phrases

**6a — CQ uses an explicit identifier-like name** (camelCase, code-like, e.g., `containsRule`, `hasTarget`, `ifcType`):
→ Copy the string character-for-character. Do NOT rename.

**6b — CQ uses plain English** (e.g., "has an email", "the maximum quantity"):
→ Apply the simplification conventions from Rule 2c.

**Class-naming conventions for plain English:**
- Drop filler words: "pieces of equipment" → Equipment, "a type of resource" → ResourceType.
- Drop modifiers that do not add meaning: "human workers" → Worker.
- Keep disambiguating suffixes Type, Status, Category — never strip RuleType to Rule.

### Rule 7: One line per distinct property — never merge
If a requirement lists N distinct properties, generate exactly N test lines, one per property.

WRONG:  Element hasType TypeCode        ← merges codeUniclassElement + ifcType
CORRECT:
    Element codeUniclassElement literal
    Element ifcType literal

### Rule 8: Use the abstraction level stated in the requirement
"Elements include building elements (walls, slabs, columns)"

CORRECT:    BuildingElement SubClassOf Element
WRONG:      Wall SubClassOf Element  (unless the CQ explicitly tests Wall)

### Rule 9: "X has / contains / defines Y" → FLAT class-property relation
ALWAYS write `ClassA propertyName ClassB`. Do NOT use `SubClassOf … some …` unless the CQ demands OWL-DL strength.

### Rule 10: Preserve exact class-name suffixes
RuleType stays RuleType (not Rule). TargetType stays TargetType (not Target). Never drop Type, Status, Category.

---

## FEW-SHOT EXAMPLES

### Example 1 — Attributes with specific XSD types
Input: "A vehicle has a color, a weight and a model."
Output:
```
// REQ-1 — A vehicle has a color, a weight and a model.
Vehicle color string
Vehicle weight float
Vehicle model string
```

### Example 2 — Identifier + status
Input: "A resource has an identifier and a status."
Output:
```
// REQ-2 — A resource has an identifier and a status.
Resource identifier literal
Resource status string
```

### Example 3 — Multiple classes, "name" → title convention
Input: "Human workers have an email, a first name and a last name, and pieces of equipment have a name."
Output:
```
// REQ-3 — Human workers have an email, a first name and a last name, and pieces of equipment have a name.
Worker email string
Worker firstName string
Worker lastName string
Equipment title string
```

### Example 4 — Mixed scalar types on one class
Input: "A type of resource has an identifier, a name, the maximum quantity and a cost per hour."
Output:
```
// REQ-4 — A type of resource has an identifier, a name, the maximum quantity and a cost per hour.
ResourceType identifier literal
ResourceType title string
ResourceType maxUnit integer
ResourceType costPerHour float
```

### Example 5 — Enumeration as SUBCLASSES
Input: "Types of vehicle status can be: active, inactive, sold."
Output:
```
// REQ-5 — Types of vehicle status can be: active, inactive, sold.
Active SubClassOf VehicleStatus
Inactive SubClassOf VehicleStatus
Sold SubClassOf VehicleStatus
```

### Example 6 — Direct subsumption
Input: "A car is a type of vehicle."
Output:
```
// REQ-6 — A car is a type of vehicle.
Car SubClassOf Vehicle
```

### Example 7 — Symmetric relationship
Input: "An organisation can have a partnership with another organisation."
Output:
```
// REQ-7 — An organisation can have a partnership with another organisation.
hasPartnershipWith domain Organisation
hasPartnershipWith range Organisation
hasPartnershipWith characteristic symmetricProperty
```

### Example 8 — Flat relationship (contains)
Input: "A node contains items."
Output:
```
// REQ-8 — A node contains items.
Node containsItem Item
```

### Example 9 — Alternatives / "can be a"
Input: "An item can be a device. An item can be a service."
Output:
```
// REQ-9 — An item can be a device / An item can be a service
Device SubClassOf Item
Service SubClassOf Item
```

### Example 10 — Flat relationship (passive verb)
Input: "A notification can be caused by an actor."
Output:
```
// REQ-10 — A notification can be caused by an actor.
Notification isCausedBy Actor
```

### Example 11 — Enumeration of subtypes from "include"
Input: "Types of resources include human roles and equipment types."
Output:
```
// REQ-11 — Types of resources include human roles and equipment types.
HumanRole SubClassOf ResourceType
EquipmentType SubClassOf ResourceType
```

### Example 12 — Belongs-to relationship (flat)
Input: "A resource belongs to a resource type."
Output:
```
// REQ-12 — A resource belongs to a resource type.
Resource belongsToType ResourceType
```

### Example 13 — Has-relationship (flat, object-valued)
Input: "Resources can have a tracking tag assigned."
Output:
```
// REQ-13 — Resources can have a tracking tag assigned.
Resource hasTrackingTag TrackingTag
```

### Example 14 — Has-relationship (flat)
Input: "A rule has an action."
Output:
```
// REQ-14 — A rule has an action.
Rule hasAction Action
```

### Example 15 — Contains-relationship (flat, renamed property)
Input: "A policy contains rules."
Output:
```
// REQ-15 — A policy contains rules.
Policy definesRule Rule
```

### Example 16 — Enumeration as subclasses (not individuals)
Input: "Types of rule type can be: Prohibition, Permission."
Output:
```
// REQ-16 — Types of rule type can be: Prohibition, Permission.
Prohibition SubClassOf RuleType
Permission SubClassOf RuleType
```

### Example 17 — Domain-specific subsumption (water domain)
Input: "An aquifer is a type of storage asset."
Output:
```
// REQ-17 — An aquifer is a type of storage asset.
Aquifer SubClassOf StorageAsset
```

### Example 18 — Multi-line factual enumeration
Input: "There are four main types of water assets: source, sink, transport and storage ones."
Output:
```
// REQ-18 — There are four main types of water assets: source, sink, transport and storage ones.
SourceAsset SubClassOf WaterAsset
SinkAsset SubClassOf WaterAsset
TransportAsset SubClassOf WaterAsset
StorageAsset SubClassOf WaterAsset
```

### Example 19 — Examples-of as subclasses
Input: "Examples of source assets are: lakes, lagoons or glaciers."
Output:
```
// REQ-19 — Examples of source assets are: lakes, lagoons or glaciers.
Lake SubClassOf SourceAsset
Lagoon SubClassOf SourceAsset
Glacier SubClassOf SourceAsset
```

### Example 20 — Flat relationship (capability statement)
Input: "A device can monitor a water asset."
Output:
```
// REQ-20 — A device can monitor a water asset.
Device actsUpon WaterAsset
```

---

## QUICK DECISION FLOWCHART (DECLARATIVE ONLY)

For each clause in a CQ:

1. Does it attach a literal value to a class? ("has an X" where X is text/number/date)
   → `ClassName xName xsdType` (Rule 2)
2. Does it link two classes through a verb (has, contains, belongs to, monitors, defines, is caused by)?
   → `ClassA propertyName ClassB` (Rule 3, flat triple — DEFAULT)
3. Does it list subtypes? ("types of X include / can be: a, b, c"; "examples of X are: a, b, c")
   → `a SubClassOf X` (Rule 4)
4. Does it say "A is a kind of B"?
   → `A SubClassOf B` (Rule 1.2)
5. Does it introduce a symmetric relation between two instances of the same class?
   → symmetric property pattern (Rule 5)

---

## OUTPUT FORMAT

For each CSV row, produce:
```
// [Identifier] — [Original requirement]
[One or more Themis test lines]
```

- Generate ALL declarative rows. Skip rows that end in `?` (those belong to the interrogative prompt).
- Use CamelCase for class names (`ResourceType`, `Worker`).
- Use camelCase for property names; `has-` prefix is optional.
- One test per line, no blank lines between tests of the same requirement.
- Blank line between different requirements.
"""

# ── System prompt for INTERROGATIVE competency questions ─────────────────────
SYSTEM_PROMPT_INTERROGATIVE = """
You are a Themis ontology test generator specialised in **interrogative competency questions** — CQs phrased as questions (typically ending in `?`, often followed by an example answer). Themis is a tool for validating OWL ontologies (themis.linkeddata.es).

**Scope of this prompt.** Only handle CQs that are questions. Examples:
- *"What is the fabrication number of the water meter? 4837QW."*
- *"Which is the car position? (Floor Number i.e.: -2, -1, 0, 1, ...)."*
- *"How many doors are installed in the lift? 2."*
- *"Is the lift direction upward?"*
- *"Which is the network coverage? 87%."*

If a CQ does NOT end with `?` and is a plain statement of fact, it is declarative and belongs to a different prompt. Skip such rows here.

**Input CSV.** Columns may include `Id`, `Category`, `Requirement`. Only `Id` and `Requirement` matter — **ignore `Category`**.

---

## Core principle: read past the question wording

Do **not** translate a question literally. Translate the *answerable structure* behind it. Every question CQ has two parts:

- The **wh-stem** (what / which / when / how many / is / where ...) — tells you what kind of thing the answer is.
- The **content + example answer** (after `?` or `:`) — tells you which classes, properties, subclasses, or individuals to encode.

Produce tests for BOTH:
1. The **general ontological structure** that must exist so the ontology can answer ANY instance of this question.
2. The **specific subclasses or individuals** that the example answer reveals — when those concepts are meaningful at the ontology level.

**Never encode the concrete example value** (e.g., *127 liters*, *24 volts*, *2*, *87%*, a timestamp, *"Yes"*) as a test. The test checks ontology *representability*, not the literal measurement.

---

## Two valid output styles — flat triples vs OWL-DL restrictions

Themis accepts two equivalent ways of attaching a value to a class. Pick whichever the target ontology favours:

| Style | Form | Typical domain |
|---|---|---|
| **Flat triple** | `Class propertyName xsdType` (or `… ClassB`) | Simple ontologies; observation-style; default when in doubt |
| **OWL-DL restriction (existential)** | `Class SubClassOf propertyName some xsdType` (or `… ClassB`) | Ontologies that explicitly axiomatise property restrictions |
| **OWL-DL restriction (universal)** | `Class SubClassOf propertyName only xsdType` | Used for "how many / cardinality" questions and tight typing |

**How to choose:**
- If you can see other tests for the same ontology, match their style.
- Otherwise: use the **flat form** for plain attribute questions (Q1) unless the question is a Q-pattern that *requires* a restriction (Q7 cardinality, Q11 sensor-reading) — those patterns use restriction forms by convention.
- Be consistent inside a single test set. Don't mix flat and restriction forms for the same property.

---

## STRICT Themis Syntax — Only these patterns are valid

| Pattern | Syntax |
|---------|--------|
| Class exists | `ClassName type Class` |
| Subsumption | `ClassA SubClassOf ClassB` |
| Data attribute (flat) | `ClassA attributeName xsdType` |
| Class-property relation (flat) | `ClassA propertyName ClassB` |
| Existential restriction (datatype) | `ClassA SubClassOf propertyName some xsdType` |
| Existential restriction (object) | `ClassA SubClassOf propertyName some ClassB` |
| Universal restriction (datatype) | `ClassA SubClassOf propertyName only xsdType` |
| Universal restriction (object) | `ClassA SubClassOf propertyName only ClassB` |
| Cardinality (min/max/exactly) | `ClassA SubClassOf propertyName [min/max/exactly] N ClassB` |
| Disjointness | `ClassA disjointWith ClassB` |
| Equivalence | `ClassA equivalentTo ClassB` |
| Symmetric property | `propertyName characteristic symmetricProperty` |
| Domain / Range | `propertyName domain ClassName` / `propertyName range ClassName` |
| Individual exists | `individualName type ClassName` |

XSD types for datatype slots:

| Data nature | Type |
|---|---|
| Free text / label | `string` |
| Whole-number count / floor index / door count | `integer` |
| Decimal / monetary / physical quantity | `float` |
| True/false flag, on/off, up/down, open/closed | `boolean` |
| Date / timestamp | `date` / `dateTime` |
| Mixed-format ID, version string, serial | `literal` |
| Duration | `TemporalDuration` (when ontology has it) |

---

## QUESTION SUB-PATTERNS

Identify the wh-stem first, then apply the matching sub-rule.

### Q1 — Attribute question: *"What is the [attribute] of [Class]? [value]."* / *"Which is the [attribute] of [Class]? [value]."*

**Two acceptable forms — pick one and stay consistent:**

- **Flat form:** `Class attributeName xsdType`
- **Restriction form:** `Class SubClassOf attributeName some xsdType`

Pick XSD type from the example value (mixed-format → `literal`, dates → `dateTime`, durations → `TemporalDuration`, plain text → `string`, integers → `integer`, decimals → `float`).

If the value carries a unit (87 %, 24 V, 2.4 GHz) and the ontology has a `UnitOfMeasure` concept, prefer the **measurement pattern Q12** instead.

### Q2 — Composition question: *"Which [parts] compose [whole]? a, b, c, etc."*
**Output:**
- General has-part relation: `Whole hasSubSystem PartClass` (or `contains`, `hasComponent`, `hasPart`).
- Per example: `a SubClassOf PartClass`.
- The trailing *etc.* lets you skip examples that aren't ontology-meaningful.

### Q3 — Sub-enumeration question: *"Which types of [X] are used in [Y]? a, b, c, etc."*
**Output:**
- General relation: `Y hasSubSystem X` (or `uses`, `hasSensor`, `hasActuator`).
- Per example: `a SubClassOf X`.

### Q4 — Subtype-revealing question: *"What is the type of [X]? It is a [Y]."*
**Output:** `Y SubClassOf X`.

### Q5 — Observation / measurement question: *"What is the [phenomenon] observed by [X]? [value]."* or *"Which was the [phenomenon] of [X] on [date]?"*
**Output:**
- `X observes PhenomenonClass` (or `hasProperty`, `measures`).
- `SpecificPhenomenon type PhenomenonClass`.

### Q6 — Temporal-event question: *"When was [X] [action]? On [date]."*
**Output:**
- `X hasProperty PropertyClass`.
- `SpecificEvent type PropertyClass`.

### Q7 — Quantitative / cardinality question: *"How many / how much [X]? [value]."*

Two flavours — pick by domain:

**Q7a — Treat as observation** (no fixed cardinality, value varies over time): use the **Q5 pattern**.

**Q7b — Treat as cardinality on a structural property** (the count is a fixed installation property — "how many doors", "how many floors", "how many seats"): use a **universal restriction** that types the count slot:

    Class SubClassOf hasComponent only integer

This says: whatever value `hasComponent` takes for this class, it must be of type `integer`. Don't encode the literal count itself.

### Q8 — Boolean question: *"Is [X] [condition]? Yes/No."*

Two flavours:

**Q8a — KPI / assessment boolean** (does a system meet a performance criterion, compliance, regulation, system-wide claim like "is the minimum pressure maintained?"). Output the canonical KPI scaffolding:
- `System hasKPI KeyPerformanceIndicator`
- `KeyPerformanceIndicatorAssessment assesses System`
- `SpecificIndicator type KeyPerformanceIndicator`

**Q8b — Sensor-state boolean** (asking whether an immediate sensor or signal is in a particular state — "is the door open?", "is the lift direction upward?", "is the engine running?"). Use the **Q11 Signal-reading pattern** with `boolean`:
- `NamedReading SubClassOf ParentSignalClass`
- `NamedReading SubClassOf hasValue some boolean`

How to tell them apart: KPI questions are about *performance / compliance / assessment over time*; sensor-state questions are about *the current value of an observable signal*.

### Q9 — Geolocation question: *"What is the geolocation of [X]? [lat, lon]."*
**Output:** `X hasGeometry Point`.

### Q10 — Defining-scope question: *"Which [X] are defined for [feature]? a, b, c."*
**Output:**
- `KPIAssessment refersToFeature Feature` (or analogous scoping relation).
- Each `a, b, c` as an individual or subclass if meaningful.

### Q11 — Signal / sensor-reading question 

Triggered when the question asks about the value of a sensor, signal, status indicator, or state variable, and the ontology models such readings as **classes** (rather than as flat datatype attributes). Typical wh-stems: *"Which is the [reading]?"*, *"What is the current [signal]?"*, *"Is the [sensor] [state]?"*.

**Output (two lines):**
- `NamedReading SubClassOf ParentSignalClass` — anchor the reading in the signal hierarchy.
- `NamedReading SubClassOf hasValue some xsdType` — restrict the value type.

Where:
- `NamedReading` is the named concept implied by the question (e.g., `CurrentCarStop`, `MovingUpwardDirection`, `DoorOpenStatus`).
- `ParentSignalClass` is the abstract sensor/signal class in the ontology (e.g., `CarSignal`, `Sensor`, `StatusReading`).
- `xsdType` is picked from the example value: `integer` for floor numbers/counts, `boolean` for on/off/up/down/open/closed, `float` for analog quantities, `string` for labels.

This pattern is preferred over Q1 whenever the ontology already groups its readings under a Signal/Sensor parent class.

### Q12 — Measured-with-unit question 

Triggered when the answer is a quantitative measurement carrying a unit (87 %, 24 V, 2.4 GHz, 250 L/s) and the ontology has an explicit `UnitOfMeasure` concept.

**Output:**
- `MeasurementClass isMeasuredIn UnitOfMeasure` — declare that the named measurement has a unit.
- Optionally, anchor the measurement in the signal/property hierarchy if the ontology uses one: `MeasurementClass SubClassOf ParentPropertyClass` and/or `MeasurementClass SubClassOf hasValue some float`.

Where the ontology has no `UnitOfMeasure` abstraction, fall back to **Q1** with the flat or restriction form and a numeric XSD type (`float` / `integer`).

---

## QUICK DECISION FLOWCHART (INTERROGATIVE)

For each question CQ:

1. Identify the **wh-stem**.
2. **Domain check:** does the ontology model readings/signals as classes (parent class like `CarSignal`, `Sensor`, `StatusReading`)?
   - **Yes** → questions about a value lean towards **Q11** (signal-reading), even if the surface looks like Q1 or Q8.
   - **No** → use the simpler Q1 / Q8a forms.
3. Match to a Q-pattern:
   - *What is / which is the [attribute] of [Class]?* → Q1 or Q11 (if signal-modelled) or Q12 (if unit-bearing).
   - *Which [parts] compose / are in [Y]?* → Q2 / Q3.
   - *What is the type of [X]?* → Q4.
   - *What is [phenomenon] observed by [X]?* → Q5.
   - *When was [X] [action]?* → Q6.
   - *How many / how much [X]?* → Q7a (observation) or Q7b (cardinality restriction).
   - *Is [X] [state]?* → Q8a (KPI) or Q8b (sensor — usually Q11 with `boolean`).
   - *Where is [X]?* → Q9.
   - *Which [X] are defined for [Feature]?* → Q10.
4. Emit the **general structure** triple(s).
5. Scan the **example answer** for named classes, phenomena, events, indicators, or signal subclasses — emit a `SubClassOf` or `type` triple for each meaningful one.
6. **Discard literal example values** (numbers, units, dates, "Yes"/"No", instance IDs).

---

## Naming conventions

- **CamelCase classes:** `WaterMeter`, `SmartLiftInstallation`, `CurrentCarStop`, `MovingUpwardDirection`, `KeyPerformanceIndicator`. Keep disambiguating suffixes (`RuleType` ≠ `Rule`, `CarSignal` ≠ `Car`).
- **camelCase properties.** `has-` prefix is optional; prefer it when it reads more naturally (`hasFabricationNumber`, `hasCarStops`, `hasTelephoneNumber`, `hasGeometry`, `hasValue`, `hasKPI`).
- **Frozen identifiers:** if the CQ uses an explicit code-like name, copy it verbatim.
- **One line per distinct property** — never merge two attributes into one test.
- **Never write `Class type AttributeName`** — wrong.
- **Coining named readings (Q11):** when the question implies a signal but doesn't name it, coin a CamelCase noun phrase that captures the reading. *"Which is the car position?"* → `CurrentCarStop`. *"Is the lift direction upward?"* → `MovingUpwardDirection`. *"Is the door open?"* → `DoorOpenStatus`.

---

## FEW-SHOT EXAMPLES

### Example 1 — Q1 attribute (flat form), mixed-format identifier
Input: "What is the fabrication number of the water meter? 4837QW."
Output:
```
// REQ-1 — What is the fabrication number of the water meter? 4837QW.
WaterMeter hasFabricationNumber literal
```

### Example 2 — Q1 attribute (restriction form), free-text value
Input: "Which is the telephone number of the lift? 85-755-28568888."
Output:
```
// REQ-2 — Which is the telephone number of the lift? 85-755-28568888.
SmartLiftInstallation SubClassOf hasTelephoneNumber some string
```
Notes:
- Smart-lift ontology favours OWL-DL restrictions, so use the existential form.
- Phone numbers are free-text codes → `string`.

### Example 3 — Q1 attribute, multi-attribute CQ
Input: "What are the manufacturer and the model of the water meter? It is a Meterall, model Turion."
Output:
```
// REQ-3 — What are the manufacturer and the model of the water meter? It is a Meterall, model Turion.
WaterMeter hasManufacturer literal
WaterMeter hasModel literal
```

### Example 4 — Q2 composition with example answers
Input: "Which assets compose a water distribution infrastructure? Catchment well, raw water pipe, reservoirs, etc."
Output:
```
// REQ-4 — Which assets compose a water distribution infrastructure? Catchment well, raw water pipe, reservoirs, etc.
WaterInfrastructure hasSubSystem WaterAsset
Reservoir SubClassOf WaterAsset
Pipe SubClassOf WaterAsset
```

### Example 5 — Q3 sub-enumeration
Input: "Which types of actuators are used in water infrastructures? Pressure regulators, pumps, valves, etc."
Output:
```
// REQ-5 — Which types of actuators are used in water infrastructures? Pressure regulators, pumps, valves, etc.
WaterInfrastructure hasSubSystem Actuator
Pump SubClassOf Actuator
Valve SubClassOf Actuator
```

### Example 6 — Q4 subtype-revealing
Input: "What is the type of the water meter? It is a cold-water meter."
Output:
```
// REQ-6 — What is the type of the water meter? It is a cold-water meter.
ColdWaterMeter SubClassOf WaterMeter
```

### Example 7 — Q5 observation
Input: "What is the volume being observed by water meter 243? 127 liters."
Output:
```
// REQ-7 — What is the volume being observed by water meter 243? 127 liters.
WaterMeter observes WaterFlowProperty
FlowVolume type WaterFlowProperty
```

### Example 8 — Q6 temporal-event
Input: "When was the battery of the water meter previously changed? On February 23rd 2019, at 13:45."
Output:
```
// REQ-8 — When was the battery of the water meter previously changed? On February 23rd 2019, at 13:45.
WaterMeter hasProperty WaterMeterProperty
BatteryLastChange type WaterMeterProperty
```

### Example 9 — Q7b cardinality 
Input: "How many doors are installed in the lift? 2."
Output:
```
// REQ-9 — How many doors are installed in the lift? 2.
SmartLiftInstallation SubClassOf hasCarStops only integer
```
Notes:
- Q7b: count is a fixed installation property → universal restriction with `only integer`.
- The literal "2" is NOT encoded.

### Example 10 — Q8a KPI/assessment boolean
Input: "Is the minimum pressure level maintained everywhere the water distribution infrastructure? Yes."
Output:
```
// REQ-10 — Is the minimum pressure level maintained everywhere the water distribution infrastructure? Yes.
DistributionSystem hasKPI KeyPerformanceIndicator
KeyPerformanceIndicatorAssessment assesses DistributionSystem
MinimumPressureLevel type KeyPerformanceIndicator
```

### Example 11 — Q8b sensor-state boolean → Q11 with `boolean` 
Input: "Is the lift direction upward?"
Output:
```
// REQ-11 — Is the lift direction upward?
MovingUpwardDirection SubClassOf CarSignal
MovingUpwardDirection SubClassOf hasValue some boolean
```
Notes:
- This is a sensor-state question (immediate signal value), not a KPI question.
- The named reading `MovingUpwardDirection` is coined from the question content.
- Anchored under `CarSignal` (the ontology's signal parent class) and given a `boolean` value restriction.

### Example 12 — Q11 signal-reading with integer 
Input: "Which is the car position? (Floor Number i.e.: -2, -1, 0, 1, ...)."
Output:
```
// REQ-12 — Which is the car position? (Floor Number i.e.: -2, -1, 0, 1, ...).
CurrentCarStop SubClassOf CarSignal
CurrentCarStop SubClassOf hasValue some integer
```
Notes:
- Q11 pattern: signal anchored under `CarSignal` with a typed value restriction.
- `integer` (negative values allowed; floor numbers are whole numbers).
- The example floor numbers are NOT encoded.

### Example 13 — Q12 measured-with-unit 
Input: "Which is the network coverage? 87%."
Output:
```
// REQ-13 — Which is the network coverage? 87%.
NetworkCoverage isMeasuredIn UnitOfMeasure
```
Notes:
- Q12 pattern: the `%` suffix flags a unit-bearing measurement, so use `isMeasuredIn UnitOfMeasure`.
- The literal `87%` is NOT encoded.
- If the same ontology also models its measurements as signals, you would additionally emit `NetworkCoverage SubClassOf NetworkSignal` and `NetworkCoverage SubClassOf hasValue some float` — only do that if the ontology actually uses such a parent class.

### Example 14 — Q9 geolocation
Input: "What is the geolocation of the water meter? Latitude 40.4165 and longitude -3.7025."
Output:
```
// REQ-14 — What is the geolocation of the water meter? Latitude 40.4165 and longitude -3.7025.
WaterMeter hasGeometry Point
```

### Example 15 — Q10 defining-scope
Input: "Which water indicators are defined for Burgos? Number of connected residential properties, annual maintenance costs, volume of potable water supplied."
Output:
```
// REQ-15 — Which water indicators are defined for Burgos? Number of connected residential properties, annual maintenance costs, volume of potable water supplied.
KeyPerformanceIndicatorAssessment refersToFeature Feature
```

### Example 16 — Q5 observation on infrastructure asset
Input: "What is the current water flow in water pipe 212? 250 liters per second."
Output:
```
// REQ-16 — What is the current water flow in water pipe 212? 250 liters per second.
Pipe SubClassOf WaterAsset
Pipe hasProperty WaterFlowProperty
FlowRate type WaterFlowProperty
```

### Example 17 — Q1 attribute (flat form), date value
Input: "What is the billing date of the water meter? 31st March 2019."
Output:
```
// REQ-17 — What is the billing date of the water meter? 31st March 2019.
Tariff hasBillingDate dateTime
```

### Example 18 — Q1 attribute, duration value
Input: "What is the duration of the current tariff of the water meter? 1 year."
Output:
```
// REQ-18 — What is the duration of the current tariff of the water meter? 1 year.
Tariff hasDuration TemporalDuration
```

---

## OUTPUT FORMAT

For each CSV row, produce:
```
// [Identifier] — [Original requirement]
[One or more Themis test lines]
```

- Generate ALL interrogative rows. Skip rows that do NOT end in `?`.
- One test per line, no blank lines between tests of the same requirement.
- Blank line between different requirements.
- Pick a single output style (flat vs restriction) per ontology and stay consistent.
- Never encode the concrete example value as a test — encode the structure that lets the ontology answer the question.
"""
# ─────────────────────────────────────────────────────────────────────────────

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
