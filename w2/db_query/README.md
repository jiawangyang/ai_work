# Database Query Tool

A web-based tool for managing PostgreSQL database connections, viewing metadata, and executing SQL queries with natural language support.

## Project Structure

```
w2/db_query/
├── backend/          # FastAPI backend (Python 3.12+)
├── frontend/         # React frontend (TypeScript, Refine 5)
├── fixtures/         # REST Client test files
│   ├── test.rest     # API test requests
│   └── README.md     # Testing guide
└── Makefile          # Development commands
```

## Quick Start

### Initial Setup

```bash
# Install all dependencies
make install

# Setup database and environment
make setup
# Then edit backend/.env and add your OPENAI_API_KEY

# Start development servers
make dev
```

### Development Commands

```bash
# View all available commands
make help

# Start backend only
make dev-backend

# Start frontend only
make dev-frontend

# Run tests
make test

# Format code
make format

# Run linters
make lint
```

## API Testing

### Using REST Client (VSCode)

1. Install [REST Client extension](https://marketplace.visualstudio.com/items?itemName=humao.rest-client)
2. Open `fixtures/test.rest`
3. Click "Send Request" above any HTTP request
4. View responses in VSCode panel

See `fixtures/README.md` for detailed testing guide.

### Using Makefile

```bash
# Check if backend is running
make health

# Open API documentation
make docs
```

## Data Export

Query results can be exported as CSV or JSON (design: `FEATURE_EXPORT.md`).

### From the UI

After executing a query, use the **Export** dropdown on the *Query Results* card,
or answer the proactive "Export query result?" dialog (it appears after each
successful query with rows; "Don't ask again this session" silences it).

### From the API

```bash
# Explicit SQL
curl -X POST http://localhost:8000/api/v1/dbs/todo/export \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT * FROM users LIMIT 100", "format": "csv"}' \
  -o result.csv

# Replay the latest successful query for the database (no sql needed)
curl -X POST http://localhost:8000/api/v1/dbs/todo/export \
  -H "Content-Type: application/json" \
  -d '{"format": "json"}' -o result.json
```

### From the CLI (one command: natural language → SQL → export)

```bash
cd backend
uv run python scripts/export_query.py --db todo --nl "top 10 users by order count" --format csv
```

### From Claude Code (agent automation)

With Claude Code opened at this project root:

```
/export todo average salary per department, export as csv
```

The custom command lives in `.claude/commands/export.md` and drives the CLI above.

## Phase 1 Status

✅ **Phase 1 Complete**: All setup and foundation tasks completed.

- Backend project structure initialized
- Frontend project structure initialized
- Core infrastructure (FastAPI, database, models) ready
- Data models defined with camelCase API convention
- Makefile with common development tasks
- REST Client test file for API testing

## Next Steps

Proceed to Phase 2 for core feature implementation (US1 + US2).
