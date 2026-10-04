# Demo: offline dependency-diff explainer

This demo compares two explicit target environments against one shared
requirements list and reports which requirements become enabled / disabled /
unknown because of their markers. Nothing is downloaded or resolved.

## Files

- `env_a.json` -- environment A: Python 3.10, `fast` extra requested
- `env_b.json` -- environment B: Python 3.12, no extras requested
- `requirements.txt` -- one requirement per line (PEP 508)
- `run_demo.py` -- convenience runner

## Run

```sh
# Human-readable text
python demo/run_demo.py

# Machine-readable JSON
python demo/run_demo.py --json
```

Or directly via the module:

```sh
python -m packaging.diffexplain demo/env_a.json demo/env_b.json demo/requirements.txt
```

## What the demo shows

| Requirement | A (3.10, extra=fast) | B (3.12, no extra) | Why |
|---|---|---|---|
| `foo; python_version < "3.11"` | enabled | disabled | `python_version < "3.11"` goes True -> False |
| `bar; extra == "fast"` | enabled | disabled | `extra == "fast"` True only for the `fast` extra |
| `baz>=1.0` | enabled | enabled | no marker |
| `qux; os_name == "plan9"` | disabled | disabled | `os_name` is `posix` in both |
| `quux; platform_release > "5.0"` | unknown | unknown | `platform_release` is missing from both env JSONs |
| `andex; os_name == "posix" and python_version < "3.11"` | enabled | disabled | conjunction; the python_version atom flips |
| `orex; os_name == "plan9" or python_version < "3.11"` | enabled | disabled | disjunction; the python_version atom flips |
| `dup>=2.0` + `dup<1.0` | conflict | conflict | mutually exclusive specifiers; sources preserved |
