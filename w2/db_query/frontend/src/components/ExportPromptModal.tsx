/** Proactive export prompt shown after a successful query. */

import React, { useState } from "react";
import { Button, Checkbox, Modal, Space, Typography } from "antd";
import { FileTextOutlined } from "@ant-design/icons";
import { ExportFormat } from "../types/export";

const { Text } = Typography;

interface ExportPromptModalProps {
  open: boolean;
  rowCount: number;
  onExport: (format: ExportFormat) => void;
  onDismiss: (dontAskAgain: boolean) => void;
}

export const ExportPromptModal: React.FC<ExportPromptModalProps> = ({
  open,
  rowCount,
  onExport,
  onDismiss,
}) => {
  const [dontAskAgain, setDontAskAgain] = useState(false);

  return (
    <Modal
      open={open}
      title="Export query result?"
      onCancel={() => onDismiss(dontAskAgain)}
      footer={
        <Space direction="vertical" style={{ width: "100%" }} size="small">
          <Space style={{ display: "flex", justifyContent: "flex-end" }}>
            <Button
              type="primary"
              icon={<FileTextOutlined />}
              onClick={() => onExport("csv")}
            >
              Export CSV
            </Button>
            <Button icon={<FileTextOutlined />} onClick={() => onExport("json")}>
              Export JSON
            </Button>
            <Button type="text" onClick={() => onDismiss(dontAskAgain)}>
              Not now
            </Button>
          </Space>
          <Checkbox
            checked={dontAskAgain}
            onChange={(e) => setDontAskAgain(e.target.checked)}
            style={{ marginLeft: "auto", display: "table" }}
          >
            Don't ask again this session
          </Checkbox>
        </Space>
      }
    >
      <Text>
        The query returned <Text strong>{rowCount}</Text> rows. Export the result
        as a CSV or JSON file?
      </Text>
    </Modal>
  );
};
