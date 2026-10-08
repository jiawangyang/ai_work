"""FastMCP server for PostgreSQL natural language query interface.

This module implements the MCP server using FastMCP, exposing the query
functionality as an MCP tool. It includes complete lifespan management for
initializing and cleaning up all components.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from asyncpg import Pool
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import DatabaseConfig, Settings
from pg_mcp.db.pool import close_pools, create_pools
from pg_mcp.models.query import QueryRequest, QueryResponse, ReturnType
from pg_mcp.observability.logging import configure_logging, get_logger
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = get_logger(__name__)

# Global state for lifespan management
_settings: Settings | None = None
_pools: dict[str, Pool] | None = None
_schema_cache: SchemaCache | None = None
_orchestrator: QueryOrchestrator | None = None
_metrics: MetricsCollector | None = None
_circuit_breaker: CircuitBreaker | None = None
_rate_limiter: MultiRateLimiter | None = None


@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:  # type: ignore[type-arg]
    """Lifespan context manager for server initialization and cleanup.

    This function manages the complete lifecycle of the MCP server:

    Startup:
        1. Load configuration from Settings
        2. Configure logging
        3. Create database connection pools
        4. Load schema cache for all databases
        5. Initialize metrics collector
        6. Create service components (generators, validators, executors)
        7. Initialize resilience components (circuit breaker, rate limiter)
        8. Create query orchestrator
        9. Start metrics HTTP server (optional)

    Shutdown:
        1. Stop schema auto-refresh (if enabled)
        2. Close all database connection pools
        3. Stop metrics HTTP server (if running)

    Yields:
        None

    Example:
        >>> async with lifespan():
        ...     # Server is running with all components initialized
        ...     pass
    """
    global _settings, _pools, _schema_cache, _orchestrator, _metrics
    global _circuit_breaker, _rate_limiter

    logger.info("Starting PostgreSQL MCP Server initialization...")

    try:
        # 1. Load Settings
        logger.info("Loading configuration...")
        _settings = Settings()

        # 2. Configure logging
        logger.info("Configuring logging...")
        configure_logging(
            level=_settings.observability.log_level,
            log_format=_settings.observability.log_format,
            enable_sensitive_filter=True,
        )

        logger.info(
            "Configuration loaded",
            extra={
                "environment": _settings.environment,
                "log_level": _settings.observability.log_level,
            },
        )

        # 3. Create database connection pools (one per configured database)
        logger.info("Creating database connection pools...")
        _pools = await create_pools(_settings.databases)
        logger.info(
            f"Created {len(_pools)} connection pool(s)",
            extra={"databases": sorted(_pools)},
        )

        # 4. Load Schema cache
        logger.info("Initializing schema cache...")
        _schema_cache = SchemaCache(_settings.cache)

        for db_name, pool in _pools.items():
            db_config = _settings.get_database(db_name) or _settings.database
            logger.info(f"Loading schema for database '{db_name}' ({db_config.db_type})...")
            schema = await _schema_cache.load(db_name, pool, db_config.db_type)
            logger.info(
                f"Schema loaded for '{db_name}'",
                extra={
                    "tables": len(schema.tables),
                },
            )

        # Optional: Start schema auto-refresh
        # Disabled by default to avoid unnecessary background tasks
        # Uncomment to enable:
        # if _settings.cache.enabled:
        #     logger.info("Starting schema auto-refresh...")
        #     await _schema_cache.start_auto_refresh(
        #         interval_minutes=60,  # Refresh every hour
        #         pools=_pools,
        #     )

        # 5. Initialize metrics collector
        logger.info("Initializing metrics collector...")
        _metrics = MetricsCollector()

        # Start metrics HTTP server if enabled
        if _settings.observability.metrics_enabled:
            from prometheus_client import start_http_server

            start_http_server(_settings.observability.metrics_port)
            logger.info(f"Metrics server started on port {_settings.observability.metrics_port}")

        # 6. Create service components (per-database, dialect-aware)
        logger.info("Initializing service components...")

        # Shared (fallback) components for PostgreSQL
        sql_generator = SQLGenerator(_settings.openai)

        # SQL Validator (wired to the configured sensitive-resource policy)
        sql_validator = SQLValidator(
            config=_settings.security,
            blocked_tables=_settings.security.blocked_tables,
            blocked_columns=_settings.security.blocked_columns,
            allow_explain=_settings.security.allow_explain,
        )
        logger.info(
            "SQL validator configured",
            extra={
                "blocked_tables": len(_settings.security.blocked_tables),
                "blocked_columns": len(_settings.security.blocked_columns),
                "allow_explain": _settings.security.allow_explain,
            },
        )

        # Per-database components (MySQL databases get MySQL-specific ones)
        sql_executors: dict[str, SQLExecutor] = {}
        sql_generators: dict[str, SQLGenerator] = {}
        sql_validators: dict[str, SQLValidator] = {}
        db_configs: dict[str, DatabaseConfig] = {}
        for db_name, pool in _pools.items():
            db_config = _settings.get_database(db_name) or _settings.database
            db_configs[db_name] = db_config

            if db_config.is_mysql:
                from pg_mcp.services.sql_executor_mysql import MySQLExecutor

                sql_executors[db_name] = MySQLExecutor(
                    pool=pool,
                    security_config=_settings.security,
                    db_config=db_config,
                )
                sql_generators[db_name] = SQLGenerator(_settings.openai, dialect="mysql")
                sql_validators[db_name] = SQLValidator(
                    config=_settings.security,
                    blocked_tables=_settings.security.blocked_tables,
                    blocked_columns=_settings.security.blocked_columns,
                    allow_explain=_settings.security.allow_explain,
                    dialect="mysql",
                )
            else:
                sql_executors[db_name] = SQLExecutor(
                    pool=pool,
                    security_config=_settings.security,
                    db_config=db_config,
                )
            logger.info(f"Created {type(sql_executors[db_name]).__name__} for database '{db_name}'")

        # Result Validator
        result_validator = ResultValidator(
            openai_config=_settings.openai,
            validation_config=_settings.validation,
        )

        # 7. Initialize resilience components
        logger.info("Initializing resilience components...")

        # Circuit Breaker for LLM calls (single instance, shared with orchestrator)
        _circuit_breaker = CircuitBreaker(
            failure_threshold=_settings.resilience.circuit_breaker_threshold,
            recovery_timeout=_settings.resilience.circuit_breaker_timeout,
        )

        # Rate Limiter (applied by the orchestrator to LLM and DB operations)
        _rate_limiter = MultiRateLimiter(
            query_limit=_settings.resilience.query_rate_limit,
            llm_limit=_settings.resilience.llm_rate_limit,
        )

        # 8. Create QueryOrchestrator
        logger.info("Creating query orchestrator...")
        _orchestrator = QueryOrchestrator(
            sql_generator=sql_generator,
            sql_validator=sql_validator,
            sql_executor=sql_executors[_settings.database.name],  # Primary/fallback executor
            result_validator=result_validator,
            schema_cache=_schema_cache,
            pools=_pools,
            resilience_config=_settings.resilience,
            validation_config=_settings.validation,
            sql_executors=sql_executors,
            sql_generators=sql_generators,
            sql_validators=sql_validators,
            db_configs=db_configs,
            rate_limiter=_rate_limiter,
            metrics=_metrics,
            circuit_breaker=_circuit_breaker,
        )

        logger.info("PostgreSQL MCP Server initialization complete!")
        logger.info(
            "Server ready to accept requests",
            extra={
                "databases": list(_pools.keys()),
                "cache_enabled": _settings.cache.enabled,
                "metrics_enabled": _settings.observability.metrics_enabled,
            },
        )

        # Yield to run the server
        yield

    finally:
        # Shutdown sequence
        logger.info("Starting PostgreSQL MCP Server shutdown...")

        # Stop schema auto-refresh with timeout
        if _schema_cache is not None:
            try:
                import asyncio

                await asyncio.wait_for(_schema_cache.stop_auto_refresh(), timeout=3.0)
                logger.info("Schema auto-refresh stopped")
            except TimeoutError:
                logger.warning("Schema auto-refresh stop timed out")
            except Exception as e:
                logger.warning(f"Error stopping schema auto-refresh: {e!s}")

        # Close database connection pools with timeout
        if _pools is not None:
            try:
                # Use 5 second timeout for graceful shutdown
                await close_pools(_pools, timeout=5.0)
                logger.info("Database connection pools closed")
            except Exception as e:
                logger.error(f"Error closing connection pools: {e!s}")

        logger.info("PostgreSQL MCP Server shutdown complete")


# Create FastMCP server instance with lifespan
mcp = FastMCP("pg-mcp", lifespan=lifespan)


@mcp.tool()
async def query(
    question: str,
    database: str | None = None,
    return_type: str = "result",
) -> dict[str, Any]:
    """Execute a natural language query against PostgreSQL database.

    This tool converts natural language questions into SQL queries and executes
    them against the specified PostgreSQL database. It includes comprehensive
    security validation, result verification, and error handling.

    Args:
        question: Natural language description of the query.
            Examples:
                - "How many users registered in the last 30 days?"
                - "Show me the top 10 products by revenue"
                - "What is the average order value by country?"

        database: Target database name (optional if only one database is configured).
            If not specified and only one database is available, it will be
            automatically selected.

        return_type: Type of result to return.
            Options:
                - "sql": Return only the generated SQL query without executing it
                - "result": Execute the query and return results (default)

    Returns:
        dict: Query response containing:
            - success (bool): Whether the query succeeded
            - generated_sql (str): The generated SQL query
            - data (dict): Query results if executed (columns, rows, row_count, etc.)
            - error (dict): Error information if query failed
            - confidence (int): Confidence score (0-100) for result quality
            - tokens_used (int): Number of LLM tokens consumed

    Examples:
        >>> # Get query results
        >>> result = await query(
        ...     question="How many active users are there?",
        ...     return_type="result"
        ... )
        >>> print(result["data"]["rows"])

        >>> # Get SQL only
        >>> result = await query(
        ...     question="Count all products",
        ...     return_type="sql"
        ... )
        >>> print(result["generated_sql"])

    Raises:
        This function does not raise exceptions. All errors are captured and
        returned in the response with success=False and error details.

    Security:
        - Only SELECT queries are allowed (no INSERT, UPDATE, DELETE, DROP, etc.)
        - Dangerous PostgreSQL functions are blocked (pg_sleep, file operations, etc.)
        - Query execution timeout is enforced
        - Row count limits prevent memory exhaustion
        - All queries run in read-only transactions
    """
    global _orchestrator

    if _orchestrator is None:
        return {
            "success": False,
            "error": {
                "code": "SERVER_NOT_INITIALIZED",
                "message": "Server not initialized properly",
                "details": None,
            },
        }

    # Validate return_type
    if return_type not in ("sql", "result"):
        return {
            "success": False,
            "error": {
                "code": "INVALID_PARAMETER",
                "message": f"Invalid return_type: '{return_type}'. Must be 'sql' or 'result'.",
                "details": {"return_type": return_type},
            },
        }

    # Build request
    try:
        request = QueryRequest(
            question=question,
            database=database,
            return_type=ReturnType(return_type),
        )
    except Exception as e:
        return {
            "success": False,
            "error": {
                "code": "INVALID_REQUEST",
                "message": f"Invalid request parameters: {e!s}",
                "details": {"error": str(e)},
            },
        }

    # Execute query through orchestrator
    try:
        response: QueryResponse = await _orchestrator.execute_query(request)
        result = response.to_dict()
        # Ensure tokens_used is always present
        if "tokens_used" not in result:
            result["tokens_used"] = 0
        return result
    except Exception as e:
        logger.exception("Unexpected error in query tool")
        return {
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": f"Internal server error: {e!s}",
                "details": {"error_type": type(e).__name__},
            },
            "tokens_used": 0,
        }


@mcp.tool()
async def health_check() -> dict[str, Any]:
    """Report server health and the status of its components.

    Returns:
        dict: Health information including:
            - status (str): "healthy", "degraded" or "uninitialized"
            - databases (list): Configured database names
            - circuit_breaker (dict): Circuit breaker state and counters
            - rate_limiter (dict): Concurrency statistics for LLM/DB operations
            - environment / metrics_enabled (str / bool): Runtime information

    Examples:
        >>> await health_check()
        {'status': 'healthy', 'databases': ['mydb'], ...}
    """
    if _orchestrator is None:
        return {
            "status": "uninitialized",
            "error": "Server not initialized properly",
        }

    health: dict[str, Any] = await _orchestrator.health()
    health["environment"] = _settings.environment if _settings else None
    health["metrics_enabled"] = _settings.observability.metrics_enabled if _settings else False
    return health


# ---------------------------------------------------------------------------
# Built-in web console (served when running with MCP_TRANSPORT=http)
# ---------------------------------------------------------------------------

_WEB_CONSOLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>pg-mcp // query console</title>
<link rel="icon" href="data:,">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap"
      rel="stylesheet">
<style>
:root{
  --bg:#0a0d0b; --line:#1d2820; --txt:#cfe0d4; --dim:#7d8f83;
  --acc:#57ff9a; --amber:#ffc46b; --err:#ff7a7a;
  --mono:'IBM Plex Mono',ui-monospace,monospace; --disp:'Chakra Petch',var(--mono);
}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{
  color:var(--txt);font:14px/1.65 var(--mono);display:flex;flex-direction:column;
  background:
    radial-gradient(1100px 520px at 85% -10%, #10331f4d, transparent),
    radial-gradient(800px 400px at -10% 110%, #0d231866, transparent),
    linear-gradient(#0c110d 1px, transparent 1px),
    linear-gradient(90deg,#0c110d 1px,transparent 1px),
    var(--bg);
  background-size:auto,auto,28px 28px,28px 28px,auto;
}
.scan{pointer-events:none;position:fixed;inset:0;z-index:9;
  background:repeating-linear-gradient(0deg,transparent 0 2px,#00000012 2px 3px)}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;
  padding:14px 22px;border-bottom:1px solid var(--line);font-family:var(--disp)}
.brand{font-size:15px;letter-spacing:.06em;text-transform:uppercase}
.brand .logo{color:var(--acc)}
.brand em{color:var(--dim);font-style:normal;font-size:12px;letter-spacing:.12em}
.status{display:flex;align-items:center;gap:8px;font:12px var(--mono);color:var(--dim)}
.led{width:8px;height:8px;border-radius:50%;background:#4a554d}
.led.ok{background:var(--acc);box-shadow:0 0 10px #57ff9a99;animation:pulse 2s infinite}
.led.bad{background:var(--err);box-shadow:0 0 10px #ff7a7a99}
@keyframes pulse{50%{opacity:.55}}
main{flex:1;overflow-y:auto;padding:26px 22px;display:flex;flex-direction:column;
  gap:16px;max-width:940px;width:100%;margin:0 auto}
.msg{border:1px solid var(--line);padding:14px 16px;animation:in .35s both;
  background:linear-gradient(180deg,#10161180,#0c110dd9)}
.msg.user{border-left:3px solid var(--acc)}
.msg.boot{border-style:dashed;color:var(--dim)}
.dim{color:var(--dim)} .errtx{color:var(--err)}
.tag{font:600 11px var(--disp);letter-spacing:.14em;padding:2px 8px}
.tag.ok{color:#04140b;background:var(--acc)}
.tag.bad{color:#140404;background:var(--err)}
.meta{color:var(--dim);font-size:12px}
pre.sql{margin-top:10px;padding:12px 14px;background:#0d1410;border:1px solid #16241b;
  border-left:3px solid var(--acc);color:var(--acc);overflow-x:auto;font-size:13px}
.tw{margin-top:10px;overflow-x:auto}
table{border-collapse:collapse;font-size:12.5px;min-width:50%}
th{color:var(--amber);text-align:left;border-bottom:1px solid var(--line);padding:5px 14px 5px 0}
td{border-bottom:1px dashed #17221b;padding:4px 14px 4px 0;white-space:nowrap}
.chips{margin-top:10px;display:flex;flex-wrap:wrap;gap:8px}
.chip{background:transparent;border:1px solid var(--line);color:var(--acc);
  font:12px var(--mono);padding:5px 12px;cursor:pointer;transition:.15s}
.chip:hover{border-color:var(--acc);box-shadow:0 0 0 1px #57ff9a33}
.cursor::after{content:'▊';color:var(--acc);animation:pulse 1s infinite}
footer{border-top:1px solid var(--line);padding:16px 22px}
.composer{display:flex;gap:10px;max-width:940px;margin:0 auto;width:100%}
.sym{color:var(--acc);align-self:center;font-weight:600}
#q{flex:1;background:#0d120e;border:1px solid var(--line);color:var(--txt);
  font:inherit;padding:11px 14px;outline:0;transition:.15s}
#q:focus{border-color:var(--acc);box-shadow:0 0 0 1px #57ff9a2b}
#send{background:var(--acc);color:#04140b;border:0;font:600 13px var(--disp);
  letter-spacing:.08em;padding:0 24px;cursor:pointer}
#send:hover{filter:brightness(1.1)}
@keyframes in{from{opacity:0;transform:translateY(8px)}}
</style>
</head>
<body>
<div class="scan"></div>
<header>
  <div class="brand"><span class="logo">◤</span> pg-mcp
    <em>// natural-language SQL console</em></div>
  <div class="status"><i class="led" id="led"></i><span id="stx">checking…</span></div>
</header>
<main id="feed"></main>
<footer>
  <div class="composer">
    <span class="sym">mysql&gt;</span>
    <input id="q" placeholder="Ask in plain language…" autocomplete="off" autofocus>
    <button id="send">RUN ↵</button>
  </div>
</footer>
<script>
const feed=document.getElementById('feed');
const esc=s=>String(s).replace(/[&<>"']/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function add(html,cls){const d=document.createElement('div');d.className='msg '+(cls||'');
  d.innerHTML=html;feed.appendChild(d);feed.scrollTop=feed.scrollHeight;return d;}
const sqlBlock=s=>'<pre class="sql">'+esc(s)+'</pre>';
function render(box,j){
  const e=j.error||{};
  if(!j.success){
    box.innerHTML='<span class="tag bad">ERROR</span> <b>'+esc(e.code||'')+'</b>'+
      '<p class="errtx" style="margin-top:6px">'+esc(e.message||'unknown error')+'</p>'+
      ((j.generated_sql||j.sql)?sqlBlock(j.generated_sql||j.sql):'');
    return;
  }
  const d=j.data||{},rows=d.rows||[],cols=d.columns||(rows[0]?Object.keys(rows[0]):[]);
  let h='<span class="tag ok">OK</span> <span class="meta">confidence '+
    (j.confidence==null?'—':j.confidence+'%')+' · '+(d.row_count??0)+' rows · '+
    (d.execution_time_ms??0)+'ms · '+(j.tokens_used??0)+' tokens</span>';
  if(j.generated_sql||j.sql)h+=sqlBlock(j.generated_sql||j.sql);
  if(rows.length){
    h+='<div class="tw"><table><thead><tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+
      '</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+esc(r[c]??'NULL')+
      '</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
  }else h+='<p class="dim" style="margin-top:8px">no rows returned</p>';
  box.innerHTML=h;
}
async function ask(text){
  text=text.trim();if(!text)return;
  add('<b>'+esc(text)+'</b>','user');
  const box=add('<span class="dim cursor">running </span>','assistant');
  try{
    const r=await fetch('/api/query',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:text})});
    render(box,await r.json());
  }catch(e){box.innerHTML='<span class="errtx">network error: '+esc(e)+'</span>';}
}
const q=document.getElementById('q');
document.getElementById('send').onclick=()=>{ask(q.value);q.value='';q.focus();};
q.addEventListener('keydown',e=>{if(e.key==='Enter'){ask(q.value);q.value='';}});
document.addEventListener('click',e=>{
  if(e.target.classList.contains('chip'))ask(e.target.textContent);});
const boot=add('I turn plain language into <b>read-only MySQL</b> and run it '+
  'against <b>todo_db</b>. Try one of these:','assistant boot');
const chips=document.createElement('div');chips.className='chips';
chips.innerHTML='<button class="chip">How many todos are there?</button>'+
  '<button class="chip">List the titles of all undone todos with their owner email</button>'+
  '<button class="chip">Count users by status</button>';
boot.appendChild(chips);
fetch('/api/health').then(r=>r.json()).then(j=>{
  const ok=j.status==='healthy'||!j.error;
  document.getElementById('led').className='led '+(ok?'ok':'bad');
  document.getElementById('stx').textContent=ok?'todo_db connected':'unhealthy';
}).catch(()=>{document.getElementById('led').className='led bad';
  document.getElementById('stx').textContent='offline';});
</script>
</body>
</html>
"""


@mcp.custom_route("/", methods=["GET"])
async def web_console(request: Request) -> HTMLResponse:
    """Serve the built-in web query console (HTTP transport only)."""
    return HTMLResponse(_WEB_CONSOLE_HTML)


@mcp.custom_route("/api/query", methods=["POST"])
async def api_query(request: Request) -> JSONResponse:
    """Run a natural-language query on behalf of the web console."""
    if _orchestrator is None:
        return JSONResponse(
            {
                "success": False,
                "error": {
                    "code": "SERVER_NOT_INITIALIZED",
                    "message": "Server not initialized properly",
                    "details": None,
                },
            },
            status_code=503,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {
                "success": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Request body must be valid JSON",
                    "details": None,
                },
            },
            status_code=400,
        )
    question = str(body.get("question", "")).strip() if isinstance(body, dict) else ""
    if not question:
        return JSONResponse(
            {
                "success": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "'question' is required",
                    "details": None,
                },
            },
            status_code=400,
        )
    result = await query(
        question=question,
        database=body.get("database") if isinstance(body, dict) else None,
        return_type="result",
    )
    return JSONResponse(result)


@mcp.custom_route("/api/health", methods=["GET"])
async def api_health(request: Request) -> JSONResponse:
    """Health snapshot for the web console status indicator."""
    return JSONResponse(await health_check())


if __name__ == "__main__":
    """Run the server when executed directly.

    Environment variables:
        MCP_TRANSPORT: 'stdio' (default) for local MCP clients, or 'http' to
            expose /mcp for remote clients plus the built-in web console at /.
        MCP_HOST / MCP_PORT: bind address for HTTP transport
            (defaults: 0.0.0.0 / 8000).
    """
    # Re-load .env with precedence over inherited OS environment variables so
    # that stale machine-level values (e.g. an invalid OPENAI_API_KEY) do not
    # shadow the project configuration right before Settings() is built.
    from dotenv import load_dotenv

    load_dotenv(override=True)

    import os

    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "http":
        mcp.run(
            transport="http",
            host=os.environ.get("MCP_HOST", "0.0.0.0"),  # noqa: S104
            port=int(os.environ.get("MCP_PORT", "8000")),
        )
    else:
        import anyio

        anyio.run(mcp.run_async)
