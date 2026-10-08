---
description: Execute a query (SQL or natural language) and export the result to CSV/JSON in one step
argument-hint: <database> <query intent or SQL> [csv|json]
allowed-tools: Bash(curl:*), Bash(uv run:*), Bash(python:*), Read, Grep
---

# Query & Export

User input: $ARGUMENTS

## Steps

1. **Parse `$ARGUMENTS`** into three parts:
   - `database`: the connection name (usually the first word). When unsure, list
     connections first: `curl -s http://localhost:8000/api/v1/dbs`.
   - query: treat as SQL if it starts with `SELECT` or clearly contains SQL;
     otherwise treat the whole phrase as natural language and use `--nl`.
   - format: `json` if the word "json" appears, otherwise `csv`.

2. **Check the backend is running**: `curl -s http://localhost:8000/health`.
   If it does not respond, tell the user to run `make dev-backend` from
   `w2/db_query` (do not start long-running servers yourself) and stop here.

3. **Run the export CLI** from the backend directory (adjust the relative path
   if your working directory differs):

   ```bash
   cd w2/db_query/backend
   uv run python scripts/export_query.py --db <database> --nl "<query intent>" --format <csv|json> --yes
   ```

   - Use `--sql "<SQL>"` instead of `--nl` when the user provided SQL.
   - Drop `--yes` only when the user wants to confirm the generated SQL interactively.
   - Add `--out <path>` when the user requested a specific output location.

4. **Report the result**: absolute file path, row count, and format (the CLI
   prints them on the last line).

5. **On failure**, read the error message and react:
   - `Metadata not found` / `No successful query found` → suggest refreshing
     metadata in the UI first, or provide the SQL explicitly.
   - SQL validation error (e.g. non-SELECT) → explain that only SELECT is allowed.
   - Table/column error → check metadata via the API and retry once with a fix.
