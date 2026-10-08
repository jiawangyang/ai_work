/** Export helpers: call the export API and trigger a browser download. */

import { apiClient } from "./api";
import { ExportFormat, ExportInput } from "../types/export";

export interface ExportOutcome {
  filename: string;
  rowCount: number;
}

function fallbackFilename(databaseName: string, format: ExportFormat): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const ts =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `query_${databaseName}_${ts}.${format}`;
}

function filenameFromDisposition(disposition: string | undefined): string | null {
  if (!disposition) return null;
  const match = disposition.match(/filename="?([^";]+)"?/i);
  return match?.[1] ?? null;
}

/** Export a query result server-side and download it in the browser. */
export async function exportQueryResult(
  databaseName: string,
  input: ExportInput
): Promise<ExportOutcome> {
  const response = await apiClient.post(
    `/api/v1/dbs/${databaseName}/export`,
    input,
    { responseType: "blob" }
  );

  const filename =
    filenameFromDisposition(response.headers?.["content-disposition"]) ||
    fallbackFilename(databaseName, input.format);
  const rowCount = Number(response.headers?.["x-row-count"] ?? 0);

  const url = URL.createObjectURL(
    new Blob([response.data], {
      type:
        input.format === "csv" ? "text/csv;charset=utf-8" : "application/json",
    })
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);

  return { filename, rowCount };
}

/** Extract a human-readable message from an export error (blob or JSON body). */
export async function exportErrorMessage(error: unknown): Promise<string> {
  const err = error as {
    response?: { data?: Blob | { detail?: string } };
    message?: string;
  };
  const data = err.response?.data;
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text());
      return parsed.detail || "Export failed";
    } catch {
      return "Export failed";
    }
  }
  return (data as { detail?: string } | undefined)?.detail || err.message || "Export failed";
}
