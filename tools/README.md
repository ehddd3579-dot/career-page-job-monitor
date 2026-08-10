# These files are behind the code they generate

`generate_actors.py` and `_main_template.py.txt` in this directory are an
**older generation** than the Actors under `actors/`. Regenerating from what is
committed here would overwrite working code with stale code — silently, because
the output still looks plausible.

Known to be missing here, but present in the deployed Actors:

- the input parsing that stops a bare string being read one character at a time
  and billed per character
- the store name that survives `LIMITED_PERMISSIONS`, so change detection works
- `output_schema.json` generation, and the `hint` / `error` / `recordType`
  columns in the dataset view
- the endpoint and FAQ sections of the README

**Before running this script, sync it from the canonical working copy.** The
`actors/` tree and `src/` are the truth; this directory is documentation that
has not caught up.

The same applies to `tests/` — `delta_test.py`, `input_test.py`,
`delta_wiring_test.py`, `signal_test.py` and `signal_wiring_test.py` are either
absent here or older than the code they cover.
