# Ghost Data Still Exists - deletion-audit agent

A multi-agent system (Claude) that audits whether a personal-data deletion is truly complete at a request's `audit_at` time, and writes `output.csv` (`case_id,deletion_status,steps_json`).

## Setup

Requires Python 3.11+ and an Anthropic API key.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r code/requirements.txt
# create .env at the repository root containing: ANTHROPIC_API_KEY=<your key>   (never commit it)
```

The `dataset/` folder from the challenge repository must be present at the repository root.

## Run (from the repository root)

| Command | What it does |
|---|---|
| `python code/main.py` | All 518 requests in `dataset/deletion_requests.csv` -> `output.csv` |
| `python code/validate.py output.csv` | Checks `output.csv` against the README submission checklist |
| `python code/main.py --samples` | The 32 labelled samples -> `output_samples.csv` |
| `python code/evaluation/score.py --pred output_samples.csv [--split dev\|holdout] [--details]` | Accuracy against `dataset/sample_output.csv` |
| `python code/main.py --custom` | Participant-built evaluation cases -> `output_custom.csv` |
| `python code/evaluation/make_reports.py --full-run <run_id>` | Regenerates `evaluation_report.md` and `usage_report.md` |
| `python code/inspect_case.py <case_id>` | Prints the deterministic case dossier (no API calls) |
| `python code/package_submission.py` | Builds `code.zip` (excludes caches, secrets, dataset) |

Useful options for `main.py`: `--cases case_05,case_11`, `--limit N`, `--run-id NAME`, `--concurrency N`, `--output PATH`.
Model per agent can be overridden with environment variables, e.g. `MODEL_INVESTIGATOR=claude-sonnet-5-5`.

Every model response is cached on disk (`code/.cache/llm`, keyed by the exact request), so re-running reproduces the same output at no extra cost; delete `code/.cache` for a fresh run. A full run takes about an hour at concurrency 4 and costs about $2.3 with the default models.

## Architecture

```
request ──► Intake agent (Haiku) ── not a deletion ──► NOT_APPLICABLE, []
              │   (+ lexical cross-check; independent second opinion on disagreement)
              ▼
        Dossier builder (deterministic tools, no LLM)
          identity (current + historical identifiers, hashes) · fact store over SQL/JSON/log/text/PDF/image
          evidence · grounded links · replica drift · snapshot absence · workflow-job candidates
              ▼
        Investigator agent (Haiku, tool use) ── per resource: ownership, category, state at audit_at, evidence
              ▼
        Policy & Planner agent (Haiku) ── status + steps (structured output)
              ▼
        Contract guardrail (code) ── ordering, numbering, op/state pairs, evidence rules, unknown ids
              ▼ (only if the guardrail reports issues)
        Reviewer agent (Haiku) ──► one planner revision ──► Sonnet escalation if issues persist
```

- `code/ghost/data/` - loaders, identity resolution, format-generic parsers, fact store, linker.
- `code/ghost/tools/` - dossier and the read-only tools exposed to the investigator (`registry.py`).
- `code/ghost/agents/` - agent calls and JSON schemas; prompts live in `code/prompts/*.md`.
- `code/ghost/llm.py` - Claude client: disk cache, usage ledger, tool loop with JSON-schema final answers.
- `code/ghost/steps.py` - output-contract guardrail; `code/ghost/contract.py` - the only fixed vocabulary.
- `code/config.yaml` - models, effort, limits, pricing, linking parameters (no case-specific values).
- `code/evaluation/` - scorer, custom cases, report generator, reports.

Design rules: evidence over claims (`reported_status`/`reported_result` are never proof); only evidence observed at or before `audit_at`, and only files captured at or before it are cited; no hardcoded case, user, resource or evidence ids; resource ids must exist in participant data.
