# Plan — Ghost Data Still Exists (HackerRank Orchestrate, Oct 2026)

## 0. Guiding principles (non-negotiable)
1. **Agentic system.** The solution is a set of LLM agents (Claude) that investigate each case using tools. Deterministic code provides the *tools* and the *output-contract guardrails*; the agents do the reasoning: intent, linking evidence, interpreting state and policy, and planning.
2. **No hardcoding.**
   - No case IDs, user IDs, resource IDs, emails, evidence IDs, or golden labels in code or prompts.
   - No hand-written category tables or system lists. Categories, conditions and actions come from `deletion_rules.csv`. The resource → category and system mappings are **learned from the evidence** (e.g. `record_type` next to `resource_id`), or decided by the agent with its reasoning recorded.
   - Paths are resolved relative to the repo root. Model names, limits, prices and thresholds live in `code/config.yaml` / env vars.
   - The only fixed values are the **output contract** (columns, phases, the 15 operation/state pairs, ordering), kept in one `contract.py` that is cited back to problem_statement §6.
   - No case-specific prompt examples drawn from the evaluation set. Few-shot examples, if used, come only from `sample_*` files, and they must be excluded when scoring those same samples.
3. **Evidence over claims.** `reported_status` / `reported_result` are never proof. Only facts with time ≤ `audit_at` are used.
4. **Determinism and reproducibility.** Fixed prompts, temperature 0 where supported, an on-disk cache keyed by (model, prompt hash), stable sorting and JSON serialisation.
5. **Generalise, don't overfit.** Every rule must trace to the spec or a pattern seen in several samples, and is checked on a held-out split.

## 1. The problem
For each of 518 requests (`dataset/deletion_requests.csv`), decide whether deletion was requested and, if so, whether it is truly complete **as of `audit_at`**. Output `case_id,deletion_status,steps_json`:
- `deletion_status` ∈ COMPLETE / INCOMPLETE / NOT_APPLICABLE.
- `steps_json` = FINDING → REMEDIATION → VERIFICATION, using the 15 allowed operation/state pairs.

Scored on status, identity resolution, discovered records and risks, cited evidence, policy treatment, step completeness and order.

## 2. Dataset facts (profiled)
- **Requests:** 518 (`case_33`…). Reported statuses: 254 COMPLETE / 152 PARTIALLY_COMPLETE / 112 NOT_STARTED. ~27 templated texts in EN/ES/PT/FR/JA/KO, plus ~10 one-off non-deletion texts (invoice copy, change email/phone, marketing-only opt-out, access/portability, login reset).
- **Samples:** 32 (`case_01`–`case_32`): 22 INCOMPLETE, 10 COMPLETE, **0 NOT_APPLICABLE**.
- **Identities:** 850 users. **Audit log:** 306 changes (email / email_hash / phone).
- **Activity:** 4,011 events across 17 types. The `details` text carries facts (e.g. "flow remains enabled", "archive can still be restored", "catalog linked X to email and user", "restore repopulated Y"). Contains decoys.
- **Rules:** 15 categories, actions DELETE / REDACT / ANONYMIZE / RETAIN, several of them **conditional** (shared support context, sole-owner attachment, retention period, active legal hold, tombstone).
- **Evidence:** 94 files (24 SQL replica snapshots, 28 JSON, 26 logs, 1 TXT, 4 PDF, 11 PNG), each with a `captured_at`.
- **Citations:** samples cite **only `evidence_XX` file IDs**, never activity/audit IDs. `evidence_92` covers 36 of 75 citations. No sample cites anything after its `audit_at`.
- **Categories:** only 9 of 15 appear by name in the text evidence (CACHE_ENTRY, MARKETING_PROFILE, PRODUCT_TELEMETRY, EMAIL_DELIVERY_LOG, ACCOUNT_TOMBSTONE, SECONDARY_IDENTIFIER never do), so these must be inferred.
- **Matching:** emails appear in mixed case; near-duplicate names exist as decoys.
- **Sample verifications:** INCOMPLETE cases verify **every** relevant resource (empty evidence). COMPLETE cases verify with cited evidence.

## 3. Known risks → mitigations (from the plan review)
| # | Risk | Mitigation |
|---|---|---|
| 1 | Overfitting to 32 samples; no NOT_APPLICABLE samples | Rules must trace to the spec. Held-out split of the samples. ≥3 custom cases (tombstone, restore risk, sync recreation, historical email, non-deletion). |
| 2 | Data category / rule condition is not explicit | Category inference tool learns the prefix/system → `record_type` mapping from the evidence. The Policy agent decides conditions (retention period, legal hold, shared context, sole owner) from evidence and records its reasons. |
| 3 | Decoys / wrong-person linking | Link records only through grounded identifiers (exact email ignoring case, hash, phone, user_id, explicit LINK activity). Never match on name alone. Every link carries a provenance reason. |
| 4 | Which evidence to cite is ambiguous | Phase A forensics derives the choice rule from the samples (latest ≤ audit_at vs. most specific source). The scorer tracks evidence accuracy separately. |
| 5 | Replica drift / data recreated after deletion | State is tracked per **copy** (resource × replica/source) on a timeline. Sync/restore/enabled-flow events are applied after deletes. |
| 6 | Hidden step-generation conventions | Phase A turns sample patterns into explicit documented rules: which resources are verified, finding+remediation pairs, tombstone handling, ordering. |
| 7 | Vision misreads | Strict JSON schema; IDs checked against the dataset; pdfplumber text first; one manual review of all 15 cached extractions. |
| 8 | "AI agent" vs. accuracy | Agents reason over tools. Deterministic validators enforce the contract. The trade-off is documented in the evaluation report. |
| 9 | Intent errors | The Intake agent classifies **all** 518 texts; a disagreement with a lexical baseline triggers a second review. |
| 10 | Cost / tokens | Precompiled case dossier (a few calls per case, not a long tool loop). Prompt caching on the shared system prompt and tools. Disk cache. Usage recorded per call so cached reruns still report the original run's usage. |
| 11 | Windows encoding / paths | UTF-8 everywhere (`sys.stdout.reconfigure`), LF CSV output, pathlib paths relative to the repo root. |
| 12 | Model IDs / pricing | Verified via the API docs before use; kept in config, not code. |

## 4. Architecture — multi-agent with tools
```
                     ┌───────────────────────── Orchestrator (code) ─────────────────────────┐
deletion_requests ──►│ for each case:                                                        │
                     │  1. Intake Agent ── deletion? (multilingual) ──► NOT_APPLICABLE / go  │
                     │  2. Dossier Builder (tools) ── identities + linked facts ≤ audit_at    │
                     │  3. Investigator Agent ── per-copy state, flows, restore risk          │
                     │  4. Policy & Planner Agent ── rules → required state → status + steps │
                     │  5. Reviewer Agent ── checks against spec + evidence, sends fixes back │
                     │  6. Contract Validator (code) ── enums, order, numbering, evidence IDs │
                     └────────────────────────────────────────────────────────────────────────┘
One-time (cached): Evidence Extraction Agent (vision) for PNG/PDF → structured facts
```

**Tools (deterministic Python, exposed to the agents as Claude tool calls):**
- `resolve_identity(email, as_of)`: user_id plus all current/historical emails, phones, hashes, names, with provenance.
- `search_facts(identifiers|resource_id, as_of)`: unified fact store across SQL/JSON/log/TXT/vision facts and activity rows.
- `query_snapshot(file_id, sql)`: read-only SQLite over a snapshot.
- `get_activity(resource_id|identifier, as_of)` / `get_rules()` / `infer_category(resource_id, system_id)`.
- `get_evidence_meta(file_id)`: path, origin, captured_at.

**Agents (Claude, prompts in `code/prompts/`):**
- **Intake:** intent classification with a rationale (cheap model).
- **Evidence Extraction:** images/PDFs → schema-validated facts (vision model), run once and cached.
- **Investigator:** builds the subject's resource inventory and the per-copy state at audit_at, with an evidence ID for each claim.
- **Policy & Planner:** applies the rules and conditions, decides the status, emits structured steps (function-call output, never free-form JSON).
- **Reviewer:** adversarial check (decoys, late evidence, missing copies, tombstone, retained records still present). Gets at most one revision round.

**Code layout**
```
code/
  main.py              CLI: python code/main.py [--input ...] [--output output.csv] [--samples] [--limit N]
  config.yaml          models, prices, limits, paths (relative)
  contract.py          output contract enums/order (from spec §6)
  llm.py               Claude client: tool loop, cache, retries, usage ledger
  data/                loaders, identity, fact store, parsers (sql/json/log/text), category inference
  tools/               tool schemas + handlers
  agents/              intake, extraction, investigator, planner, reviewer
  prompts/             system prompts (no eval-case content)
  orchestrator.py      per-case pipeline, concurrency, deterministic merge
  validate.py          output validator
  evaluation/
    score.py           status / step / field / sequence / evidence accuracy, held-out split
    custom_cases/      ≥3 participant-built cases
    evaluation_report.md, usage_report.md
```

## 5. Execution order
- **Phase 0 ✅ Setup:** clone, `.venv`, requirements, `.gitignore`, `.env.example`, `log.txt`.
- **Phase A — Sample forensics:** tabulate all 32 samples. For every step, record why each resource is included, which evidence is cited, the category, condition, ordering and tombstone handling. Write the derived conventions into this Plan (§6).
- **Phase B ✅ Data layer + tools:** implemented in `code/ghost/` (package renamed from `code` to avoid a clash with the stdlib `code` module).
  - `data/`: loader, normalize, identity, parsers (SQL/JSON/log/text/PDF, format-generic), facts (store, indexes, learned prefix→system/type, snapshot scope, absence, replica drift), linker (grounded links, ownership, workflow-job candidates, global assignment).
  - `tools/`: dossier and registry (6 read-only Claude tools). Dev utility: `code/inspect_case.py`.
  - Results: 2,891 facts from 82 files (11 PNGs wait for Phase D). All 550 subjects resolved. Dossier median ~13k chars, all 550 built in 14 s.
  - Linking on samples: recall 0.88 / precision 0.92. Remaining misses: concurrent workflow jobs (account swaps, anonymised analytics ties, flagged AMBIGUOUS for the agent) and unknowable resources.
  - Visibility rule: records observed ≤ audit_at are usable for reasoning; only files captured ≤ audit_at are citable.
- **Phase C ✅ Scorer:** `code/evaluation/score.py --pred <csv> [--split all|dev|holdout] [--details] [--json out]`.
  - Metrics: status accuracy; step P/R/F1 (with and without evidence_id); resource P/R/F1; position-aligned field accuracy; sequence accuracy; evidence accuracy; contract-error rows; missing rows. Metric definitions are documented in the file (the official grader formula is not published).
  - Validated: gold vs gold = 1.0 on every metric; a perturbed file (status flip, dropped step, wrong evidence, reorder, invalid op, missing row) is detected by the expected metrics.
- **Phase D ✅ Agents:** Intake → Extraction (cache and review the 15 files) → Investigator → Planner → Reviewer. Iterate on the scorer using the dev split; check the held-out split.
  - Built: `ghost/llm.py` (disk cache, usage ledger, tool loop with JSON-schema final output, refusal fallback on Sonnet/Opus), `ghost/agents/{schemas,core}.py`, `code/prompts/*.md`, `ghost/steps.py` (contract guardrail; reproduces all 32 gold step lists exactly), `ghost/orchestrator.py`, `code/main.py`.
  - Models: start every agent on Haiku 5.5, then measure Sonnet 5.5 on the samples and keep the cheapest model that holds quality (user instruction: avoid Opus unless needed). Thinking is always on (no temperature control), so reproducibility comes from the disk cache.
  - PDFs use the pdfplumber text layer (no vision call). The 11 PNGs go through the vision extraction agent once.
  - Key fixed. Sample iterations (32 cases, all splits):
    | run | models | status acc | step F1 | seq acc | cost |
    |---|---|---|---|---|---|
    | haiku_s1 | all Haiku | 0.72 | 0.76 | 0.38 | $0.26 |
    | haiku_s3 | all Haiku + fixes | 0.97 | 0.90 | 0.47 | $0.23 |
    | haiku_s5 | all Haiku + retries | 0.97 | 0.88 | 0.50 | $0.23 |
    | sonnet_ip_s1 | Sonnet investigator + planner | 1.00 | 0.91 | 0.50 | $1.33 |
    | mixed_s2 (final) | Sonnet investigator + planner, Haiku intake/review/extraction | 1.00 | 0.90 (dev 0.92 / holdout 0.87) | 0.47 | ~$1.3 |
  - Fixes from failure analysis:
    - Intake: deletion-verification follow-ups are deletion requests; the second opinion now uses a distinct prompt.
    - Snapshot coverage is inferred from the rows' systems.
    - Unknown or invented resource ids are dropped.
    - Image-extracted ids count only when their prefix is known from structured data.
    - Findings must cite evidence that observes the resource; remediation-type activity is a claim.
    - INCOMPLETE without findings becomes COMPLETE.
    - Workflow-job profile assignment is accepted; AMBIGUOUS follow-ups are excluded (including them added more FPs than TPs).
    - Case-level retries cover network errors.
  - Cost optimization (user request): the first full-run attempt used Sonnet for the investigator and planner plus review on every case, projected at ~$22 for 518 cases, and was stopped after 39 cases ($1.50). Measured on the samples:
    | config | status | step F1 | cost / 32 cases |
    |---|---|---|---|
    | Sonnet inv+planner, review always | 1.00 | 0.90 | ~$1.30 |
    | Sonnet inv+planner, review only on guardrail issues | 1.00 | 0.916 | $1.07 |
    | Sonnet inv, Haiku planner, review on issues | 1.00 | 0.911 | $0.79 |
    | **All Haiku, review on issues, Sonnet escalation only if issues persist (final)** | **1.00** | **0.913-0.934** | **$0.145** |
  - Final model decision: Haiku 5.5 for every agent. Sonnet 5.5 only as escalation for cases whose plan still violates the contract after review (0 of 32 samples). The quality gains came from the prompt and guardrail fixes and from removing the noisy reviewer revisions, not from the larger model.
- **Phase E ✅ Full run:** 518 cases → `output.csv` → validator → determinism rerun.
  - Done: `code/validate.py` implements the README "Before submitting" checklist and the problem-statement ordering. Tested: the blank template fails; a structurally valid synthetic file passes.
  - Row order: GitHub only says "sorted by case_id". Output follows the order of `dataset/deletion_requests.csv`, which is numeric (case_33 … case_550).
  - Evidence references: the README allows `file_id`, `activity_id` or `audit_id`. The guardrail, validator and prompts now accept all three (the samples cite files).
  - Done: run `final_full_v3` produced `output.csv` (518 rows: COMPLETE 153, INCOMPLETE 355, NOT_APPLICABLE 10). The user ran `code/validate.py`, which reports VALID. Full-run cost was ~$2.31 (2,048 calls, ~31.8k tokens per request).
- **Phase F (built; user runs the final commands):** `evaluation_report.md`, `usage_report.md`, `code/README.md`, `code.zip` (exclude dataset/, .venv, caches, .env).
  - `code/evaluation/make_reports.py` generates both reports from `score.py` and the usage ledger.
  - `code/evaluation/custom_cases/` has 4 participant-built cases: upper-case email, Korean request, marketing-only opt-out, correction request.
  - `code/package_submission.py` builds `code.zip` (README.md + code/, no caches, secrets, dataset or nested archives; fails on anything that looks like a key).
  - `requirements.txt` moved to `code/`. A solution section was added at the top of the root README.md.

## 6. Derived conventions (Phase A — from all 32 samples)
**6.1 Step patterns, by required treatment** (the category comes from the rules; the agent decides it, no fixed table)
| Situation at audit_at | FINDING | REMEDIATION | VERIFICATION |
|---|---|---|---|
| DELETE-category record still present (profile, search, support attachment, export, contact/marketing-like record, replica copy) | REPORT_REMAINING_RECORD | DELETE_RECORD | VERIFY_RECORD_ABSENT |
| REDACT/ANONYMIZE-category record still identifiable (support message, analytics, tombstone with PII) | REPORT_EXPOSED_PII | REDACT_RECORD / ANONYMIZE_RECORD | VERIFY_RECORD_REDACTED / _ANONYMIZED |
| Connector enabled for the subject | REPORT_ACTIVE_DATA_FLOW | DISABLE_DATA_FLOW | VERIFY_DATA_FLOW_DISABLED |
| Backup restorable for the subject | REPORT_RESTORE_RISK | BLOCK_RESTORE | VERIFY_RESTORE_BLOCKED |
| Account already gone | — | — | VERIFY_RECORD_ABSENT (always listed) |
| Security audit record (RETAIN) | — | — | VERIFY_RECORD_PRESENT (always listed if one exists) |
| Already compliant (anonymized, redacted, flow disabled, restore blocked) | — | — | matching VERIFY_* |
- **INCOMPLETE:** findings plus remediation for the violations only. Verifications cover **every relevant resource**, compliant ones included. Remediation and verification evidence = "".
- **COMPLETE:** verifications only, for every relevant resource, each citing evidence.
- **Ordering:** phase → operation table order → system_id → resource_id. This matched all 32 samples, so no special "downstream first" handling was needed.
- **Copies:** a replica copy is reported under the logical `system_id` (e.g. `account_service`), and the SQL replica file is cited.

**6.2 Evidence citation**
- Only `evidence_XX` file IDs are cited, never activity/audit IDs.
- Cite the **latest file captured ≤ audit_at that shows the resource's state**. In practice one file per case usually covers all of its steps.
- Later files exist for the same resources (decoys for time) and must be ignored.

**6.3 Linking resources to the subject** (hardest part)
- **Grounded links:** identifier fields in evidence (email in any letter case, email_hash, phone, `user_id` as subject/customer_reference), log trace correlation (`lookup_key` → `resource_id` via `trace_id`), `LINK_RESOURCE_IDENTITY` activity, and images/PDFs.
- **Historical identifiers matter:** e.g. a record keyed by an old email hash → DELETE.
- **Ungrounded accounts:** many account resources appear **only** in activity (`DELETE_RECORD` "primary profile"), with no identifier. They are found through the workflow job: the account deletion runs ~45–75 min before `reported_at`, and policy actions follow. Timing alone is ambiguous (1–6 candidates; ~40% correct on samples, but the true account was always among the candidates). Strategy: grounded links first, then **exclusion** of accounts grounded to other users, then a global one-to-one assignment by timing. The Investigator agent resolves the remaining ties with its reasons recorded.
- **Unknowable:** a few gold resources appear in no participant data (e.g. some audit records). Accept the loss; never fabricate.
- Substring matching causes false links (`user_32` ≠ `user_320`). Use token-boundary matching.

**6.4 Evidence content facts**
- **States:** handover/TXT rows give `system | resource | record_type | state(NOT_FOUND/FOUND/IDENTIFIABLE/ACTIVE) | subject`. JSON exports give fields plus `enabled` / `restore_allowed`. Logs give `rows=0/404` (absent), `matched_value` (exposed PII), `enabled=`, `restore_allowed=`, `retention_until=`. SQL gives replica tables.
- **Activity:** `CHECK_DATA_FLOW` "remains enabled" and `CHECK_RESTORE_ACCESS` "can still be restored" are risk facts; `SYNC_RECORD` / "repopulated" means a record was recreated. `reported_result` is never proof.
- **Images/PDFs:** case-specific and few (a console per subject, legal register, retention journal). The legal register lists orders, not resources, so it produces no steps unless a matching resource exists.

**6.5 Split for evaluation:** stratified by status. Within each status (case_id order), cases alternate dev / held-out, giving 16/16 cases (COMPLETE 5/5, INCOMPLETE 11/11). Plain odd/even was not stratified (COMPLETE 6/4).

## 7. Verification
- `python code/main.py --samples` + `python code/evaluation/score.py` → metrics on dev and held-out splits.
- `python code/validate.py output.csv` → 518 rows, sorted, valid JSON, allowed enums, evidence IDs exist and are ≤ audit_at.
- A rerun with a warm cache gives a byte-identical `output.csv`.
- `grep` the code and prompts for literal `case_`/`user_`/`evidence_` IDs → none (no-hardcoding check).

## 8. Setup / run
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env   # add ANTHROPIC_API_KEY
.venv\Scripts\python code\main.py
```
Submission link: https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
