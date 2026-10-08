/** Export feature types. */

export type ExportFormat = "csv" | "json";

export interface ExportInput {
  sql?: string;
  format: ExportFormat;
  filename?: string;
}
