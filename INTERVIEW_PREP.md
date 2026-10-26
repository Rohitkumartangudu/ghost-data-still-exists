# Interview Prep — Ghost Data Still Exists (HackerRank Orchestrate, Oct 2026)

> Personal preparation notes. Every number here comes from the final runs (`final_full_v4`, `final_samples`, `final_custom2`) and the generated reports in `code/evaluation/`.

---

## 1. 30-second pitch

"I built a multi-agent deletion auditor. For each of 518 privacy requests it decides whether deletion was actually requested and whether the data is truly gone at the audit cut-off. The pipeline has two layers. A deterministic layer parses every evidence format (SQL snapshots, JSON, logs, text, PDFs, screenshots) into one fact store, resolves current and historical identities, and assembles a case dossier. Claude agents then do the judgement: intake, investigation with tools, policy planning, review, and vision extraction. A code guardrail enforces the exact output contract, so the model never controls ordering, numbering, or invalid operations. On the 32 labelled samples it gets 100% status accuracy, step F1 0.895, and 100% evidence accuracy. Four custom stress cases match exactly. The full 518-case run cost about $2.29 (~$0.0044 per request) on Claude Haiku 5.5, after I measured that the larger model didn't pay for itself."

---

## 2. Problem statement (what they asked)

**Goal:** audit whether a user's personal data was really deleted. A workflow can report "Complete" while copies remain in databases, replicas, logs, search indexes, support attachments, exports, analytics, archives, or backups. Active sync or restore jobs can also recreate deleted data.

**Per request the agent must:**
1. Decide whether the request authorizes deletion. If not, return `NOT_APPLICABLE` with `[]`. Examples: access, correction, export, marketing opt-out.
2. Resolve the subject's **current and historical** identifiers (emails, phones, email hashes, user_id).
3. Find every relevant copy, using **only evidence at or before `audit_at`**.
4. Apply `deletion_rules.csv`: DELETE / REDACT / ANONYMIZE / RETAIN. An account tombstone keeps only the internal user_id.
5. Decide the status:
   - **COMPLETE**: every copy has the right treatment, retained records still exist, and no active flow or restore path remains.
   - **INCOMPLETE**: anything else.
6. Write `steps_json`:
   - INCOMPLETE: FINDING → REMEDIATION → VERIFICATION.
   - COMPLETE: evidence-backed VERIFICATIONs only.

**Inputs (`dataset/`):** 518 requests, 32 labelled samples, 850 identities, 306 identity changes, 4,011 activity events, 15 deletion rules, and 94 evidence files (24 SQL, 28 JSON, 26 logs, 1 TXT, 4 PDF, 11 PNG).

**Output contract:**
- Header `case_id,deletion_status,steps_json`, 518 rows sorted by case_id.
- Each step has `step_no, phase, system_id, resource_id, operation, state, evidence_id`.
- Only the 15 allowed operation/state pairs may be used.
- Ordering is phase → operation-table order → system_id → resource_id.
- Findings and COMPLETE verifications cite evidence (`file_id`, `activity_id` or `audit_id`). Remediation and future checks use `""`.

**Deliverables:** `output.csv`, `code.zip` (code, prompts, README, `evaluation/evaluation_report.md`, `evaluation/usage_report.md`), `log.txt` as the chat transcript, and the AI interview.

**What makes it hard:**
- `reported_status` and activity `reported_result` are **claims, not proof**.
- The time cut-off: later evidence must not be used or cited.
- Historical identifiers: old emails, old hashes, mixed-case emails.
- **Decoys**: look-alike names and emails belong to other people.
- **Replica drift**: a record deleted from the primary still lives in a replica.
- **Recreation paths**: enabled connectors, restorable backups.
- Policy nuance: redact vs anonymize vs retain, tombstones, retained security logs.
- Some accounts appear **only** in the activity log with no identifier at all.

---

## 3. Architecture

```
request ──► [Intake agent]  ── not deletion ──► NOT_APPLICABLE, []
               │  lexical cross-check; disagreement → [Intake-review agent] (independent prompt)
               ▼
        Dossier builder  (deterministic, no LLM)
          • identity resolver (current + historical + derived sha256 hashes)
          • fact store over SQL / JSON / logs / text / PDF text / image extractions
          • linker: grounded links, ownership exclusion, workflow-job global assignment
          • replica drift, snapshot absence, citable flags, documents, rules
               ▼
        [Investigator agent]  (tool use: search_evidence, get_resource_history, query_snapshot,
               │               lookup_identifier_owner, get_evidence_file_info)
               │  → per resource: belongs_to_subject, data_category, state_at_audit, evidence, rationale
               ▼
        [Policy & Planner agent] → deletion_status + steps (JSON schema)
               ▼
        Contract guardrail (code): derive state from op, drop unknown/invented ids, evidence rules,
               │                    ordering, numbering, consistency (INCOMPLETE needs a finding)
               ▼  only if the guardrail reports issues
        [Reviewer agent] → one planner revision → [Sonnet escalation] if issues persist
               ▼
        output row  (+ per-case trace JSON for debugging)

One-time: [Evidence Extraction agent] (vision) reads each PNG → structured facts (cached).
```

**Why hybrid (code and agents)?** Parsing, joining, time filtering and output formatting are exact jobs where LLMs make costly mistakes. Intent, ownership judgement, state interpretation, policy application and reading screenshots are judgement jobs where LLMs add value. Each half does what it is good at.

**Repository map:**

| Path | Role |
|---|---|
| `code/main.py` | CLI entry (`--samples`, `--custom`, `--cases`, `--run-id`) |
| `code/ghost/data/` | loader, normalize, identity, parsers, facts (fact store), linker |
| `code/ghost/tools/` | dossier builder, read-only tool registry |
| `code/ghost/agents/` | agent calls (`core.py`), JSON schemas (`schemas.py`) |
| `code/prompts/*.md` | system prompts for the 6 agent roles |
| `code/ghost/llm.py` | Claude client: disk cache, usage ledger, tool loop, refusal fallback |
| `code/ghost/steps.py` | contract guardrail |
| `code/ghost/contract.py` | the only fixed vocabulary (phases, 15 op/state pairs, order) |
| `code/ghost/orchestrator.py` | per-case pipeline, concurrency, retries, escalation |
| `code/config.yaml` | models, effort, limits, pricing, linking parameters |
| `code/validate.py` | README submission checklist |
| `code/evaluation/` | scorer, custom cases, report generator, reports |

---

## 4. The agents (5 core roles + 1 helper + escalation)

| # | Agent | Model | Runs when | Input | Output (JSON schema) |
|---|---|---|---|---|---|
| 1 | **Intake** | Haiku 5.5, effort low | every case | request text | authorizes_deletion, request_type, language, confidence, rationale |
| 1b | **Intake-review** (helper) | Haiku 5.5 | intake disagrees with lexical check, or low confidence | text + first verdict + lexical signal | same schema |
| 2 | **Evidence Extraction** | Haiku 5.5 (vision) | once per screenshot (11), cached | image | screen type, identifiers, records (resource_id, system, status, details), facts |
| 3 | **Investigator** | Haiku 5.5, effort medium, **tool use** | every deletion case | full dossier | per resource: ownership, category, state at audit, evidence id, rationale |
| 4 | **Policy & Planner** | Haiku 5.5, effort medium | every deletion case | request + investigation + evidence index + rules | status + steps (phase, system, resource, operation, evidence) |
| 5 | **Reviewer** | Haiku 5.5, effort low | **only if the guardrail reports issues** | dossier + investigation + plan + issues | approved, issues[], summary |
| E | **Escalation** (Investigator + Planner) | Sonnet 5.5 | issues still present after review; accepted only if not worse | same as 3–4 | same |

**Deterministic components (not agents):** identity resolver, 5 format-generic parsers, fact store, linker, dossier builder, 6 read-only tools (5 exposed to the Investigator), contract guardrail, validator, scorer, report generator.

**Tools exposed to the Investigator:** `search_evidence`, `get_resource_history`, `query_snapshot` (read-only single SELECT), `lookup_identifier_owner`, `get_evidence_file_info`. The `as_of` argument is **forced to the case's audit_at**, so the agent cannot look into the future.

---

## 5. Key design decisions (and why)

1. **Evidence over claims.** Workflow statuses and activity results are never proof. Only observations count, such as 404s, rows=0, NOT_FOUND, presence in a snapshot, or connector `enabled=true`.
2. **Two time rules.**
   - A record *observed* at or before audit_at can be used for reasoning, even if its file was captured later. Gold relies on this for linking.
   - Only files *captured* at or before audit_at can be **cited**.
3. **No hardcoding.**
   - No case, user, resource or evidence IDs in code or prompts.
   - Resource prefix → system and record-type mappings are **learned from the data**.
   - Categories come from `deletion_rules.csv`; models, prices and windows live in `config.yaml`.
   - The only fixed vocabulary is the spec's output contract, in `contract.py`.
4. **Structured outputs.** The Claude 5.5 models take no temperature and reject forced `tool_choice`, so every agent ends with a JSON-schema-constrained answer (`output_config.format`) and enums come from the contract and data.
5. **The guardrail owns the format.** The model proposes steps. Code derives the state, sorts, numbers, applies the evidence rules and drops invented IDs, so contract errors are 0.
6. **Reproducibility.** Every response is cached on disk by exact request hash, so reruns are byte-identical and free. Usage is logged per call, including calls served from the cache.
7. **Linking strategy.**
   - Grounded identifiers come first (token-boundary matching, so `user_32` doesn't match `user_320`).
   - Resources owned by other people are excluded.
   - Identifier-less primary profiles are assigned by a **global one-to-one** workflow-job assignment, scored by evidence co-occurrence, then timing.
8. **Consistency rule.** INCOMPLETE requires at least one evidenced violation. Otherwise the case is COMPLETE, and COMPLETE verifications must cite evidence.
9. **Empty snapshots prove nothing.** A snapshot with zero rows is not absence evidence (found by custom case 1).
10. **Aliases.** Catalogue activity such as `object_19 (delivery_confirmation_19.pdf)` maps screenshot file names to resource IDs.
11. **Review only on issues.** The always-on reviewer was measured to hurt (see §7).

---

## 6. Evaluation

**Scorer** (`code/evaluation/score.py`). The official formula isn't published, so the metric definitions are mine:
- status accuracy (case level)
- step precision, recall and F1 over full steps (row level), also computed without evidence_id
- resource F1
- position-aligned field accuracy (field level)
- sequence accuracy: the whole ordered list must match
- evidence accuracy
- contract-error rows

**Split:** stratified by status, alternating within each status: dev 16 / held-out 16 (COMPLETE 5/5, INCOMPLETE 11/11).

**Final results:**

| split | status acc. | step P | step R | step F1 | resource F1 | evidence acc. | sequence acc. |
|---|---|---|---|---|---|---|---|
| all 32 | 100% | 0.905 | 0.886 | **0.895** | 0.907 | 100% | 46.9% |
| dev 16 | 100% | 0.911 | 0.920 | 0.915 | 0.922 | 100% | 56.2% |
| held-out 16 | 100% | 0.898 | 0.850 | 0.873 | 0.891 | 100% | 37.5% |
| custom 4 | 100% | 1.000 | 1.000 | 1.000 | 1.000 | 100% | 100% |

**Custom cases** (each reuses a sample's evidence and changes one input, so the answer is known):

| case | change | tests |
|---|---|---|
| custom_01 | email in UPPER CASE (case_05) | identity normalization → found the empty-snapshot bug |
| custom_02 | request rewritten in Korean (case_14) | multilingual intake |
| custom_03 | "remove me from marketing lists only" | opt-out with the word "remove" → NOT_APPLICABLE |
| custom_04 | phone correction request | non-deletion for a subject with real remaining data |

**Iteration history:**

| iteration | change | status | step F1 | cost per 32 cases |
|---|---|---|---|---|
| 1 | first end-to-end, all Haiku | 71.9% | 0.764 | $0.26 |
| 2 | intake fix, snapshot coverage, status consistency | 96.9% | 0.807 | $0.24 |
| 3 | job-linked profile accepted, retained-log policy, attachments | 96.9% | 0.897 | $0.23 |
| 4 | drop invented IDs, finding grounding, retries | 96.9% | 0.883 | $0.23 |
| 5 | Sonnet Investigator + Planner | 100% | 0.914 | $1.33 |
| 6 | reviewer only on issues | 100% | 0.916 | $1.07 |
| 7 | all Haiku + review on issues + escalation (final) | 100% | 0.895–0.934* | $0.145 |

\*Run-to-run variation from LLM sampling. The final submitted samples run is 0.895.

**Ceiling:** a few gold resources (for example `account_004`, `audit_record_6645`, `audit_record_5569`) appear in no participant-visible data, so no honest system can recover them.

---

## 7. Cost and model selection story

- The first full run used Sonnet for the Investigator and Planner plus an always-on reviewer. It was projected at **~$22** and I stopped it after 39 cases.
- Per-agent cost analysis: the Sonnet Investigator and Planner were 96% of spend. The Haiku Reviewer forced a revision in **46% of cases**, and the revisions **lowered** F1 because it rejected correct workflow-job links.
- Measurements on the samples: all-Haiku with review only on guardrail issues matched or beat Sonnet at **$0.145 per 32 cases**, about 10× cheaper.
- **Final full run:** 2,044 model calls, ~16.4M tokens (~31.6k per request), **$2.29 total, ~$0.0044 per request**. Sonnet escalation ran twice (~$0.03).
- Prices used (per 1M tokens, input/output): Haiku 5.5 $0.10 / $0.50, Sonnet 5.5 $2 / $10. Opus was not needed.

---

## 8. Pros / strengths

- **Correct by construction on format**: 0 contract errors, and the validator passes.
- **Evidence discipline**: 100% evidence accuracy on samples. Time-leak protection is built into the tools (forced `as_of`) and the guardrail.
- **No hallucinated resources**: IDs must exist in participant data, and image IDs must have a known prefix.
- **Generic parsing**: one parser per format, no per-file rules. It handles 6 log styles, nested JSON, replica SQL, and text tables.
- **Explainable**: every case has a trace (dossier, investigation rationale, plan, issues) and every link has a provenance reason.
- **Reproducible and cheap**: cached reruns are free, the full run costs ~$2.3, and switching models is a config change.
- **Measured decisions**: model choice, review loop and ambiguity handling were each A/B tested on the scorer.
- **Honest evaluation**: held-out split, custom cases, and documented limitations.

---

## 9. Cons / drawbacks / limitations (be upfront)

1. **Identifier-less accounts rely on a timing heuristic.** Assignment uses the deletion job ~45–75 minutes before `reported_at`, plus evidence co-occurrence. Concurrent jobs make some cases ambiguous: samples 22, 23 and 31/32 swap accounts. *Defence:* the gold includes accounts that only exist in activity rows, so the only evidence-based link is the workflow job. I made it global, one-to-one and exclusion-based, and flagged ambiguity rather than guessing silently.
2. **Ambiguous follow-ups are excluded** (anonymized analytics with no identifier). This costs recall in some COMPLETE cases. Including them was measured: +5 true positives but +8 false positives, so I excluded them.
3. **LLM non-determinism.** Fresh runs vary (sample F1 0.895–0.934). The cache makes reruns identical, but a cold rerun can differ slightly.
4. **Small labelled set.** 32 samples, a 16-case held-out set, and none of the samples are NOT_APPLICABLE. The custom cases partly compensate.
5. **Prompts encode sample-derived conventions.** For example, "verify every subject resource in INCOMPLETE cases". There is an overfitting risk if the hidden gold differs.
6. **Category judgement is borderline in a few cases.** Example: an analytics contact keyed by a historical hash, where gold says DELETE and the model says ANONYMIZE.
7. **Screenshot extraction can miss or garble facts.** A small model reads them once. The alias and prefix filters reduce damage but can't add facts the model missed.
8. **The guardrail drops unknown IDs**, which could in theory drop a true resource that appears in no data. By the spec such a resource can't be evidenced anyway.
9. **Absence by omission is weak evidence**: a record missing from a partial snapshot. The agent decides how much to trust it.
10. **Some heuristics are dataset-shaped**: job windows, the primary-record category. They're configurable in `config.yaml`, but would need retuning on other data.
11. **No Batch API.** Cost could halve, but at ~$2.3 per run the complexity didn't seem worth it.
12. **Single-machine, single-process design.** It's not a production service: no queue, no authentication, no incremental updates.

---

## 10. What I'd improve with more time

- Use cross-subject consistency (each resource has one owner) as a global constraint solver over all cases, instead of greedy assignment.
- A self-consistency vote: 2–3 cheap Haiku runs per case, keeping the majority plan to reduce variance.
- A larger labelled set (synthesize more cases from the samples) and per-category confusion analysis.
- The Batch API for the full run (50% cheaper), plus prompt-cache tuning.
- Richer replica reasoning: explicit primary-vs-replica timelines per copy.
- Production hardening: idempotent queue, observability dashboard, PII-safe logging, access controls.

---

## 11. Likely interview cross-questions, with answers

**Architecture and agents**

1. **How many agents, and why not one big prompt?**
   Five roles (Intake, Extraction, Investigator, Planner, Reviewer) plus an intake-review helper and Sonnet escalation. Splitting them keeps each prompt focused and lets me use cheap models per role. It also lets me measure each stage separately. One prompt mixing intent, ownership, state, policy and formatting was harder to debug and failed more on format.
2. **Where exactly is the "AI" vs plain code?**
   - AI: intent (multilingual), reading screenshots, deciding ownership when links are weak, interpreting record state, choosing categories, building the plan, and reviewing.
   - Code: parsing, joining, the time filter, candidate generation, output formatting and contract enforcement.
3. **Why does the Investigator get a pre-built dossier instead of exploring with tools from scratch?**
   Cost and reliability. A free-roaming tool loop over 518 cases is expensive and variable. The dossier gives about 95% of what's needed in one shot, and tools remain for real follow-ups.
4. **Why does the Reviewer run only on issues?**
   I measured it. Always-on review caused revisions in 46% of cases and lowered F1, because it wrongly rejected valid workflow-job links. On-issues review gave a higher F1 at lower cost.
5. **What does escalation do, and how do you stop it making things worse?**
   If guardrail issues persist after review, the Investigator and Planner rerun on Sonnet. The result is accepted only if it has steps and no more issues than before. This was added after a full-run bug where escalation replaced a valid plan with an empty one.

**Correctness and evidence**

6. **How do you prevent hallucinated resources or evidence?** In three layers:
   - The prompt requires exact IDs from the dossier.
   - The guardrail drops any resource ID that exists in no participant data, and image-only IDs need a known prefix.
   - Findings must cite evidence that observes that resource and isn't a workflow claim.
7. **How do you enforce `audit_at`?**
   - Facts carry an observed time and a file capture time.
   - The dossier shows only observations at or before audit_at and marks which are citable.
   - Tool calls force `as_of = audit_at`.
   - The guardrail and validator reject evidence dated after audit_at.
8. **Why can you use a record from a file captured after audit_at?**
   Its own `observed_at` is before the cut-off, so the observation happened in time. I use it for reasoning but never cite the file. The gold links follow this pattern.
9. **How do you treat `reported_status = COMPLETE`?**
   As a claim. Many gold INCOMPLETE cases have a COMPLETE workflow status. State comes only from observations.
10. **Walk me through case_05.**
    The account was deleted from the primary, but the failover-replica snapshot (evidence_54) still holds `account_005`. Replica drift shows "present in failover, absent from primary". So the output is FINDING REPORT_REMAINING_RECORD citing evidence_54, then REMEDIATION DELETE_RECORD, then VERIFY_RECORD_ABSENT.
11. **Walk me through case_11.** In the handover file (evidence_03):
    - the search entry is FOUND → remaining record, delete
    - analytics and support are IDENTIFIABLE → exposed PII, anonymize and redact
    - a connector is ACTIVE → disable the flow
    - the security audit record is retained → VERIFY_PRESENT
    - the account is NOT_FOUND → VERIFY_ABSENT

    The guardrail orders them: disable before delete/redact/anonymize, then sorted by system and resource.
12. **How do you handle tombstones, backups and retention?**
    - Tombstones: REDACT, keeping only user_id.
    - Restorable backups: REPORT_RESTORE_RISK, BLOCK_RESTORE, VERIFY_RESTORE_BLOCKED. The archive itself is retained.
    - Security audit logs within retention: RETAIN → VERIFY_RECORD_PRESENT. The internal user reference is allowed.
13. **How do you avoid decoys?**
    Token-boundary matching, no name-only links, and an ownership map. A resource carrying another user's identifiers is excluded unless the subject is also clearly linked.
14. **How do you find historical identifiers?**
    The current snapshot, plus audit-log changes up to audit_at, plus a SHA-256 of every known email. Evidence often carries hashes of old emails.

**Generalization and evaluation**

15. **How do you know it generalizes beyond 32 samples?**
    - Rules come from the spec, not from case IDs.
    - A stratified held-out split scores status 100% and F1 0.873.
    - Four custom stress cases score 4/4, and one of them found a real bug.
    - Parsers are format-generic.
    - A no-hardcoding check: no case, user or evidence IDs in code or prompts.

    Remaining risk: the small labelled set.
16. **Your metrics aren't the official ones. How do you know they correlate?**
    They mirror the scoring dimensions the README lists: status, identity resolution, discovered records and risks, evidence, policy, and step order. I report several views (step, field, sequence, evidence) so no single definition hides problems.
17. **Why is sequence accuracy only 47%?**
    It requires every step of a case to be perfect. One missing identifier-less resource or a swapped account fails the whole case. Step F1 (0.895) and status (100%) show most of each answer is right.
18. **Is the workflow-job timing heuristic a "shortcut" or leakage?**
    It uses only participant data (activity times relative to `reported_at`). It's the same reasoning an auditor would use: which profile did the deletion job for this request process? No gold or generator internals are used. It's configurable and labelled as a heuristic.
19. **What failed, and how did you debug it?**
    Per-case diffs from the scorer, plus per-case traces. Examples:
    - intake misread verification requests
    - the planner returned INCOMPLETE without findings
    - snapshot tables without system names
    - invented IDs from screenshots
    - the noisy reviewer
    - the escalation regression
    - the empty snapshot

**Cost, models, operations**

20. **Why Haiku and not Opus?**
    Measured: Sonnet gave about +3 points in one configuration, but once the pipeline was fixed, all-Haiku matched it at about 10× lower cost. Opus wasn't needed. Sonnet stays as targeted escalation.
21. **What does a full run cost?**
    $2.29 for 518 requests (~$0.0044 each), ~31.6k tokens per request, 2,044 calls. The details are in `usage_report.md`.
22. **How deterministic is it?**
    The deterministic layer is fully deterministic. LLM outputs are cached by request hash, so reruns are identical. Fresh runs vary slightly (sample F1 0.895–0.934), because temperature can't be set on these models.
23. **What if the evidence format changes?**
    The parsers detect structure (SQL tables with `resource_id`, JSON objects with `resource_id`, key=value or JSON logs, pipe tables), not file names. A new format means one new parser function. Prefix → system mappings are relearned automatically.
24. **How would you productionize it?**
    - A queue per request, with idempotent workers.
    - Evidence ingestion into a real store with an index.
    - The Batch API for bulk audits.
    - Monitoring of guardrail issues and escalation rate.
    - Human review for ambiguous linkage.
    - PII-safe logging and access control.
25. **Security and PII?**
    - Keys come from `.env` only, and the packager refuses to zip anything that looks like a key.
    - Logs avoid secrets.
    - Data is synthetic, but the design is read-only: no destructive actions, tools are SELECT-only.
26. **Rate limits or network failures?**
    SDK retries (8, with backoff), case-level retries, and cache reuse. A run with connection errors was fixed by reruns without paying again for completed calls.
27. **Why structured outputs instead of free text plus parsing?**
    Guaranteed valid JSON with enums from the contract. Forced tool calls aren't supported on 5.5 models.

**Process and AI-assistant usage**

28. **How did you use the AI coding assistant?**
    Claude Code drove a phase plan (Plan.md, Phases A–F): sample forensics → data layer → scorer before agents → agents → full run → reports. Every turn is logged in `log.txt` per AGENTS.md. Decisions such as model choice and review mode were measured, not guessed.
29. **Why build the scorer before the agents?**
    So every change is measured. It caught regressions, such as including ambiguous follow-ups, which I then reverted.
30. **What would you do differently?**
    Optimize cost earlier (I first ran expensive configurations), build the custom cases earlier (they found a real bug), and add self-consistency voting.

---

## 12. Numbers cheat sheet

| Item | Value |
|---|---|
| Requests / samples / custom | 518 / 32 / 4 |
| Evidence files | 94 (24 SQL, 28 JSON, 26 log, 1 TXT, 4 PDF, 11 PNG) |
| Facts parsed | ~2,900 records from 82 files + 11 screenshot extractions |
| Final output | COMPLETE 155, INCOMPLETE 353, NOT_APPLICABLE 10 — VALID |
| Samples (all) | status 100%, step F1 0.895, evidence 100%, sequence 46.9% |
| Held-out | status 100%, step F1 0.873 |
| Custom | 4/4 exact |
| Linking (deterministic, samples) | recall 0.88, precision 0.92 |
| Full-run cost | $2.29 (~$0.0044 / request), 2,044 calls, ~31.6k tokens / request |
| Models | Haiku 5.5 for all agents; Sonnet 5.5 escalation (2 calls) |
| Run time | ~1 hour for 518 at concurrency 4 |
| Key commands | `python code/main.py` · `python code/validate.py output.csv` · `python code/evaluation/score.py --pred output_samples.csv` |

---

## 13. Glossary

- **Claim vs evidence**: workflow status or activity result (claim) vs an observed state in a snapshot, export or log (evidence).
- **audit_at**: the cut-off; the state must be judged as of this time.
- **Observed vs citable**: an observation at or before audit_at is usable; a file captured at or before audit_at is citable.
- **Grounded link**: a resource tied to the subject by email, phone, hash, user_id, or a catalogue link.
- **Workflow-job assignment**: linking an identifier-less profile through the deletion job that processed this request.
- **Replica drift**: a record absent from the primary but still present in a replica table.
- **Recreation path**: an enabled connector (data flow) or restorable backup that can bring data back.
- **Tombstone**: a minimal account record keeping only the internal user_id for references; direct identifiers must be removed.
- **Dossier**: the pre-assembled, time-filtered evidence bundle per case.
- **Guardrail**: code that turns agent-proposed steps into the exact contract and reports violations.
- **Escalation**: rerunning investigation and planning on a stronger model only when contract issues persist.
