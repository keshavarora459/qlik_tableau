# Qlik → Fabric Mapping: LLM Rules Implementation Plan

Status of the Qlik mapping agent, what changed, and what remains. Written
against `mapping (2)/mapping/` unless another repo is named.

---

## 1. The core problem

The mapping agent advertised itself as LLM-driven and was not.

There was exactly **one** LLM call site in the whole service
(`BaseConverter._call_llm`), reachable from exactly one converter
(`dashboard_objects`). That converter returned early whenever the
deterministic lookup recognised the chart type — which is almost always — so
in practice the model never ran.

Evidence from the two captured production runs: every `rationale` in
`MAPPINGRESPONSE.md` (FleetVision KSA, 49 visuals) and `mapping2.md`
(Weather Analytics, 59 visuals) is the deterministic f-string template
`"The Qlik Sense 'X' visual maps directly to Fabric 'Y' visual."`. Only 4
visuals across both apps (`qlik-variable-input`) ever reached the model.

Meanwhile `rules/__init__.py` exported `ALL_RULES` — ~70 hand-written
conversion rules — and **nothing imported it**. `src/converters/measures/`
and `src/converters/dimensions/` each contained only `rules.py`, with no
`converter.py` to consume them.

Everything else was regex or dict lookup:

| Stage | How it worked | LLM? |
|---|---|---|
| Measures → DAX | `DAXConverter`, ~20 chained `re.sub` | no |
| Dimensions → DAX column | `DimensionMapper.to_dax` | no |
| Visual type | 40-entry dict in `VisualMapper` | no |
| Field roles / axes | static `ROLES` dict + `difflib` **in the generation agent** | no |
| Colours | passthrough, mostly dropped | no |
| Variables | raw passthrough, never converted | no |
| Column types | hardcoded English keyword lists | no |
| M-query | per-connector template | no |

---

## 2. Design principle: baseline-first

Every LLM stage keeps the deterministic result as a **floor**:

1. The deterministic converter runs and produces a draft. Never skipped.
2. The draft, the Qlik source and the resolved schema go to the model, which
   is asked to **correct** the draft rather than translate from scratch.
3. `services/dax_guard.py` validates the answer. On any problem the draft is
   kept and the rejection is counted.

So enabling a stage can raise quality but cannot fall below the regex floor.
This matters because bad DAX produces a *wrong number*, not an obviously
broken chart. Each stage has its own flag (`USE_LLM_MEASURES`, etc.) and can
be turned off independently.

---

## 3. What was built

### 3.1 New shared plumbing

| File | Purpose |
|---|---|
| `services/prompt_builder.py` | Filters `ALL_RULES` by `type`, budgets them, builds system/user prompts per stage. **This is the module that was missing** — without it the rules were dead code. |
| `services/schema_context.py` | Renders the resolved model (tables, columns with Qlik→Fabric renames, measures, relationships with One/Many sides) as prompt context, so the model picks from real names instead of inventing them. |
| `services/dax_guard.py` | Validates candidate DAX; `choose()` picks between baseline and candidate. |
| `services/llm_usage.py` | Per-run counters (attempted / succeeded / failed / accepted / rejected), via `ContextVar` so concurrent runs don't mix. |

`src/converters/llm_client.py` gained `generate_text()` alongside the existing
`generate_structured_response()`. DAX goes through the text path deliberately:
wrapping an expression in a JSON string forces the model to escape every quote
and bracket, which is where malformed output comes from.

### 3.2 New rules files

All registered in `rules/__init__.py` and reachable via `ALL_RULES`.

| File | Rules | Covers |
|---|---|---|
| `src/converters/variables/rules.py` | 17 (`var1`–`var17`) | The four variable kinds and what each becomes in Fabric |
| `src/converters/columns/rules.py` | 14 (`col1`–`col14`) | Data type, `summarizeBy`, format strings, naming |
| `src/converters/mquery/rules.py` | 23 (`mq1`–`mq23`) | Qlik load script → Power Query M |
| `src/converters/dashboard_objects/rules.py` | 14 (`dash1`–`dash14`) | Rewritten: added field roles, aggregation, colour, combo/scatter axes |
| `rules/measures_advanced_rules.py` | +4 (`rule45`–`rule48`) | Dollar-sign expansion, measure aliases, what-if refs, set analysis with variables |

Current totals — all reach the prompts:

```
measures           48    dimensions         19
variables          17    columns            14
mquery             23    dashboard_objects  14      = 135 injected
```

### 3.3 Two rule metadata fields

**`scope: "output_format"`** — excluded from prompts. `MEASURES_CORE rule15`
said *"Return JSON with 'expression', 'dataType', 'formattext'"* and
`DIMENSIONS rule7` said *"Return a valid JSON object..."*, while
`prompt_builder` asks for a bare DAX expression. Two contradictory format
instructions in one message makes the response unparseable roughly half the
time. Both are now tagged and skipped.

**`priority: "critical"`** — 48 rules tagged. Truncation is priority-first,
so a growing rule set can never silently evict a correctness rule (cross-table
`RELATED()`, "no `MATCH` in DAX", dollar-sign expansion) in favour of a
cosmetic one. A dropped critical rule logs at `ERROR`.

Budgets were raised (`LLM_RULES_CHAR_BUDGET` 4000 → 12000,
`LLM_SYSTEM_PROMPT_LIMIT` 7000 → 24000) because at 4000 chars **21 measure
rules and 11 M-query rules were being silently dropped**. The Tableau side
caps at 7000 because it packs the schema into the *system* message; here the
schema travels in the user message, so the system prompt is smaller.

### 3.4 New converters

| File | Behaviour |
|---|---|
| `src/converters/measures/converter.py` | Regex draft → LLM correction → `dax_guard` → pick. Skips stubs and measures with no source expression. |
| `src/converters/dimensions/converter.py` | Same, calculated dimensions only. Plain field dimensions map 1:1 to a column and hierarchies are level lists — neither needs the model. Validates with `is_measure=False`. |
| `src/converters/variables/converter.py` | Deterministic classifier + LLM for the ambiguous cases. |

### 3.5 Dashboard converter rewrite

Two structural fixes in `src/converters/dashboard_objects/converter.py`:

**The early return is gone.** The deterministic type is now a *hint passed
into the prompt* and the *fallback*, not a short circuit. The model runs for
every visual, so field roles, aggregations and colours are produced for all
of them rather than left to downstream guesswork.

**`source_block()` no longer whitelists.** It rebuilt each visual from 17 keys
and dropped everything else — including `color`, `kpi_styling`,
`custom_coloring`, `style_and_formatting`, `reference_lines`, `image_url` and
`button_action`, all of which `unified-parsing` had already extracted. That is
why, in FleetVision, **4 action buttons had no navigation target and 2 image
visuals had no URL**. It now preserves the source object and only normalises
aliases.

The widened `OUTPUT_SCHEMA` adds `field_roles[]` (field, role, entity,
property, is_measure, aggregation), `colors{}` and `formatting{}`.

Guard rails on the model's answer:
- `visual_type` must be in `VALID_VISUAL_TYPES`, else the rule-based type wins.
- An `unsupported` verdict from the deterministic layer is **not** overturnable.
- Colours: only literal hex accepted, and a colour the **parser** resolved
  always beats the model's. Verified — the parsed `#0065B3` won over a
  model-supplied `#999999`, and a fabricated `"not-a-color"` was rejected.
- Unresolvable fields come back as `Unbound` and force `requires_review`.

### 3.6 Evidence-based confidence scoring

`BaseConverter.DummyConfidence` returned a flat **0.95 for every visual**,
whatever happened. Because 0.95 sits above the 0.85 review threshold, **no
visual was ever flagged** — a visual that fell back to a generic table with
every field unbound scored the same as a clean conversion.

`ConfidenceEvaluator.evaluate_visual_conversion()` replaces it with eight
weighted checks, each of which is something that can actually be wrong in
the emitted report:

| Check | Weight | Fails when |
|---|---|---|
| `visual_type_recognized` | 0.35 | Fell through to a generic Table |
| `required_roles_populated` | 0.30 | e.g. a `barChart` with nothing on Y — renders blank |
| `fields_bound_to_model` | 0.25 | A field could not be bound to a real entity |
| `visual_renders_natively` | 0.20 | Needs an AppSource package this pipeline never registers |
| `all_fields_assigned_a_role` | 0.15 | A source field received no role |
| `layout_resolved` | 0.10 | Zero or negative size |
| `colors_preserved` | 0.05 | Source specified colours that were lost |
| `title_resolved` | 0.05 | Fell back to the chart type as a placeholder |

Two behaviours worth noting:

- **Dataless visuals are exempt.** A textbox, button or image legitimately
  binds nothing, so the field checks are skipped rather than failed.
- **The model's self-reported confidence can lower the score but never raise
  it.** A model claiming 0.99 on a visual that failed a structural check is
  not evidence of anything.

Verified on the smoke fixture: a combo chart with one unbound field now
scores **0.75 / medium / requires_review**, where the old code returned
0.95 / high / no review.

### 3.7 Honest telemetry

`build_llm_status()` hardcoded `converted: true` and named the Groq model
regardless of whether a request was ever sent. It now derives from the run's
own counters and reports per-stage usage, which stages are enabled, and
rejection reasons.

---

## 4. Verified results

### Variables (`USE_LLM_VARIABLES=false`, deterministic classifier only)

| App | In | reserved | literal | parameter | Reaching Parameters table |
|---|---|---|---|---|---|
| FleetVision KSA | 29 | 25 | 2 | 2 | **4** (was 29) |
| Weather Analytics | 29 | 25 | 4 | 0 | **4** (was 29) |

- 25 Qlik reserved locale variables (`ThousandSep`, `DateFormat`,
  `MoneyFormat`) correctly excluded and routed to model format settings.
- Date serials resolved: `44562 → 2022-01-01`, `45657 → 2024-12-31`.
- `vTopN`, `vMeasure` → parameters with
  `[vTopN Value] = SELECTEDVALUE('vTopN'[vTopN], 10)`.

### `dax_guard`

All cases pass, including the two added after the Qlik-docs audit:

| Case | Verdict |
|---|---|
| `SUM('Trips'[fare])` | accept |
| `SUM('Trips'[fare]` | reject — unbalanced |
| `MATCH('Trips'[fare], 1)` | reject — not a DAX function |
| `SUM('FACT_TABLE'[fare])` | reject — placeholder table |
| `VAR _x = SUM(...)` (no RETURN) | reject |
| `VAR VALUE = 1 RETURN VALUE` | reject — reserved VAR name |
| `[Total 'Sales'[Cost]]` | reject — nested brackets |
| `DIVIDE(..., $(vTarget), 0)` | reject — unexpanded `$()` |
| `[Total Cost] - [Total Amount]` | accept — plain measure refs |

### Test suite

**155 passed** (from 86). `test_rules_loading` was updated — it asserted
`ALL_RULES` equalled exactly the three old lists — and `tests/test_llm_conversion.py`
added, covering: variable expansion (nested, circular, parameterised, set
analysis), the DAX guard, measure aliases, all 18 audited Qlik object types,
variable classification, M-query validation, and the confidence checks.

Two test-authoring mistakes worth recording, both caught by the tests
themselves:

- I asserted critical rules form a strict *prefix* after truncation. They
  don't — packing is greedy, so a shorter rule legitimately fits where a
  longer one didn't. The greedy behaviour is correct; the assertion was
  rewritten to the invariant that matters: no non-critical rule is kept while
  a critical one is dropped.
- I asserted a model claim of 0.99 leaves an evidence score of 1.0 unchanged.
  `min(evidence, llm_score)` correctly lowers it to 0.99 — that *is*
  lowering. The implementation was right; the expectation was wrong.

---

## 5. Findings from the Qlik documentation audit

Checked against Qlik's official docs; each was reproduced by running the code.

### 5.1 21 of 59 official object types collapse to a generic table

| Group | Falls back |
|---|---|
| Classic native (21) | 2 — `bulletchart`, `writetable` |
| Nebula native (18) | 3 — `sn-bullet-chart`, `sn-distplot`, `sn-navigation-menu` |
| Bundle extensions (20) | 16 — `qlik-date-picker`, `qlik-multi-kpi`, `sn-grid-chart`, `sn-org-chart`, `qlik-smart-pivot`, `qlik-radar-chart`, `sn-word-cloud`, `sn-nlg-chart`, `sn-text`, `sn-network-chart`, `sn-layout-container`, `qlik-variance-waterfall`, `sn-shape`, `sn-video-player`, `qlik-animator`, `qlik-trellis-container` |

Several already have targets the code knows **elsewhere**:
`CATEGORY_TO_VISUAL_TYPE` maps `date_picker → dateSlicer`, and the generation
agent's `visual_catalog` maps `bulletchart → gauge`. They are keyed on
`object_category`, so a visual arriving with `chart_type: "qlik-date-picker"`
never reaches them.

### 5.2 Suffix-stripping bug

`normalize_qlik_type` strips `-ext`/`_ext`, then falls through to a bare
`endswith("ext")`:

```
'sn-text' → 'text' → endswith('ext') → 't' → default → tableEx
```

The Text bundle object becomes a Table. `text-image` survives by accident.

### 5.3 Dollar-sign expansion is never resolved

Qlik's docs: `$(vName)` is textual substitution performed **before** the
expression is parsed. The pipeline never expands it:

```
$(vSales)                              -> $(vSales)
Sum(Amount) / $(vTarget)               -> DIVIDE(SUM('Sales'[Amount]), $(vTarget), 0)
Sum({<Year={$(vCurrentYear)}>} Amount) -> CALCULATE(SUM(...), Year = "$(vCurrentYear)")
```

The first two are invalid DAX. The third is worse — a **string literal**
filtering on the text `"$(vCurrentYear)"`, which silently matches nothing and
returns a plausible-looking wrong number.

`dax_guard` now rejects any expression containing `$(`, so this fails loudly
instead of shipping. The expansion pass itself is still to be written (§6.1).

### 5.4 Measure-label references produce malformed DAX

Qlik's docs: a measure's name inside an expression is an alias for that
measure. The regex converter qualifies *inside* the brackets:

```
[Total Revenue] - [Total Cost]  ->  [Total Revenue] - [Total 'Sales'[Cost]]
```

Bracket counts still balance, so a naive check passes it. `dax_guard`'s
nested-bracket check now catches it.

### 5.5 Correct Fabric targets per variable kind

| Qlik variable | Correct target |
|---|---|
| Reserved (`ThousandSep`, `DateFormat`) | Model culture + format strings — **not** a parameter row |
| Value used as a constant | Inline literal, or a constant measure |
| Value driven by `variable-input` | **Own** what-if table + `SELECTEDVALUE` measure |
| Expression (`vSales = Sum(Sales)`) | A DAX **measure** — a Parameters row holding `"Sum(Sales)"` is inert |
| `$(vName)` in an expression | Substitute before conversion |

The generation agent's current `build_parameters_tmdl` has two further
structural problems: it builds **one shared** table
(`Parameter`/`Value`/`Label`/`Order`), so a slicer filters every parameter at
once; and `Value` is typed `text`, so `vTopN = 10` arrives as `"10"`.

---

## 6. Remaining work

### ✅ 6.1 Dollar-sign expansion pass — DONE
`services/variable_expander.py`, wired into `coordinator_agent` **before**
`extract_measures`. Scans rather than regex-matches so nested `$($(x))` and
parameterised `$(vName(a,b))` work; recursive with a depth cap and cycle
detection. Verified:

```
$(vSales)                              -> Sum(Amount)
Sum(Amount) / $(vTarget)               -> Sum(Amount) / 1000
Sum({<Year={$(vYear)}>} Amount)        -> Sum({<Year={2024}>} Amount)
$(vNested)                             -> Sum(Amount) / 1000
$(vParam(Amount, 2))                   -> Sum(Amount) * 2
$(vMissing)                            -> /* UNRESOLVED_VARIABLE: vMissing */
$(vLoopA)                              -> /* CIRCULAR_VARIABLE: vLoopA */
```

Originals preserved as `<field>_raw`. Expansion runs over measures,
dimensions and every visual's expression fields.

### ✅ 6.2 Measure-alias resolution — DONE
`DAXConverter.qualify_columns` now stashes bracketed spans before
qualification. `[Total Revenue] - [Total Cost]` stays intact instead of
becoming `[Total Revenue] - [Total 'Sales'[Cost]]`, while
`Sum(Amount) - [Total Cost]` still qualifies the column correctly.

### ✅ 6.3 Visual type coverage — DONE
19 object types added and the `ext` suffix bug fixed (only `-ext`/`_ext` are
stripped now, so `sn-text` no longer becomes `t`). Audited against Qlik's
registry:

| Group | Before | After |
|---|---|---|
| Classic native (21) | 2 fell back | **0** |
| Nebula native (18) | 3 fell back | **0** |
| Bundle extensions (20) | 16 fell back | **2** |

The remaining two — `sn-org-chart`, `sn-network-chart` — are deliberate:
neither has a Power BI native equivalent, and a parent/child table is the
documented substitution (matching the generation agent's `visual_catalog`).

### ✅ 6.4 Column typing converter — DONE
`src/converters/columns/converter.py`. Batched per table. Only sends columns
the engine did not type authoritatively — a field Qlik already calls a
date/number is not second-guessed (rule col1). Refuses to create a column the
model invents, and forces `summarize_by=none` on anything flagged as a key
(rule col5) regardless of what the model returned.

### ✅ 6.5 M-query converter — DONE (off by default)
`src/converters/mquery/converter.py` with `validate_mquery()`: checks
`let`/`in`, balanced delimiters (M-aware — `""` is the escape), a real
connector call, no unexpanded `$()`, no untranslated Qlik syntax
(`RESIDENT`, `CROSSTABLE`, `ApplyMap`, `AUTOGENERATE`, `INLINE`), no local
`C:\` or `lib://` paths, and no mis-cased M prefixes.
**`USE_LLM_MQUERY` stays `false`** until the golden-file harness exists.

### 6.6 Generation-agent consumption
`field_roles` and `colors` are emitted but not yet read.
`az-wa-repo-generationagent/app/report/visual_builder.py` should prefer them
over its `ROLES` table and `difflib` matching, keeping those as fallback.

### 6.7 Parameters table restructure
Per-variable tables + `SELECTEDVALUE` measures; filter `kind == "reserved"`
out entirely; type numeric parameters as numbers.

### 6.8 Parsing gap
Variable-input objects do not carry `variable_name` — the binding falls back
to the object ID (`dgEzRMq`, `hSFqB`). `vTopN`/`vMeasure` were caught only by
the name-hint rule. Fix in
`unified-parsing/parsers/qlik/enrichment/visuals.py`.

### 6.9 Golden-file regression harness
`az-wa-repo-generationagent/tests/conftest.py` already loads a real
FleetVision mapping. Add Weather Analytics as a second fixture and snapshot
per-app metrics: visuals by type, unbound-field count, default-colour count,
inert buttons/images, and the full set of generated DAX. **Do this before
enabling `USE_LLM_MQUERY` or trusting LLM DAX in production.**

---

## 7. Configuration

```bash
USE_LLM_MEASURES=true       # measure DAX refinement
USE_LLM_DIMENSIONS=true     # calculated-column refinement
USE_LLM_VISUALS=true        # visual type, field roles, colours
USE_LLM_VARIABLES=true      # variable classification
USE_LLM_COLUMNS=true        # reserved: converter not yet written
USE_LLM_MQUERY=false        # off until the harness exists

LLM_RULES_CHAR_BUDGET=12000
LLM_SYSTEM_PROMPT_LIMIT=24000
LLM_MAX_CONCURRENCY=3
LLM_MAX_RETRIES=3
GROQ_MODEL=openai/gpt-oss-120b
```

**Cost.** Removing the early return means one call per visual (~50–130 per
app) plus one per measure and per non-reserved variable. Concurrency is capped
at 3 by a module-level semaphore. If latency becomes a problem, batch visuals
per sheet rather than per visual.

---

## 8. How to add a rule

1. Add to the relevant `rules.py` with `type`, `id`, `rule`.
2. Add `"priority": "critical"` if a wrong answer produces wrong numbers or
   invalid output.
3. Add `"scope": "output_format"` if it describes the response envelope —
   it will be excluded from prompts.
4. Run `pytest tests/test_mapping_agent.py` — well-formedness, budget and
   exclusion are all asserted.

Rule ids must be unique **within a type**, not globally.
