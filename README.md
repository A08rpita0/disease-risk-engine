# Disease Correlation & Risk Prediction Engine

A laboratory data → cohort detection → Disease Master mapping → explainable risk →
personalized action plan pipeline, with a medical analytics dashboard on top.

**This is a disease-risk signalling system, not a diagnostic system.** It identifies
clinically established patterns in laboratory data and maps them to the supplied Disease
Master. Correlations are grounded in published guidelines and studies, cited per cluster;
correctness is established by the technical validation suites described below. Every
finding carries its reasoning, its triggering parameters and its missing information.

---

## Quick start

```bash
pip install -r requirements.txt
python -m uvicorn app:app --port 8000
# open http://127.0.0.1:8000
```

Verify the build:

```bash
python tools/validate.py       # all seven suites; exits non-zero on any failure
python tools/consistency.py    # suites 4 and 5 on their own, with per-link detail
python tools/coverage.py       # suite 6 on its own, with the per-marker breakdown
python tests/test_engine.py    # 116 unit checks on extraction and normalization
```

Rebuild the machine-readable Disease Master after editing the workbook:

```bash
python tools/build_disease_master.py path/to/DiseaseMaster_Updated.xlsx
```

---

## Architecture

```
Input ─► Extraction ─► Normalization ─► Standardized Patient ─► Cohort/Correlation Engine
                                                                        │
        Results ◄─ Recommendation Engine ◄─ Disease Risk Engine ◄─ Disease Master
```

| Stage | Module | Responsibility |
|---|---|---|
| Input | `app.py` | Upload handling, size/type limits, sample runner, both UI routes |
| Extraction | `engine/extract.py` | PDF / CSV / TXT / arbitrarily-nested JSON → raw observations |
| Normalization | `engine/normalize.py` | Alias resolution, unit conversion, reference ranges, duplicates, derived values, abnormality grading |
| Cohort engine | `engine/cohorts.py` | Cluster detection, redundancy control, confidence |
| Risk engine | `engine/risk.py` | Weighted mapping onto Disease Master rows, evidence levels |
| Recommendations | `engine/recommend.py` | Action plan from Disease Master guidance + action library |
| Orchestration | `engine/pipeline.py` | Wires the stages, assembles the enriched record |
| Config | `engine/config.py` | Loads and **validates** every cross-reference at start-up |

### Interfaces

Two front ends are served from the same APIs and the same engine — nothing behind them
differs, and either can be removed without touching the backend.

| Route | Directory | Notes |
|---|---|---|
| `/` | `web/` | **The interface in use.** Card-based dashboard with tab navigation. |
| `/v2` | `web_v2/` | Alternative dense clinical-analytics layout: patient banner, H/L result flags, reference-range gauges, sidebar navigation, print stylesheet. Kept for reference; not linked from the main interface. |

`app.py` mounts `/v2` only when `web_v2/` exists, so deleting that directory removes the
route cleanly:

```bash
rm -rf web_v2      # removes the alternative layout entirely
```

All clinical content lives in `config/` as JSON and can be edited without touching code.

```
config/
  disease_master.json      generated from the workbook; all 22 columns preserved verbatim
  parameters.json          209 parameters, 904 aliases, units, sex-aware ranges, decision bands
  cohorts/*.json           82 clusters, grouped by domain, each with citations
  recommendations.json     action library keyed by cohort and by parameter
  unmappable.json          conditions deliberately not mapped, with the reason
```

---

## How the Disease Master's fields are used

The workbook was inspected first, and each column was assigned a concrete role rather
than being carried along as decoration.

| Disease Master column | How the engine uses it |
|---|---|
| **Disease/Medical Condition** | The join key. Every cohort→disease link names a row exactly; a typo fails validation at start-up. |
| **Classification** | Distinguishes a `Disease` from a `Risk Condition/Syndrome` in the output wording. |
| **Related Profile(s)** | Shown per condition; also the basis for the cross-profile design of the cohorts. |
| **Related Markers/Tests** | The free-text marker list was the source for each cohort's `expected_parameters`, which is what turns it into a checkable list and makes *data coverage* computable. |
| **High-Risk Indicators** | The single most important field. Each cohort's trigger conditions implement the abnormal pattern this column describes, and each link quotes the sentence in its `dm_basis`. |
| **Confirmatory/Diagnostic Tests** | Surfaced verbatim on every finding as "what would actually confirm this". |
| **Severity/Urgency Level** | Parsed into a structured triage tier plus a separate *conditional* tier (see below). |
| **Prevention/Lifestyle Guidance** | Used verbatim as disease-level recommendations. |
| **Recommended Next Step** | Used verbatim as the referral/consultation recommendation. |
| **Definition, Symptoms, Prognosis, Complications, Risk Factors, Differentials** | Shown in the expandable Disease Master record on each finding. |
| **ICD-10 Code** | Displayed as a badge. |
| **Review Status / Source** | Preserved verbatim and shown inside each condition's Disease Master record, so the provenance the workbook records travels with the output. |
| Profile Coverage / Field Legend / Data Quality Notes / Unclear Mappings sheets | Loaded and shown in the dashboard's Reference tab. |

One row — *"Note: Tumour Marker Test profile overlaps with Cancer Profile"* — is a sheet
annotation rather than a condition. It is excluded, and the exclusion is reported.

### Urgency parsing

The Severity column mixes unconditional statements with conditional ones:

- `"Emergency - life-threatening condition"` → tier **emergency**
- `"Emergency if acute chest pain...; otherwise needs monitoring"` → tier **monitoring**, conditional tier **emergency**

Treating the second kind as an unconditional emergency would make the system cry wolf on
every raised cholesterol. 5 conditions are unconditionally urgent; 29 carry a conditional
escalation that is shown as *"can escalate"* rather than driving the triage banner.

---

## Cohorts (clusters)

82 cohorts, each with parameters, conditions, minimum evidence thresholds, disease links
with weights, and cited clinical references. Two evaluation modes:

- **weighted** — fires when at least `min_triggers` distinct trigger conditions are met.
- **count_of** — fires on an explicit count, used where the clinical definition *is* a
  count: metabolic syndrome's 3-of-5 (IDF/AHA harmonised), the ISTH DIC score,
  pancytopenia's three cell lines, multi-nutrient malabsorption.

### Cross-profile by design

The brief asked that parameters from different profiles feed one cohort where clinically
supported. Examples actually implemented:

| Cohort | Draws from |
|---|---|
| Metabolic Syndrome Cluster | Lipid Profile + Diabetes Monitoring + BMI & BP + Liver Profile |
| Raised ALP with normal Ca/PO4 | Bone Health + Liver Profile + Electrolyte Profile |
| Cardiovascular risk (4 cohorts) | Lipid + Cardiac + Diabetes Monitoring + Inflammation + Kidney |
| Adrenal Insufficiency Pattern | Electrolyte Profile + Hormones (low Na + high K + low cortisol) |
| Multiple Micronutrient Deficiency | Vitamin + Anemia Studies + Stool + Liver + Electrolyte + Mineral |
| Peripheral Neuropathy Risk | Neurological + Vitamin Profile + Diabetes Monitoring |
| CRAB / myeloma pattern | Blood Counts + Liver Profile + Electrolyte + Kidney |
| Haemolysis Pattern | Blood Counts + Liver Profile + Hb Electrophoresis |

Each cross-profile cohort records *why* it crosses, and the dashboard shows that rationale.

### Avoiding double counting

Parameters carry a `redundancy_group`. LDL, non-HDL and ApoB all measure atherogenic
particle burden, so inside one cohort only the strongest signal from each group carries
full weight; the rest are retained in the trace at 25% weight and labelled
*"counted at reduced weight to avoid double-counting the same biology"*.

### Link gating

A panel cohort (e.g. the acute febrile illness panel) links to several diseases. Links
are gated with `requires_any`, so a positive dengue test raises dengue while a **negative**
malaria test in the same panel raises nothing. Without this the engine would report
malaria risk from a negative malaria test — the gating is covered by a regression test.

---

## Risk scoring

For each disease, contributions are pooled across every cohort that fired:

```
contribution = link_weight × cohort_confidence × role_factor
score        = 1 − Π (1 − contribution)          # noisy-OR
```

Role factors: `primary 1.0`, `supporting 0.7`, `downstream_risk 0.6`, `differential 0.45`.

Noisy-OR rather than a sum because it is bounded in [0,1] without clipping, more
independent evidence always helps but with diminishing returns, and — the point — every
contribution stays individually inspectable. The dashboard renders each one with its bar,
its weight, its cohort confidence and the Disease Master sentence behind it.

Evidence bands: **High** ≥ 0.72, **Moderate** ≥ 0.48, **Low** ≥ 0.28, **Limited** ≥ 0.12.
Below 0.12 nothing is reported.

**Data sufficiency can only lower a level, never raise it.** Coverage is the
contribution-weighted fraction of each disease's relevant markers that were actually
measured. Below 60% the level is capped at Moderate; below 34% at Limited, and the finding
says how thin the data was and which tests would sharpen it.

---

## Coverage with limited parameters

Measured by `tools/validate.py` on synthetic panels drawn from real panel compositions,
60 records per size:

| Parameters available | Useful output | All findings explained |
|---|---|---|
| 10 | 85.0% | yes |
| 15 | 91.7% | yes |
| 20 | 95.0% | yes |
| 30 | 100.0% | yes |

*Useful* means the engine either produced explained findings or correctly reported that
nothing abnormal was present. Target was ~80%.

---

## Handling messy input

| Situation | Behaviour |
|---|---|
| Different names for one test | 904 aliases, plus British/American spelling folding, bracket stripping, `SGOT/AST` splitting, and specimen-prefix removal (`S. Creatinine`) |
| Different units | Per-parameter conversion tables (mmol/L, µmol/L, g/L, 10⁹/L, lakhs/cumm…); an unrecognised unit is flagged, never silently assumed |
| Different reference ranges | The range printed on the patient's own report wins over the dictionary; standard clinical decision bands (ADA, KDIGO, NCEP) still supply the grading |
| Male/female ranges | Sex-aware ranges and band sets; with sex unknown the widest interval is used **and annotated** |
| Missing / null values | Dropped. Never imputed. |
| Duplicate parameters | Resolved to the most informative record, with what was dropped, why, and whether values conflicted, all reported |
| Different JSON structures | Recursive walk handles arrays of test objects, flat name/value maps, value-objects keyed by name, deep nesting, split low/high keys and nested range objects |
| Metadata blocks | Skipped — unless they actually contain results, so a payload nesting results under `laboratory` is not thrown away |
| Scanned PDF | Reported as having no text layer and needing OCR, rather than returning an empty result silently |

---

## Evidence basis

Every correlation the engine asserts is traceable to two things: a published clinical
reference, and the Disease Master field it was matched on. Both are enforced at load
time — a cluster with no citation, or a disease link with no `dm_basis`, is a
configuration **error** and the engine refuses to start.

| Measure | Current |
|---|---|
| Clusters citing a clinical reference | 82 / 82 |
| Distinct references cited | 145 |
| Average citations per cluster | 1.9 |
| Disease links quoting a Disease Master field | 255 / 255 |
| Parameters with a named threshold source | 21 |

The references are guideline and primary-literature sources, not general knowledge — for
example ADA *Standards of Care* for the glycaemic thresholds, KDIGO for eGFR and
albuminuria staging, NCEP ATP III and the 2018 AHA/ACC cholesterol guideline for lipids,
the harmonised IDF/AHA/NHLBI statement for metabolic syndrome, ISTH for the DIC score,
WHO for anaemia thresholds and the 6th-edition semen reference limits, ACR/EULAR for the
rheumatology classification criteria, Endocrine Society guidance for the pituitary,
adrenal and vitamin D thresholds, and the Revised Atlanta criteria for pancreatitis.
Each citation carries a one-line note saying what it establishes; the dashboard shows
them per cluster.

---

## Technical validation

`tools/validate.py` runs seven suites and exits non-zero on any failure.

| # | Suite | What it proves | Result |
|---|---|---|---|
| 1 | **Configuration** | Every parameter reference, disease name, gate and weight resolves; every cluster cites a reference; every link quotes its basis; no cluster can fire on an entirely normal panel | 0 errors, 0 warnings |
| 2 | **Evidence** | Citation and basis coverage is measured, not assumed | 82/82 clusters, 255/255 links |
| 3 | **Clinical cases** | 9 hand-built cases with expectations stated up front, including two negative controls | 9/9 |
| 4 | **Consistency** | Determinism, bounded scores, monotonicity, mutually exclusive patterns, complete audit trails, negative results never raising their disease | 16/16 |
| 5 | **Disease Master mapping** | Every link exercised end to end through the real pipeline | 121/121 mappable conditions reachable, 115/115 primary links, 0 dead mappings |
| 6 | **Parameter coverage** | Every Disease Master marker resolves to a parameter; no orphan parameters; every condition uses the markers its own row names | 282 markers, 100% resolved; 0 orphans; 128/128 |
| 7 | **Coverage** | Useful output at realistic panel sizes | 90% / 92% / 95% / 100% |

`tests/test_engine.py` adds 116 unit checks on the normalization edge cases.

### What the consistency suite actually checks

- **Determinism** — the same input produces byte-identical output.
- **Bounds** — every cluster confidence, disease score and coverage figure stays in range.
- **Monotonicity** — worsening a value can never *lower* the risk it drives. Checked on
  LDL→cardiovascular, TSH→hypothyroidism, ferritin→iron deficiency, eGFR→CKD, HbA1c→diabetes.
- **Mutually exclusive patterns** — hypo/hyperthyroid, iron-deficient/thalassaemic,
  hypo/hypergonadotropic and polycythaemia/pancytopenia never co-fire.
- **Negative suppression** — a negative result never raises the disease it tests for,
  checked across ten infection and autoimmune markers.
- **Healthy-panel silence** — a full 162-parameter panel of mid-range values raises nothing.
- **No invention** — adding in-range results to an abnormal panel never introduces a new
  condition.
- **Audit trail completeness** — every reported finding has an explanation, at least one
  contribution, at least one triggering parameter, and a Disease Master basis on every
  contribution.

### Parameter coverage

Disease coverage and *parameter* coverage are different questions, and suite 6 answers
the second one in three directions:

- **Marker → parameter.** The Disease Master's `Related Markers/Tests` column is free
  prose. Splitting it yields 301 tokens, of which 19 are prose fragments rather than
  markers ("if included in panel)", the allergen examples inside a specific-IgE phrase).
  Of the 282 real markers, **241 resolve directly by alias and 37 through a documented
  umbrella term** (`CBC`, `LFT markers`, `stool analysis`, `semen analysis`, `blood
  pressure`…). Four are declared as having no assay in the catalogue. **Unresolved: 0.**
- **Parameter → cluster.** 206 of 209 parameters are evaluated by at least one cluster.
  The other three are declared *reported-only* — extracted, converted, graded and shown,
  but deliberately not driving any cluster (basophil %, urine pH, ABO group). **Orphans: 0.**
- **Condition → its own markers.** For all **128** linked conditions, at least one marker
  named on that condition's own Disease Master row is evaluated by a cluster that links
  to it. This is what proves a mapping fires on the *right* markers rather than by some
  unrelated route.

The umbrella terms, prose fragments, missing assays and reported-only parameters are all
declared in `config/marker_map.json`, so the metric stays honest: anything not declared
there and not resolvable by alias is reported as a gap and fails the suite.

### Mapping reachability

The mapping suite is the strongest check in the set. For each of the 255 cohort→disease
links it synthesises a patient *from that cluster's own trigger conditions* and pushes it
through the real pipeline; the linked condition must come out the other end. A link that
resolves by name but can never fire is a dead mapping, and the suite fails on it.

Three outcomes are distinguished:

- **Reachable** (230 links) — fires end to end.
- **Combination-only** (25 links) — the cluster fires but this link alone stays under the
  0.12 reporting floor. These are deliberately weak differentials that contribute evidence
  alongside others; not defects.
- **Documented as not lab-mappable** (7 conditions) — recorded with reasons in
  `config/unmappable.json`, so mapping coverage is measured against what is actually
  mappable rather than being quietly inflated.

This suite found four real defects during development, all since fixed: a panel cluster
crediting diseases whose tests were negative, an isolated-APTT cluster that fired on a
completely healthy panel because two of its three triggers asserted *normality*,
categorical results carrying zero severity so Rh-negative status could never reach the
reporting floor, and Paget's disease having no reachable path despite the Disease Master
describing its exact biochemical signature.

---

## What the system deliberately will not do

- It does not compute statistical correlation from one patient's report. All associations
  come from the configured, cited, medically established relationships.
- It does not invent missing data.
- It does not name drugs, doses or treatments.
- It does not present findings as diagnoses.
- It does not raise a condition the Disease Master says has no laboratory marker.
  Eight such conditions are listed in `config/unmappable.json` with the reason and, where
  one exists, the test that would make them mappable — adding an IGRA would make
  Tuberculosis mappable, adding venom-specific IgE would do the same for Insect Sting
  Allergy. Mapping coverage is measured against what is actually mappable, so the gap is
  visible rather than hidden.

---

## Extending it

- **New parameter** — add an entry to `config/parameters.json` with aliases and a range.
- **New cohort** — add to any file in `config/cohorts/`; the validator checks it on load.
- **New disease link** — add to a cohort's `diseases` array with a `weight` and a
  `dm_basis` quoting the Disease Master field that justifies it.
- **Updated Disease Master** — re-run `tools/build_disease_master.py`; the validator will
  name any link that no longer resolves.

Restart the server (or call `get_config(reload=True)`) to pick up config changes.

---

## Provenance and traceability

The Disease Master is the single source of truth for disease-level content: definitions,
markers, high-risk indicators, confirmatory tests, prevention guidance and next steps are
used verbatim and never paraphrased. Its `Review Status` and `Source / Reference` fields
are preserved and shown inside each condition's record, so the provenance the workbook
records travels with the output.

The correlation layer added on top is held to its own standard: every cluster names the
guideline or study that establishes its pattern, every disease link quotes the Disease
Master sentence it was matched on, and both are enforced as hard configuration errors
rather than conventions. Any finding in the dashboard can be traced backwards in three
clicks — from the condition, to the clusters that produced it with their weights and
confidences, to the individual parameter values that fired, and out to the citation.

When the Disease Master is updated, re-run `tools/build_disease_master.py` followed by
`tools/validate.py`; suite 1 names any link that no longer resolves and suite 5 names any
mapping that has stopped firing.
