# LaForge: Plan

**Owner:** Eyal Amir · **Status:** Draft v0.1 · **Date:** 2026-10-13
**One line:** LaForge is a new product, built on the ideas of Forge and Grinder, delivered as an agent inside the customer's own LLM. It finds feature combinations that predict the customer's target, with or without knowledge of their data and models, and hands back recipes that plug straight into the customer's insurance models.

---

## 1. Recommendation

1. **Ship LaForge as an MCP tool server plus a portable skill/instruction pack, deployable both Moove-hosted and locally inside the customer's environment.** Prototype on Claude via MCP, but design for the likely real targets: Microsoft Copilot or an unknown internal LLM running locally. None of the named design partners uses an LLM today. The LLM does the conversation and orchestration. All combination search, scoring, and ranking happens in deterministic tools, never in the LLM's head, and the tool surface stays simple enough for a weaker local model to drive.
2. **Build four modes on one engine**, each unlocked by what the customer shares:
   - **Blind**: nothing shared. Use Moove-side priors.
   - **Schema-aware**: field names and target definition only.
   - **Data-aware**: the customer's data, computed in their environment.
   - **Model-aware**: the customer's model used as a black box.
3. **Ship Blind mode first.** It reuses the existing Forge output, needs no customer data access, no security review, and no integration, and it is the fastest path to a live demo. Every later mode adds precision, not a new product.
4. **The deliverable is always a recipe**: a ranked list of combinations plus generated SQL/Python that computes them, a validation report, and a confidence score. The customer's data scientists plug the recipe into their own pipeline, as with Forge today.
5. **Make over-selection the central quality problem.** Forge scores 60,000+ pairs. Picking the "top 100–1000" from that is a multiple-comparisons and overfitting trap. LaForge's value is the validation layer on top of the search, not the search itself.

## 2. What Forge is today (from Drive)

Sources read: *FORGE Product* deck, *Correlation Impact on Machine-Learning*, *Overfitting*, *Nulls in Data Science*, *Confidence Score Guide*, *Customer Project Sheet*, *Archimedes Platform Plan v0.1*.

| Aspect | Current Forge (BigQuery) |
|---|---|
| Problem | Insurance DS teams don't know how to evaluate RoadIQ contextualized data or lift predictions with it. Feature engineering and feature combination is the hard part. |
| Mechanism | Step 1: per `here_segment_id`, for every pair (feat1, feat2) of ~200 features and op ∈ {×, ÷, 1}, compute `avg(f(feat1,feat2))` and `f(avg(feat1),avg(feat2))`. Step 2: correlate each result against the accident signal using ~10 measures (Pearson, Spearman, Kendall, mutual info, R², cosine, distance corr, CCC, ...). |
| Output | One table, ~60,000+ rows (pair × op), ~10 strength columns. Sample shows top rows such as `speed_kph_total ÷ historical_accidents_score` (Pearson ≈ 0.80). |
| Customer use | The DS team picks their own top 100–1000 by their own criteria and tests them in their own models. |
| Product concept | A **Forge** creates a per-customer, per-dataset **Grinder**, a recipe widget. Quanata was the first delivery (accident data, Massachusetts). |
| Fit in Archimedes | Forge is "customer-side feature composition" over approved Archimedes features. It is not the source of truth for signals. |

**Gaps LaForge must close** (from the decks themselves):

- **Collinearity:** the Correlation deck warns that stacking near-duplicate features biases the model and mismeasures lift. A raw top-N by correlation will return many near-duplicates (e.g. several `speed_total ÷ X` rows with nearly identical scores). LaForge needs diversity selection.
- **Overfitting:** the Overfitting deck is about validating against data used in training. Searching 60k+ combos against one target guarantees some spurious winners. LaForge needs held-out and out-of-geography/time validation and false-discovery control.
- **Nulls:** the Nulls deck says true nulls stay null and known-meaning nulls get replaced. Combination math must respect this, because `avg()` ignores nulls and a `÷` on a null or zero denominator is silent damage.
- **Confidence:** the Confidence Score Guide (`c_j = 1/(1+k)`, conformal calibration) already exists per feature. It should carry through to combos.
- **Step-1 inconsistency:** the deck says Step 2 yields "4 rows" for 200×200×2, which is a typo for 80,000. Row-count claims should be reconciled before they go in LaForge docs.
- **Row-level vs segment-level:** Forge correlates at segment-average level. A model that works at trip or policy level needs the aggregation made explicit, or the combo may not transfer.

## 3. Product definition

### 3.1 Who and what

- **User:** an insurer's data scientist or actuary, working in their company's LLM.
- **Job to be done:** "Tell me which combinations of Moove features (and my own) will lift my accident/loss model, and give me the code to compute them."
- **Design partners:** Quanata, HDVI, Milliman, Geico, State Farm. Quanata and HDVI are the nearest (Quanata was the first Forge delivery; HDVI is in production on data). None currently uses an LLM. We expect them to adopt Microsoft Copilot or an internal LLM run locally. That is an assumption to test with each partner in Phase 0.
- **Targets (what "predicts accidents" means):** both **frequency** (volume of accidents) and **severity**, built from DOT crash data and its proxies. Details in §3.4.
- **Out of scope for v1:** training or hosting the customer's model; replacing the customer's feature store; the agent inventing combinations or numbers it did not compute.

### 3.2 The four modes

| Mode | Customer shares | What LaForge does | Output |
|---|---|---|---|
| **A. Blind** | Nothing but a target type (e.g. "claim frequency, auto, US") | Serves precomputed Moove-side combination rankings against Moove's own accident signals (road-accident and driver-accident), with diversity selection and stability ranking. | Ranked, de-duplicated shortlist with SQL/Python recipes and confidence. |
| **B. Schema-aware** | Feature names/types, target definition, grain (policy, trip, segment), existing feature list | Maps their features to Moove features, drops combos that duplicate what they already have, adapts aggregation and join logic to their grain. | Same as A, tailored and de-duplicated against their existing features. |
| **C. Data-aware** | Access to their data in their own environment (warehouse or LLM code sandbox), joined to Moove data on location/time | Runs the Forge search against their real target in their environment. Moove features come in via tokenized join (Faraday). Raw data never leaves. | Combos ranked on *their* target with proper holdout validation. |
| **D. Model-aware** | A callable scoring function or exported predictions and residuals | Treats their model as a black box: finds combos that explain **residuals**, then measures **incremental** lift (A/B: with vs. without, per the Correlation deck's "correct experiment"). | Combos ranked by incremental lift on their model, with significance. |

Modes stack: a customer can start at A in a first conversation and move up as trust and integration grow.

### 3.3 Agent behavior (what lives in the LLM)

- **Guided intake:** asks only for what it needs, explains what each level of sharing unlocks, and records the mode.
- **Plan → run → verify → explain:** the agent calls tools, checks tool-reported validation, then narrates. It must cite the tool result for every number and must refuse to rank combos it has not computed.
- **Hand-off artifacts:** the agent returns a recipe file (SQL, pandas, and optionally a dbt/Snowflake/BigQuery UDF), a validation report, and a short rationale per combo ("why this plausibly relates to accidents").
- **Consistent with the Archimedes boundary:** Curie routes and narrates but does not originate calculations. LaForge is the same principle in the customer's LLM: it narrates and orchestrates, and computation is deterministic and logged.

### 3.4 Targets: frequency and severity from DOT data

LaForge scores every combination against **two targets, plus a combined view**, rather than one "ACCIDENT" label as in the current Forge sample.

| Target | Meaning | Notes |
|---|---|---|
| **Frequency** | Accident volume per road segment (and time bucket), normalized by exposure | Counts, so use rate-based measures (per exposure such as traffic volume or VMT), not raw counts. Exposure normalization matters: busy roads have more crashes. |
| **Severity** | How bad accidents are: share of injury/fatal crashes, or a severity-weighted score from DOT injury coding | Heavy-tailed and sparse. Needs different metrics than frequency (rank-based and tail-aware), and more data per segment. |
| **Combined risk** | Frequency × severity (an expected-loss style view) | Closest to what insurers price. Report it, but keep the two components visible. |

**Sources:** DOT crash records are the ground truth (state DOT crash databases such as MassDOT and ODOT, which already appear in the Drive decks, plus any national DOT files we can license or use). **Proxies** stand in where DOT coverage is thin: for example, hard-braking and near-miss events, speed-variance measures, and Moove's own driver-accident and road-accident signals. Proxy list and national coverage to be decided in Phase 0.

**Cautions:**
- DOT data is under-reported and severity coding differs across states, so tag every target with its source and coverage, and run state-by-state holdouts.
- A proxy can look excellent because it is built from the same inputs as the features. Keep proxy-derived targets separate from DOT-derived ones, and show which a combination was validated against.
- Severity and frequency can rank combinations differently. That is useful information for the customer and must not be averaged away.

## 4. Architecture

```
Customer's LLM (Claude / ChatGPT / Gemini / private)
        │  MCP (primary)  ·  OpenAPI tool spec (fallback)  ·  skill/instruction pack
        ▼
LaForge Gateway (Moove-hosted)   — auth, tenancy, rate limits, audit log, mode gating
   ├─ Catalog service      — Moove feature catalog: definitions, grain, null semantics, confidence c_j
   ├─ Combination Store    — precomputed pair/triple scores per target & geography (Blind/Schema modes)
   ├─ Selection engine     — diversity, stability, FDR control, holdout validation
   ├─ Recipe generator     — SQL (BQ/Snowflake/Databricks) + Python from a combo spec
   └─ Compute connectors
        ├─ Moove-side: BigQuery (current Forge)
        └─ Customer-side: runs *in their environment* (see below)
```

**Key design choices**

1. **Tool server over prompt-only.** Combination search over ~200 features is a compute problem. The LLM calls tools: `list_features`, `suggest_combinations`, `score_combination`, `validate_shortlist`, `generate_recipe`, `explain_combination`, and (Data/Model modes) `run_search_in_env`.
2. **Compute location follows data sensitivity.** Blind and Schema modes run on Moove's BigQuery. For Data and Model modes, ship the **search as code that runs in the customer's environment** (their warehouse via generated SQL, or an LLM code sandbox on a sample). Only metrics and the chosen recipe come back to the LLM. Raw rows stay put. Where the join needs identifiers, use Faraday tokenization or a clean room.
3. **Combination language, not just pairs.** Keep Forge's {×, ÷, 1} on pairs for v1 parity. Add (a) triples and ratios of aggregates, (b) simple transforms (log, rank, bucket), (c) interaction with context (e.g. speed ÷ speed-limit × wet-road flag). Cap the search with a budget. Do not exhaustively enumerate beyond pairs.
4. **LLM-agnostic, and local-first-capable.** The expected customer LLMs (Copilot, or an internal LLM run locally) are not under our control. MCP is the main channel, with an OpenAPI spec as fallback, and the skill/instruction pack kept small and tested per LLM. Specifically:
   - Package the engine so it can run **inside the customer's environment** (container or local MCP server), including an offline snapshot of the Combination Store, so no call has to leave their network. This is likely what Milliman, Geico and State Farm require before they use it at all.
   - Keep the tool surface **small and workflow-shaped** (few tools, structured inputs/outputs, the next step stated in each tool's response), because a local or smaller model will drive it less reliably than Claude.
   - **Do not depend on Claude-specific features.** Claude via MCP is the prototype environment only.
   - Treat MCP support in Copilot and in each partner's internal LLM as an **assumption to verify** in Phase 0, with a fallback: a CLI/notebook interface that produces the same recipe.
   - Treat each customer LLM as an integration to certify, not an assumption.
5. **Recipes are portable.** A recipe is a versioned spec (JSON) → generated SQL/Python. It must run without Moove runtime access, or the customer will see it as lock-in, and offline productionization is exactly what Forge/Grinder promised.

## 5. The search-and-validate engine (the real product)

1. **Candidate generation:** pair ops (Forge parity), then budgeted triples/transforms guided by step-1 survivors.
2. **Null and zero handling:** apply the Nulls policy (true null stays null; known-meaning null replaced). Safe division. Record coverage per combo (share of rows non-null).
3. **Scoring:** keep Forge's metric suite but add a **rank-aggregated score**, since a single raw Pearson of 0.8 on aggregate speed ÷ historical accidents may be a leakage artifact. Flag combos where one input is itself a near-proxy for the target (e.g. `historical_accidents_score`). Offer a leakage check.
4. **Redundancy control:** cluster combos by correlation with each other and with the customer's existing features (B/C/D). Select a diverse top-N (greedy mRMR-style or cluster representatives), per the Correlation deck.
5. **Honest validation (Overfitting deck):**
   - Split by **geography and time**, not random rows (segments are spatially autocorrelated).
   - Control false discovery across the 60k+ tests (e.g. Benjamini–Hochberg, or a permutation null on the target).
   - Stability: a combo must rank consistently across folds/states.
   - Final shortlist is evaluated on data never used for ranking.
6. **Confidence per combo:** propagate per-feature `c_j` (and coverage, n, stability) into a combo confidence so users can weight or filter.
7. **Incremental lift (Model mode):** residual correlation, then add-one-at-a-time and add-set evaluation against their model's metric (e.g. Gini, lift curve, loss ratio, deviance) with confidence intervals.

## 6. Phased roadmap

Timeline is relative to kickoff. It should be aligned with the Archimedes 12-week burst, since LaForge depends on the Movement Index/signal registry for approved features.

| Phase | Weeks | Deliverables | Exit criteria |
|---|---|---|---|
| **0. Foundations & decisions** | 1–2 | Define LaForge's boundary vs. Forge, Grinder, Archimedes (new product built on their ideas). Confirm Quanata and HDVI as first design partners; open conversations with Milliman, Geico, State Farm. Interview each on: which LLM they will use (Copilot / internal / none), whether it runs locally, and whether it supports MCP or tool calling. Choose the DOT sources and proxies for the frequency and severity targets (§3.4). Freeze v1 feature catalog scope (~200 features) with null semantics. Reconcile Forge row-count/typo issues. Audit the current BQ Forge code for reuse. | Signed decision record; two named partners; LLM-environment findings per partner; target definitions and data sources agreed; catalog v1 with owners. |
| **1. Blind-mode MVP** | 3–6 | Precomputed Combination Store for the frequency and severity targets (DOT-based, with proxies kept separate); MCP server with `list_features`, `suggest_combinations`, `generate_recipe`; diversity + stability selection; skill pack prototyped on Claude via MCP; packaged so it can also run locally. A **compatibility spike** against Copilot and one local open model to confirm the small tool surface works. | A DS can, in ~10 minutes, get a de-duplicated shortlist and runnable recipe with no data shared. Internal dry run beats the current manual Forge delivery. The same MCP server runs locally and is driven end to end by at least one non-Claude LLM. |
| **2. Validation layer** | 5–8 (overlaps) | Geography/time holdout, FDR control, leakage flags, combo confidence, validation report generator. | On a known case (Quanata/MA accident data), the validated shortlist outperforms naive top-N correlation on held-out data. |
| **3. Schema-aware mode** | 7–10 | Feature mapping, de-duplication against customer features, grain adaptation, richer recipe targets (BQ, Snowflake, Databricks, pandas). | Design partner reaches a recipe tailored to their schema with no data shared. |
| **4. Data-aware mode** | 9–14 | In-environment execution (generated SQL / sandbox code), Faraday tokenized join, security review pack, audit logging. | One partner runs a search on their own target; only aggregates leave their environment; reviewed by their security team. |
| **5. Model-aware mode** | 13–18 | Residual search, incremental lift harness, significance, A/B ("Moove vs. competitor data") experiment template. | One partner reports incremental lift on their production model metric. |
| **6. Multi-LLM & scale** | 15–20 | Certified integrations for 2–3 more LLMs; usage dashboards; pricing/packaging; docs. | Two customers on different LLMs onboarded without custom engineering. |

Parallel tracks throughout: security/privacy review, evaluation harness (see §7), customer success with the design partner.

## 7. Evaluation and success metrics

**Evaluation harness (build in Phase 2, use forever)**

- Historical cases with known outcomes (start with Quanata/MA). Replay: does LaForge's shortlist, built without peeking, improve the held-out target metric?
- Baselines: (a) raw Forge top-N by correlation, (b) their model with no Moove data, (c) Moove base features without combinations.
- Agent-level evals: tool-call correctness, no un-computed numbers, correct refusal when a mode is not unlocked, correct handling of nulls and leakage warnings, behavior differences across LLMs.

**Success metrics**

| Area | Metric | Target (initial, to be ratified) |
|---|---|---|
| Time to value | Time from first prompt to usable recipe (Blind) | < 15 minutes |
| Lift | Held-out lift of validated shortlist vs. raw top-N | Statistically significant, positive on the design-partner case |
| Trust | Share of recommended combos that survive customer holdout | Tracked per release; target set after the first partner |
| Adoption | Combos from LaForge in a customer's production model | ≥ 1 customer by end of Phase 5 |
| Efficiency | DS-hours per customer vs. manual Forge/Grinder delivery | Large reduction; measure the baseline now |
| Safety | Raw customer rows leaving their environment | Zero |

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Spurious winners from searching 60k+ combos | FDR control, geography/time holdouts, stability filter, held-out final test (§5). |
| Near-duplicate combos inflate the shortlist | Redundancy clustering and diversity selection; de-dup against customer features. |
| Leakage (a feature that is effectively the target, e.g. historical accident score) | Leakage flags; explicit "proxy-for-target" review in the catalog. |
| LLM hallucinates combos or numbers | Tool-only numerics; answers cite tool outputs; evals with adversarial prompts. |
| Customers won't share data or models | Blind and Schema modes carry standalone value; in-environment execution for Data mode. |
| Different LLMs behave differently with tools; Copilot or internal local LLMs may be weaker at tool use or lack MCP | Small workflow-shaped tool surface; per-LLM eval suite; keep the skill pack thin; CLI/notebook fallback that produces the same recipe; verify each partner's LLM setup in Phase 0. |
| Design partners have no LLM today, so adoption depends on a purchase they have not made | Blind mode and the CLI fallback deliver value without an LLM; validate demand with partners before building Phases 4–6. |
| DOT data gaps and inconsistent severity coding across states | Tag targets by source and coverage; state-by-state holdouts; keep DOT-based and proxy-based results separate (§3.4). |
| Scope blur with Forge/Grinder/Archimedes | Decide boundaries in Phase 0. LaForge = delivery and agent layer on Forge's engine, consuming approved Archimedes features. |
| Moove IP leakage via full ranking tables | Rate limits, tiered output (top-N per session), watermarking, and contract terms. Decide how much of the Combination Store is exposed. |
| Compute cost of per-customer searches | Precompute for Blind/Schema; budgeted search; cost guardrails per tenant. |
| BQ dependency: customers on Snowflake/Databricks | Recipe generator targets multiple SQL dialects. Moove-side compute stays on BQ. |

## 9. Decisions and open questions

**Decided (Eyal, 2026-10-13)**

1. **Product:** LaForge is a new product built on the ideas of Forge and Grinder.
2. **Design partners:** Quanata or HDVI first; Milliman, Geico and State Farm are also design partners. None uses an LLM today. We expect Copilot or an unknown internal LLM run locally.
3. **Targets:** both accident volume (frequency) and severity, using DOT data and its proxies (§3.4).
4. **First LLM environment:** Claude via MCP for the prototype.

**Still open**

1. **Relationship to existing Forge/Grinder:** does LaForge retire them, run alongside them, or reuse the Forge engine underneath? Affects the BQ audit and Quanata's existing delivery.
2. **First partner:** Quanata or HDVI? Quanata has Forge history and a known data scientist contact; HDVI is in production on Moove data. The choice sets the first evaluation case.
3. **DOT sources and proxies:** which state DOT files and any national sources do we already hold or license, and which proxies are acceptable? The Drive material shows Massachusetts and Ohio, and the project sheet lists a 50-state expansion as a next step.
4. **Customer LLM reality:** will the partners actually adopt Copilot or an internal LLM, and would they allow an MCP server (hosted or local)? If not, the CLI/notebook fallback becomes the primary channel.
5. **Commercials:** priced per recipe, per seat, per search, or bundled with data licensing? Affects how much of the Combination Store is exposed.
6. **Feature scope:** only combine existing catalog features in v1, or also propose new feature definitions? I recommend existing only.
7. **Archimedes dependency:** do LaForge features need to come from the Movement Index signal registry from day one, or can v1 use the current Forge feature set?

## 10. Immediate next steps (two weeks)

1. Review and mark up this plan; decide §9 open items 1, 2 and 4.
2. Audit the existing BigQuery Forge pipeline (tables, runtime, cost per run) for reuse. I did not inspect the BQ code for this draft.
3. Re-run Forge on the Quanata/MA case and save it as the evaluation baseline. I didn't read `quanata_accidents_MA_correlations.xlsx` (it is a shortcut) and did not see lift results from the April delivery.
4. Draft the feature catalog v1 with null semantics, grain, and leakage flags.
5. Sketch the MCP tool contract (§4), prototype on Claude via MCP, and ask Quanata/HDVI what LLM environment they expect to use.
6. Decide the DOT sources and proxies for the frequency and severity targets (§3.4).

---

*Source caveat: this plan is derived from the Drive decks listed in §2. The Drive material is dated mostly 2025, so the status of Forge and the Quanata delivery should be confirmed.*
