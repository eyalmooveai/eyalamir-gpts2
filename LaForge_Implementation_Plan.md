# LaForge: Implementation Plan

**Owner:** Eyal Amir · **Status:** Draft v0.1 · **Date:** 2026-10-14
**Companion to:** `LaForge_Plan.md` (product plan) and the deck `LaForge-design.pptx`. This document says *how to build it*: architecture, data model, interfaces, milestones, tests, and the first spikes.

**Status of verification:** a first read-only audit of `moove-archimedes-staging` and `moove-platform-testing-data` is recorded in §0. It is partial: I could not find the code that computes the correlations, so Spike S1 (§9) is only partly done. Team size and names are placeholders. Effort is given as S/M/L, not days, until the spikes report.

---

## 0. What the existing Forge looks like in BigQuery (audit, 2026-10-14)

Read-only queries on table metadata, schemas and a few result rows. No data was changed.

**Found**
- **Quanata/MA outputs** in `moove-archimedes-staging`: `bucket_in.quanata_accidents_MA_correlations` (9,891 rows), the inputs `bucket_in.result2_div_correlations_input` (4,841 rows) and `result2_mul_correlations_input` (5,050 rows), univariate baselines (101 rows), and ranked outputs in `calc_out` (`..._top10`, `..._top10_2021over`, `..._top10_2021over_deduped`, `..._deduped_max`). The same tables also exist in `moove-platform-testing-data.calc_tmp`.
- **Raw Quanata point data** in `moove-platform-testing-data.bucket_in` (`quanata_1_percent_sample_2`: about 2.8 TB and 2.56 billion rows), plus a filtered copy (about 169 million rows) in `moove-archimedes-staging.calc_in`.
- **An existing accident risk model**: `inferred.accidents_risk_model1_US_FARS` and `..._US_WAZE` (about 112 and 109 million rows), and state tables such as `calc_tmp.accidents_model_CA_*`, `_FL_*`, `_TX_*`, `_PA_*`, `_NY_*`.

**Findings that change the plan**
1. **The delivered Forge was much smaller than the deck says.** The deck describes ~200 features and 60,000+ combinations. The Quanata/MA run used about **100 features** and about **5,558 analysis units**, giving **9,891 combinations**. Multiply has 5,050 rows, which equals 100 × 101 / 2 (so squares are included). Divide has 4,841, so some pairs were dropped, probably null or infinite results. The 60,000+ figure is a design maximum, not a delivered result.
2. **The target is a raw integer count.** `target_array` is `ARRAY<INT64>`; a sample shows a maximum of 159. There is no visible exposure normalization, so busy roads score as riskier. This confirms the need for exposure-adjusted targets.
3. **Pair inputs are stored as arrays** (`result_array`, `target_array`, one pair per row), and the correlations are computed from those. I could not find a BigQuery routine or recent query job that does this, so it likely runs outside BigQuery SQL (a script or notebook). **The code that computes the metrics has not been located.** Ask the team where it lives.
4. **Not all listed metrics are populated.** `bicor` and `hoeffding_d` are STRING columns in the output while the others are FLOAT64, which suggests they were not computed. The measures that are populated: Pearson, Spearman, Kendall, mutual information, R², cosine similarity, distance correlation, and CCC.
5. **A de-duplication step already exists** (`..._deduped_max`, 51 rows; `..._top10_2021over_deduped`, 56 rows), and results are ranked per metric. Its method is not documented.
6. **Pairs did beat singles, in sample.** The best pair reaches Spearman 0.42, Pearson 0.41 and Kendall 0.35, against the best single feature at 0.34, 0.24 and 0.25. Caveats: these are in-sample, from tables that may use different filters, and the pair table is the top results per metric, so selection bias applies. This is the number the validated engine has to reproduce honestly on held-out data.
7. **FARS-related tables are risk-model outputs, not raw FARS.** `accidents_risk_model1_US_FARS` holds `accidents_risk_score`, `accidents_per_vehicle_mile`, `confidence` and top-3 risk explanations by `local_segid` and date. It already includes an exposure-normalized rate and a confidence column, which is useful for the severity and frequency work. **The raw FARS crash records have not been located.** Ask where they are loaded.
8. **The query-history table showed only my own queries**, so I could not recover how the Quanata tables were built.

**Effect on the plan:** the engine's first scale target is about 100 features and ~5,500 units, which BigQuery handles easily. The 200-feature case is a later scale test, not the starting point. S1 shrinks to "find the correlation code and the raw FARS data, then document the existing de-duplication."

---

## 1. What gets built

Five deliverables, in dependency order:

1. **Feature catalog**: the governed list of ~200 features with grain, null semantics, leakage flags, and per-feature confidence.
2. **Targets**: frequency, severity (FARS), and combined risk, at a chosen analysis unit, with exposure.
3. **Engine**: candidate generation → cheap screen → validation → selection, producing the **Combination Store**.
4. **Recipe layer**: a combination spec (JSON) that renders to SQL and Python and reproduces the numbers exactly.
5. **Delivery**: an MCP server, an instruction pack, a local/offline bundle, and a CLI fallback.

Build order follows the deck: Blind mode first (Phases 0–2), then Schema-aware, Data-aware, Model-aware.

## 2. Repository layout

```
laforge/
  catalog/      features.yaml, schema, validators, null-semantics rules
  targets/      FARS loader, non-fatal source loader (TBD), exposure, target builders
  engine/
    candidates/   pair generation (×, ÷, identity), budgeted extensions later
    screen/       cheap scoring over all candidates (BigQuery SQL)
    validate/     holdouts, permutation null, FDR, stability, leakage (Python)
    select/       clustering, diversity selection, confidence
  store/        table DDL, loaders, versioning
  recipe/       spec JSON schema, renderers (bigquery, snowflake, databricks, pandas)
  server/       MCP server and tool implementations
  skills/       instruction pack (versioned, tested per LLM)
  cli/          command-line fallback with the same tool functions
  evals/        golden cases, planted-signal tests, agent evals, compatibility matrix
  deploy/       Dockerfile, offline bundle builder, hosted infrastructure
  docs/decisions/   short decision records
```

Languages: Python for server, validation and CLI; SQL (BigQuery) for the heavy screen. Use the official MCP SDK. Keep each tool a plain function that the CLI and the MCP server both call, so the fallback never drifts.

## 3. Data model

**Analysis unit is a parameter, not a constant.** FARS fatal crashes are very rare per road segment, so the unit (segment, corridor, road-class × area, county or zip) and the pooling window (1–5 years) are chosen by Spike S2 and stored with every run.

| Table | Key columns |
|---|---|
| `catalog.features` | `feature_id`, `name`, `grain`, `null_policy` (`true_null` / `replace_with:X`), `leakage_flag`, `c_j` (confidence), `owner`, `catalog_version` |
| `targets.unit_target` | `unit_id`, `window`, `source` (`FARS` / `state_dot` / `proxy:<name>`), `n_events`, `exposure`, `freq_rate`, `severity_score`, `combined_risk`, `coverage_flag`, `state`, `year` |
| `store.runs` | `run_id`, `engine_version`, `catalog_version`, `target_id`, `unit`, `window`, `config_hash`, `started_at`, `cost_bytes` |
| `store.combinations` | `run_id`, `combo_id`, `feat_a`, `feat_b`, `op`, `agg_mode` (avg-of-op / op-of-avg), screen metrics (~10 Forge measures plus the new ones in §4), `coverage`, `stability`, `perm_p`, `fdr_q`, `leakage_flag`, `cluster_id`, `diversity_rank`, `confidence`, `target_source` |
| `store.recipes` | `recipe_id`, `spec_json`, `rendered_sql`, `rendered_py`, `validation_report`, `created_for` (tenant, mode) |

Every row is stamped with `engine_version` and `catalog_version` so any result can be reproduced. Dataset layout follows the Archimedes pattern: dev, staging and production projects, relative dataset references, staging-to-production promotion through CI, no manual production edits.

## 4. Engine design

A funnel, because validating all ~80,000 candidates (200 × 200 × 2) is wasteful.

**Stage A: candidates.** All ordered pairs for ×, ÷ and identity, in both aggregation modes Forge uses (`avg(f(a,b))` and `f(avg(a),avg(b))`). Apply the catalog's null policy first. Safe division: a zero or null denominator yields null, never 0 or infinity. Record `coverage` (share of units with a valid value).

**Stage B: screen (BigQuery).** Score every candidate cheaply. Keep the measures Forge actually populates (Pearson, Spearman, Kendall, mutual information, R², cosine similarity, distance correlation, CCC; drop `bicor` and `hoeffding_d` unless they are made to work) and add measures that suit counts and rare events. Also report each combination's **gain over its better parent feature**, since the audit shows single features already reach Spearman ≈ 0.34 on Quanata/MA:
- Frequency: Poisson or negative-binomial log-likelihood gain with an exposure offset; rank correlation of rate.
- Severity (FARS): AUC and average precision for "any fatal crash in the unit/window", plus rate correlation.
- Avoid materializing the 400M-row intermediate Forge describes where `CORR`-style aggregates can be computed directly; measure cost in S1.
- Output: top K (start K = 5,000) per target by a rank-aggregated score, plus everything with a leakage flag so it can be reported.

**Stage C: validation (Python, on the K survivors).**
1. **Splits:** leave-one-state-out and year folds. Never random rows.
2. **Permutation null:** shuffle the target in blocks (within state and road class, or by corridor) to preserve spatial structure; empirical p-value per combination.
3. **FDR:** Benjamini–Hochberg on those p-values across all tested candidates, not just survivors.
4. **Stability:** sign consistency and rank stability across folds; store the worst-fold score.
5. **Leakage:** flag a combo if either input is catalog-flagged, or if a single input alone explains most of the combo's score (single-feature ablation).

**Stage D: selection.**
1. Cluster survivors by absolute correlation of their values (on a sample) and by shared parent features.
2. Pick a diverse shortlist (greedy relevance-minus-redundancy), capped per parent feature.
3. **Combo confidence** = a function of the two features' `c_j`, coverage, stability, and `fdr_q`. Start with `min(c_a, c_b) × coverage × stability_score`, and calibrate it later with the coverage audit already described in the Confidence Score Guide. Mark it "uncalibrated" until then.
4. Final untouched test: a never-used year/state set, evaluated once per release and recorded.

**Frequency vs. severity** are ranked separately and never averaged. Combined risk is reported from the two component scores.

## 5. Recipe layer

A recipe spec is JSON: target, unit, window, list of combinations (`feat_a`, `feat_b`, `op`, `agg_mode`, null policy), catalog and engine versions, and the validation summary.

- Renderers: BigQuery first, then Snowflake, Databricks, and pandas.
- **Fidelity test (required):** for every rendered dialect, re-compute each combination on a sample and match the store's values within tolerance. A recipe that does not reproduce its own numbers fails CI.
- The recipe must run with no Moove access: no calls back, no Moove-only functions.

## 6. MCP server and instruction pack

**Tools** (small, workflow-shaped, structured I/O; each response includes `next_step`):

| Tool | Input | Output |
|---|---|---|
| `list_features` | filter (domain, grain) | features with grain, null policy, confidence |
| `start_session` | target type, mode, unit preference | session id, unlocked tools, what each extra share would unlock |
| `suggest_combinations` | target (frequency / severity), top N, optional exclusions | diverse shortlist with scores, `fdr_q`, stability, coverage, confidence, target source |
| `score_combination` | one combo spec | its stored or freshly computed metrics |
| `validate_shortlist` | list of combos | per-fold results, leakage flags, final-test result |
| `generate_recipe` | list of combos, dialect | recipe spec, rendered code, validation report |
| `explain_combination` | combo id | plain-language rationale built from catalog definitions (no new numbers) |
| Later modes | `map_schema`, `run_search_in_env`, `score_with_model`, `incremental_lift` | see Milestones M4–M6 |

**Rules enforced in code, not only in the prompt:**
- Mode gating: tools for modes the customer has not unlocked return a structured refusal that says what to share.
- Each response carries a `provenance` block (run id, versions); the instruction pack tells the LLM to quote numbers only from tool output.
- The server refuses to return the full combination table: top-N per session, rate-limited. How much of the store is exposed is a commercial decision (open).

**Instruction pack:** short, versioned. A step order (intake → suggest → validate → recipe → explain), the no-invented-numbers rule, refusal behavior, and output format. Test it per LLM.

## 7. Delivery and deployment

- **Hosted:** server and store on the Moove side for Blind and Schema modes.
- **Local bundle:** a container with the MCP server and a read-only, versioned snapshot of the Combination Store, which runs inside the customer's network with no outbound calls. Sign the bundle and record its versions. Decide expiry and licensing of the snapshot (open).
- **CLI fallback:** the same functions as the tools, with notebook examples. This is the primary channel if a partner's LLM cannot use MCP.
- **Data-aware mode:** ship the search as code that runs in the customer's warehouse or sandbox. Only metrics and the chosen recipe leave. Use Faraday (tokenization / clean room) where identifiers must be joined.

## 8. Milestones and work breakdown

Weeks are relative to kickoff and match the deck. Each milestone lists its acceptance test.

### M0: Foundations (weeks 1–2)
| Work | Size | Depends on |
|---|---|---|
| Spikes S1–S3 (§9) | M | none |
| Feature catalog v1 (~200 features, null semantics, leakage flags) | M | S1 |
| Decision records: relationship to Forge/Grinder, first partner, analysis unit, source for non-fatal crashes | S | S1, S2 |
| Repo skeleton, CI, dev/staging/prod projects | M | none |
| Partner interviews: LLM environment, MCP support, local-run requirement | S | none |

**Accept:** catalog v1 signed off; target definitions agreed; spike results written up; CI green on an empty pipeline.

### M1: Targets and engine core (weeks 2–5)
| Work | Size |
|---|---|
| FARS loader; geocode to units; exposure join; pooled windows | L |
| Non-fatal frequency source loader (once chosen) | M |
| Stage A and B on BigQuery with cost guardrails | L |
| Planted-signal test data (synthetic) | M |

**Accept:** a screen run completes for each target with a recorded cost; on synthetic data with planted true combinations, the screen recovers them.

### M2: Validation and selection (weeks 4–8)
| Work | Size |
|---|---|
| Fold generation; block permutation null; BH FDR | L |
| Stability, leakage, diversity selection, combo confidence | L |
| Quanata/MA replay harness and baseline (raw top-N vs. validated shortlist) | M |

**Accept:** (a) on permuted targets, the false-discovery rate at q = 0.05 is at or below about 5%; (b) the validated shortlist beats raw top-N on held-out data in the replay; (c) the best validated pair beats the best single feature on held-out data (in sample today: Spearman 0.42 vs 0.34). If (b) or (c) fails, stop and fix the engine before building more.

### M3: Blind-mode MVP: MCP, recipes, local bundle (weeks 3–6, overlaps)
| Work | Size |
|---|---|
| Combination Store loaders and versioning | M |
| MCP server with the Blind-mode tools, mode gating, provenance | L |
| Recipe renderers (BigQuery, pandas first) and fidelity tests | L |
| Instruction pack v1; agent evals on Claude | M |
| Local bundle and CLI fallback | M |
| Compatibility spike on Copilot and one local open model | M |

**Accept:** a data scientist reaches a usable recipe in under 15 minutes with no data shared; recipes reproduce their stored numbers; the local bundle runs with no outbound network; at least one non-Claude LLM drives the full flow.

### M4: Schema-aware mode (weeks 7–10)
Feature mapping (customer fields to catalog features), de-duplication against their existing features, grain adaptation, Snowflake and Databricks renderers.
**Accept:** a design partner gets a tailored recipe from field names and a target definition alone.

### M5: Data-aware mode (weeks 9–14)
In-environment search code (generated SQL / sandbox), tokenized join through Faraday, audit logging, security-review pack, customer-side holdout design.
**Accept:** a partner runs a search on their own target; only aggregates leave; their security team signs off.

### M6: Model-aware mode (weeks 13–18)
Residual search and incremental-lift harness for a black-box model: score function or exported predictions, add-one and add-set evaluation, confidence intervals, the "with vs. without Moove data" experiment template from the Correlation deck.
**Accept:** one partner reports incremental lift on their own metric with an interval.

### M7: Multi-LLM and scale (weeks 15–20)
Certified integrations for further LLMs, usage and cost dashboards, packaging and pricing hooks, documentation.
**Accept:** two customers on different LLMs onboarded without custom engineering.

## 9. First spikes (time-boxed, week 1–2)

| Spike | Question | Output |
|---|---|---|
| **S1: Forge audit** (partly done, §0) | Remaining: where is the code that computes the correlation metrics, and what does it cost per run? Where are the raw FARS crash records? How does the existing de-duplication work? What can be reused as Stage A/B? The 400M-row intermediate in the deck is not visible in staging (the Quanata run was ~5,500 units). | Reuse/rewrite decision per component; cost baseline; location of FARS data |
| **S2: Unit and sparsity** | At which unit and pooling window do FARS fatal counts give enough positives for stable scoring? How do frequency and severity rankings differ? | Chosen unit/window; minimum-positives rule |
| **S3: LLM compatibility** | Does Copilot (and each partner's internal LLM) support MCP or tool calling? Can a small local model drive a 6-tool workflow? | Support matrix; go/no-go for the CLI as primary channel |
| **S4: Validation cost** | Can block-permutation FDR run over ~80,000 candidates at acceptable cost (or on the screen survivors with an approximate correction)? | Chosen method and budget |
| **S5: Recipe fidelity** | Does generated SQL reproduce the engine's aggregates exactly, including nulls and zero denominators? | Fidelity test design |

## 10. Quality and testing

- **Unit tests:** null and zero handling; safe division; coverage; confidence formula; mode gating.
- **Planted-signal tests:** synthetic data with known true combinations, near-duplicates, and chance patterns. The engine must keep the true ones, collapse the duplicates, and reject the chance ones.
- **Statistical calibration:** permuted-target runs must show the nominal false-discovery rate.
- **Golden replay:** the Quanata/MA case, run without peeking, compared against raw Forge top-N.
- **SQL tests:** BigQuery dry-run for cost, schema checks, row-count and freshness assertions.
- **Agent evals:** correct tool selection; every number in the answer matches a tool output (automated check); correct refusals for locked modes; behavior across the compatibility matrix.
- **CI:** lint, unit tests, SQL dry-runs, planted-signal and fidelity tests on every change; golden replay and agent evals before each release. Promotion from staging to production is gated on all of them.

## 11. Security and privacy

- Tenant isolation, per-tenant audit logs, authentication on the hosted server.
- Blind and Schema modes never receive customer data. Data-aware mode never moves raw rows; only metrics and the recipe return.
- Local bundle: signed, versioned, no outbound calls, with licensing terms for the snapshot.
- Rate limits and top-N caps to protect Moove's combination rankings.
- Security review before M5 and before any pilot that touches customer data.

## 12. Staffing assumptions (placeholders)

Roles, not people: a data-science lead (engine and validation), a data engineer (targets, BigQuery, store), a backend engineer (MCP server, recipes, deployment), part-time product owner (Eyal), and a security reviewer at M5. A second data engineer would shorten M1. Confirm capacity in week 1, and where it conflicts with the Archimedes burst, record the trade-off.

## 13. Decisions needed, and by when

| Decision | Needed by |
|---|---|
| Does LaForge retire, run alongside, or reuse the Forge engine | End of S1 (week 1–2) |
| First partner: Quanata or HDVI | Week 2 |
| Non-fatal crash data source for frequency | Week 2 (blocks M1 frequency work) |
| Analysis unit and pooling window | End of S2 |
| Proxy list and how proxies are labeled | Week 3 |
| Exposure source (traffic volumes) | Week 3 |
| How much of the Combination Store the server may expose; licensing of the offline snapshot | Before M3 acceptance |
| Pricing model | Before the first partner pilot |
| Whether v1 features must come from the Archimedes registry | Week 2 |

## 14. Main implementation risks

| Risk | Early signal | Response |
|---|---|---|
| FARS too sparse at segment level | S2 shows few positives per unit | Coarser unit, pooled years, rare-event metrics |
| Validation too costly | S4 budget exceeded | Validate screen survivors only; approximate correction |
| Weak or missing tool support in partner LLMs | S3 matrix | CLI as primary; thinner tool surface |
| Recipes don't reproduce | Fidelity tests fail | Fix renderers before adding dialects |
| Engine doesn't beat raw top-N | M2 replay | Stop and rework before building delivery on it |
| Capacity conflict with Archimedes | Owners not named in week 1 | Record the trade-off; shrink M4–M7 scope first |
