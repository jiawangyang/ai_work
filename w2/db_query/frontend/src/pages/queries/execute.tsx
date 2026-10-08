/** Query execution page with SQL editor and result table. */

import React, { useState, useEffect } from "react";
import { useParams } from "react-router-dom";
import { App, Card, Button, Space, Spin, Alert, List, Typography } from "antd";
import { PlayCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { apiClient } from "../../services/api";
import { exportErrorMessage, exportQueryResult } from "../../services/export";
import { QueryResult, QueryHistoryEntry, QueryInput } from "../../types/query";
import { ExportFormat } from "../../types/export";
import { SqlEditor } from "../../components/SqlEditor";
import { ResultTable } from "../../components/ResultTable";
import { ExportButton } from "../../components/ExportButton";
import { ExportPromptModal } from "../../components/ExportPromptModal";

const { Text } = Typography;

const EXPORT_PROMPT_DISMISSED_KEY = "dbq.exportPromptDismissed";

export const QueryExecute: React.FC = () => {
  const { message } = App.useApp();
  const { databaseName } = useParams<{ databaseName: string }>();
  const [sql, setSql] = useState("SELECT * FROM ");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<QueryHistoryEntry[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [exportPromptOpen, setExportPromptOpen] = useState(false);

  useEffect(() => {
    if (databaseName) {
      loadHistory();
    }
  }, [databaseName]);

  const loadHistory = async () => {
    if (!databaseName) return;

    setLoadingHistory(true);
    try {
      const response = await apiClient.get<QueryHistoryEntry[]>(
        `/api/v1/dbs/${databaseName}/history`
      );
      setHistory(response.data);
    } catch (err) {
      console.error("Failed to load history:", err);
    } finally {
      setLoadingHistory(false);
    }
  };

  const handleExecute = async () => {
    if (!databaseName || !sql.trim()) {
      setError("Please enter a SQL query");
      return;
    }

    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const input: QueryInput = { sql: sql.trim() };
      const response = await apiClient.post<QueryResult>(
        `/api/v1/dbs/${databaseName}/query`,
        input
      );
      setResult(response.data);
      // Reload history after successful query
      await loadHistory();
      // Proactively offer export when there are rows (unless dismissed this session)
      if (
        response.data.rowCount > 0 &&
        sessionStorage.getItem(EXPORT_PROMPT_DISMISSED_KEY) !== "1"
      ) {
        setExportPromptOpen(true);
      }
    } catch (err: any) {
      const errorMessage =
        err.response?.data?.detail || err.message || "Query execution failed";
      setError(errorMessage);
    } finally {
      setLoading(false);
    }
  };

  const handleHistoryClick = (historyItem: QueryHistoryEntry) => {
    setSql(historyItem.sqlText);
    setError(null);
    setResult(null);
  };

  const handleExport = async (format: ExportFormat) => {
    if (!databaseName || !result?.sql) return;
    try {
      const { filename, rowCount } = await exportQueryResult(databaseName, {
        sql: result.sql,
        format,
      });
      message.success(`Exported ${rowCount} rows to ${filename}`);
    } catch (err) {
      message.error(await exportErrorMessage(err));
    }
  };

  const handleDismissExportPrompt = (dontAskAgain: boolean) => {
    setExportPromptOpen(false);
    if (dontAskAgain) {
      sessionStorage.setItem(EXPORT_PROMPT_DISMISSED_KEY, "1");
    }
  };

  return (
    <div style={{ padding: 24 }}>
      <Card
        title={`Execute Query - ${databaseName}`}
        extra={
          <Space>
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              onClick={handleExecute}
              loading={loading}
            >
              Execute
            </Button>
            <Button
              icon={<ReloadOutlined />}
              onClick={loadHistory}
              loading={loadingHistory}
            >
              Refresh History
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" style={{ width: "100%" }} size="large">
          <div>
            <Card title="SQL Editor" size="small">
              <SqlEditor value={sql} onChange={(val) => setSql(val || "")} height="200px" />
            </Card>
          </div>

          {error && (
            <Alert
              message="Error"
              description={error}
              type="error"
              showIcon
              closable
              onClose={() => setError(null)}
            />
          )}

          {loading && (
            <div style={{ textAlign: "center", padding: "50px" }}>
              <Spin size="large" />
            </div>
          )}

          {result && (
            <Card
              title="Query Results"
              size="small"
              extra={
                <ExportButton
                  databaseName={databaseName ?? ""}
                  sql={result.sql}
                  disabled={result.rowCount === 0}
                />
              }
            >
              <ResultTable result={result} loading={loading} />
            </Card>
          )}
        </Space>
      </Card>

      <Card title="Query History" style={{ marginTop: 16 }}>
        {loadingHistory ? (
          <Spin />
        ) : (
          <List
            dataSource={history}
            renderItem={(item) => (
              <List.Item
                style={{
                  cursor: "pointer",
                  backgroundColor: item.success ? "transparent" : "#fff2f0",
                }}
                onClick={() => handleHistoryClick(item)}
              >
                <List.Item.Meta
                  title={
                    <Space>
                      <Text
                        code
                        style={{
                          maxWidth: "600px",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                          display: "inline-block",
                        }}
                      >
                        {item.sqlText}
                      </Text>
                      {item.success ? (
                        <Text type="success">
                          ✓ {item.rowCount} rows in {item.executionTimeMs}ms
                        </Text>
                      ) : (
                        <Text type="danger">✗ Failed</Text>
                      )}
                    </Space>
                  }
                  description={
                    <Text type="secondary">
                      {new Date(item.executedAt).toLocaleString()}
                    </Text>
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Card>

      <ExportPromptModal
        open={exportPromptOpen}
        rowCount={result?.rowCount ?? 0}
        onExport={(format: ExportFormat) => {
          setExportPromptOpen(false);
          handleExport(format);
        }}
        onDismiss={handleDismissExportPrompt}
      />
    </div>
  );
};
