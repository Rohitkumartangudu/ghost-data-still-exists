# Ghost Data Still Exists

An AI-assisted audit tool for checking whether personal-data deletion requests
have actually been completed. A workflow can report success while personal
data remains in replicas, logs, exports, support records, or backups. This
project checks the available evidence at the audit time and identifies records
or data flows that may still need attention.

## What it does

- Determines whether a request authorizes deletion.
- Connects current and historical identifiers to records in the supplied evidence.
- Applies the supplied retention and deletion rules.
- Reports `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE` and produces ordered,
  evidence-linked steps in the required CSV format.

## Run

Use Python 3.11 or later. Keep the challenge's `dataset/` directory at the
repository root.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r code/requirements.txt
```

Set `ANTHROPIC_API_KEY` in your environment, then run from the repository root:

```powershell
python code/main.py
python code/validate.py output.csv
```

The full run reads `dataset/deletion_requests.csv` and writes `output.csv`.
For sample requests, run `python code/main.py --samples`; this writes
`output_samples.csv`.

## Project files

- `code/` contains the audit agent, prompts, and evaluation tools.
- `dataset/` contains the requests, policy, identities, and evidence used by
  the challenge. It is not included in the submission archive.
- `output.csv` contains the full-run predictions.
- `code/evaluation/` contains the evaluation and usage reports.

See [`code/README.md`](code/README.md) for setup and run details.
