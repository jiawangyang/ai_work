/** Export dropdown button for the current query result. */

import React, { useState } from "react";
import { App, Button, Dropdown } from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import type { MenuProps } from "antd";
import { ExportFormat } from "../types/export";
import { exportErrorMessage, exportQueryResult } from "../services/export";

interface ExportButtonProps {
  databaseName: string;
  sql?: string;
  disabled?: boolean;
}

export const ExportButton: React.FC<ExportButtonProps> = ({
  databaseName,
  sql,
  disabled = false,
}) => {
  const { message } = App.useApp();
  const [loading, setLoading] = useState(false);

  const handleExport = async (format: ExportFormat) => {
    setLoading(true);
    try {
      const { filename, rowCount } = await exportQueryResult(databaseName, {
        sql,
        format,
      });
      message.success(`Exported ${rowCount} rows to ${filename}`);
    } catch (error) {
      message.error(await exportErrorMessage(error));
    } finally {
      setLoading(false);
    }
  };

  const items: MenuProps["items"] = [
    { key: "csv", label: "Export as CSV" },
    { key: "json", label: "Export as JSON" },
  ];

  return (
    <Dropdown
      menu={{ items, onClick: ({ key }) => handleExport(key as ExportFormat) }}
      disabled={disabled}
    >
      <Button icon={<DownloadOutlined />} loading={loading} disabled={disabled}>
        Export
      </Button>
    </Dropdown>
  );
};
