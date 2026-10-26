# Running the audit agent

Run commands from the repository root. The challenge `dataset/` directory
must be present there.

## Setup

Requires Python 3.11 or later and an Anthropic API key.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r code/requirements.txt
```

Set `ANTHROPIC_API_KEY` in your environment. Do not commit API keys or put them
in source files.

## Commands

```powershell
python code/main.py
python code/validate.py output.csv
```

The first command processes all requests and writes `output.csv`. Other
available commands:

```powershell
python code/main.py --samples
python code/main.py --custom
python code/inspect_case.py case_05
python code/package_submission.py
```

Sample output is written to `output_samples.csv`; custom evaluation output is
written to `output_custom.csv`. The package command builds `code.zip` without
including the dataset.
