import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { getModelStatus } from "./api";
import type { ModelStatus } from "./types";

// Shared model state: the header menu edits it, and pages read it to stop a
// submit when no model can run and to open the menu from an error.
type ModelContextValue = {
  status: ModelStatus | null;
  loadError: string | null;
  setStatus: (status: ModelStatus) => void;
  refresh: () => Promise<void>;
  panelOpen: boolean;
  setPanelOpen: (open: boolean | ((open: boolean) => boolean)) => void;
};

const ModelContext = createContext<ModelContextValue | null>(null);

export function modelReady(status: ModelStatus | null): boolean {
  if (!status) return false;
  if (status.provider === "chatgpt") return status.chatgpt.signed_in;
  if (status.provider === "claude-code") return status.claude.available && status.claude.signed_in;
  return status.api.configured;
}

export function ModelProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await getModelStatus());
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <ModelContext.Provider value={{ status, loadError, setStatus, refresh, panelOpen, setPanelOpen }}>
      {children}
    </ModelContext.Provider>
  );
}

export function useModel(): ModelContextValue {
  const value = useContext(ModelContext);
  if (!value) throw new Error("useModel must be used inside ModelProvider");
  return value;
}
