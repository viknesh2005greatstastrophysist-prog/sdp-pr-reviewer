# Injected-defect benchmark card

- Cases: 60 (seed 20261113); corpus SHA-256 (evaluation/injection_corpus.jsonl): `64b80e61e911690026b30d65b41eecc405076ad01f21c7c7fe04b5bdb6340fbe`
- Each case adds one real source file from the evaluation corpus with exactly one deterministic injected defect; ground truth = the injected line (recorded in metadata).
- Split inherited from the source repository: {'test': 27, 'dev': 33}

| Category | Mutation | Cases |
| --- | --- | ---: |
| architecture | hard-coded-endpoint | 4 |
| architecture | layering-violation | 4 |
| architecture | process-global-state | 4 |
| architecture | removed-default-argument | 3 |
| bug-hunter | command-injection | 3 |
| bug-hunter | eval-of-input | 3 |
| bug-hunter | missing-none-check | 1 |
| bug-hunter | sql-injection | 3 |
| bug-hunter | swallowed-exception | 2 |
| bug-hunter | wrong-comparison | 3 |
| performance | n-plus-one-query | 4 |
| performance | quadratic-loop | 4 |
| performance | regex-compiled-in-loop | 3 |
| performance | string-concat-in-loop | 4 |
| style | comparison-to-true | 4 |
| style | mutable-default-argument | 2 |
| style | type-equality | 4 |
| style | wildcard-import | 5 |

| Category | Cases |
| --- | ---: |
| bug-hunter | 15 |
| performance | 15 |
| style | 15 |
| architecture | 15 |
