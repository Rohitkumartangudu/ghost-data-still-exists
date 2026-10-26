# Participant-created evaluation cases

Each case reuses the evidence of a public sample and changes one input, so the expected answer is known without organizer data.

| case | derived from | change | what it tests |
|---|---|---|---|
| custom_01 | case_05 | email | Identity normalisation: the request email is supplied in upper case; expected output equals case_05. |
| custom_02 | case_14 | request_text | Multilingual intake: case_14 rewritten in Korean; expected output equals case_14. |
| custom_03 | case_06 | request_text | Non-deletion despite the word 'remove': a marketing-only opt-out must be NOT_APPLICABLE. |
| custom_04 | case_05 | request_text | Correction request for a subject who has real remaining data: must be NOT_APPLICABLE with []. |

Run: `python code/main.py --custom` then `python code/evaluation/score.py --pred output_custom.csv --gold code/evaluation/custom_cases/expected.csv --details`.
