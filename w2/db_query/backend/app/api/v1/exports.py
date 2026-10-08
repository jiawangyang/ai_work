"""Data export API endpoints."""

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel import Session, col, desc, select

from app.database import get_session
from app.models.database import DatabaseConnection
from app.models.query import QueryHistory, QuerySource
from app.models.schemas import ExportInput, QueryResult
from app.services.exporter import export_to_csv, export_to_json
from app.services.query_wrapper import execute_query_with_service
from app.services.sql_validator import SqlValidationError

router = APIRouter(prefix="/api/v1/dbs", tags=["exports"])

_FILENAME_INVALID_CHARS = re.compile(r"[^\w\-.]+")
_MEDIA_TYPES = {"csv": "text/csv", "json": "application/json"}


def _sanitize_filename(name: str) -> str:
    """Replace characters unsafe for filenames or header injection."""
    cleaned = _FILENAME_INVALID_CHARS.sub("_", name).strip("._")
    return cleaned or "export"


def _default_filename(database_name: str) -> str:
    """Build the default download filename: query_<db>_<timestamp>."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"query_{_sanitize_filename(database_name)}_{timestamp}"


def _resolve_sql(session: Session, database_name: str, sql: str | None) -> str:
    """Return the explicit SQL, or replay the latest successful query from history."""
    if sql:
        return sql

    statement = (
        select(QueryHistory)
        .where(QueryHistory.database_name == database_name)
        .where(col(QueryHistory.success).is_(True))
        .order_by(desc(QueryHistory.executed_at))
        .limit(1)
    )
    latest = session.exec(statement).first()
    if not latest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No successful query found for database '{database_name}'. "
                "Provide 'sql' in the request body."
            ),
        )
    return latest.sql_text


@router.post("/{name}/export")
async def export_query_result(
    name: str,
    input_data: ExportInput,
    session: Session = Depends(get_session),
) -> Response:
    """
    Execute a query and export the result as a downloadable CSV or JSON file.

    Set ``sql`` explicitly, or omit it to replay the latest successful query
    for this database. Responds with a file attachment and includes
    ``X-Row-Count`` / ``X-Execution-Time-Ms`` headers.
    """
    statement = select(DatabaseConnection).where(DatabaseConnection.name == name)
    connection = session.exec(statement).first()
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database connection '{name}' not found",
        )

    sql = _resolve_sql(session, name, input_data.sql)

    try:
        result: QueryResult = await execute_query_with_service(
            session,
            name,
            connection.db_type,
            connection.url,
            sql,
            QuerySource.MANUAL,
        )
    except SqlValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query execution failed: {str(e)}",
        )

    base_name = (
        _sanitize_filename(input_data.filename)
        if input_data.filename
        else _default_filename(name)
    )
    filename = f"{base_name}.{input_data.format}"

    if input_data.format == "csv":
        content = export_to_csv(result.columns, result.rows)
    else:
        content = export_to_json(result.columns, result.rows)

    return Response(
        content=content.encode("utf-8"),
        media_type=_MEDIA_TYPES[input_data.format],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Row-Count": str(result.row_count),
            "X-Execution-Time-Ms": str(result.execution_time_ms),
        },
    )
