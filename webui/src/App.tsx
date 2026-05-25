import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { DeleteConfirm } from "@/components/DeleteConfirm";
import { RenameChatDialog } from "@/components/RenameChatDialog";
import { Sidebar } from "@/components/Sidebar";
import { SessionSearchDialog } from "@/components/SessionSearchDialog";
import { SettingsView } from "@/components/settings/SettingsView";
import { ThreadShell } from "@/components/thread/ThreadShell";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";

import { useSessions } from "@/hooks/useSessions";
import { useDeferredTitleRefresh } from "@/hooks/useDeferredTitleRefresh";
import { useSidebarState } from "@/hooks/useSidebarState";
import { ThemeProvider, useTheme } from "@/hooks/useTheme";
import { cn } from "@/lib/utils";
import {
  checkBootstrapWithoutAuth,
  clearAuth,
  fetchBootstrap,
  fetchBootstrapWithToken,
  getToken,
  getUser,
  setAuth,
  setupAdmin,
} from "@/lib/auth";
import { onApiUnauthorized } from "@/lib/api";
import { deriveWsUrl } from "@/lib/bootstrap";
import { deriveTitle } from "@/lib/format";
import { NanobotClient } from "@/lib/nanobot-client";
import { ClientProvider, useClient } from "@/providers/ClientProvider";
import type { BootPayload, ChatSummary } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type BootState =
  | { status: "loading" }
  | { status: "setup" }
  | { status: "error"; message: string }
  | { status: "auth"; failed?: boolean }
  | {
      status: "ready";
      client: NanobotClient;
      token: string;
      tokenExpiresAt: number;
      modelName: string | null;
    };

const SIDEBAR_STORAGE_KEY = "nanobot-webui.sidebar";
const COMPLETED_RUNS_STORAGE_KEY = "nanobot-webui.sidebar.completed-runs.v1";
const RESTART_STARTED_KEY = "nanobot-webui.restartStartedAt";
const SIDEBAR_WIDTH = 272;
const SIDEBAR_RAIL_WIDTH = 56;
const TOKEN_REFRESH_MARGIN_MS = 30_000;
const TOKEN_REFRESH_MIN_DELAY_MS = 5_000;
type ShellView = "chat" | "settings";

function bootstrapTokenExpiresAt(expiresInSeconds: number): number {
  return Date.now() + Math.max(0, expiresInSeconds) * 1000;
}

function tokenRefreshDelayMs(expiresAt: number): number {
  const remaining = Math.max(0, expiresAt - Date.now());
  const margin = Math.min(
    TOKEN_REFRESH_MARGIN_MS,
    Math.max(1_000, remaining / 2),
  );
  return Math.max(TOKEN_REFRESH_MIN_DELAY_MS, remaining - margin);
}

function AuthForm({
  mode,
  failed,
  onSubmit,
}: {
  mode: "login" | "setup";
  failed: boolean;
  onSubmit: (username: string, password: string) => void;
}) {
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [setupError, setSetupError] = useState("");

  const isSetup = mode === "setup";

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const u = username.trim();
    const p = password.trim();
    if (!u || !p) return;
    if (isSetup) {
      if (p !== confirmPassword.trim()) {
        setSetupError(t("app.auth.passwordMismatch"));
        return;
      }
      if (p.length < 6) {
        setSetupError(t("app.auth.passwordTooShort"));
        return;
      }
    }
    setSetupError("");
    setSubmitting(true);
    onSubmit(u, p);
  };

  return (
    <div className="flex h-full w-full items-center justify-center px-6">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4"
      >
        <div className="flex flex-col items-center gap-1 text-center">
          <p className="text-lg font-semibold">
            {isSetup ? t("app.auth.setupTitle") : t("app.auth.title")}
          </p>
          <p className="text-sm text-muted-foreground">
            {isSetup ? t("app.auth.setupHint") : t("app.auth.hint")}
          </p>
        </div>
        {failed && !isSetup && (
          <p className="text-center text-sm text-destructive">
            {t("app.auth.invalid")}
          </p>
        )}
        {setupError && (
          <p className="text-center text-sm text-destructive">{setupError}</p>
        )}
        <div className="flex flex-col gap-2">
          <label htmlFor="auth-username" className="text-sm font-medium">
            {t("app.auth.username")}
          </label>
          <Input
            id="auth-username"
            type="text"
            placeholder={t("app.auth.usernamePlaceholder")}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={submitting}
            autoFocus
            autoComplete="username"
          />
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="auth-password" className="text-sm font-medium">
            {t("app.auth.password")}
          </label>
          <Input
            id="auth-password"
            type="password"
            placeholder={t("app.auth.passwordPlaceholder")}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={submitting}
            autoComplete={isSetup ? "new-password" : "current-password"}
          />
        </div>
        {isSetup && (
          <div className="flex flex-col gap-2">
            <label htmlFor="auth-confirm" className="text-sm font-medium">
              {t("app.auth.confirmPassword")}
            </label>
            <Input
              id="auth-confirm"
              type="password"
              placeholder={t("app.auth.confirmPasswordPlaceholder")}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              disabled={submitting}
              autoComplete="new-password"
            />
          </div>
        )}
        <Button
          type="submit"
          className="w-full"
          disabled={!username.trim() || !password.trim() || submitting || (isSetup && !confirmPassword.trim())}
        >
          {isSetup ? t("app.auth.createAdmin") : t("app.auth.submit")}
        </Button>
      </form>
    </div>
  );
}

function readSidebarOpen(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const raw = window.localStorage.getItem(SIDEBAR_STORAGE_KEY);
    if (raw === null) return true;
    return raw === "1";
  } catch {
    return true;
  }
}

function readCompletedRunChatIds(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(COMPLETED_RUNS_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((item): item is string => typeof item === "string"));
  } catch {
    return new Set();
  }
}

function writeCompletedRunChatIds(chatIds: Set<string>): void {
  try {
    window.localStorage.setItem(
      COMPLETED_RUNS_STORAGE_KEY,
      JSON.stringify(Array.from(chatIds)),
    );
  } catch {
    // ignore storage errors (private mode, etc.)
  }
}

export default function App() {
  const { t } = useTranslation();
  const [state, setState] = useState<BootState>({ status: "loading" });
  const credentialsRef = useRef<{ username: string; password: string } | null>(null);
  const stateRef = useRef<BootState>({ status: "loading" });
  stateRef.current = state;

  // Global 401 handler: any API call that gets 401 redirects to login.
  useEffect(() => {
    onApiUnauthorized(() => {
      clearAuth();
      credentialsRef.current = null;
      const s = stateRef.current;
      if (s.status === "ready") {
        s.client.close();
      }
      setState({ status: "auth" });
    });
    return () => onApiUnauthorized(null);
  }, []);

  const bootstrapWithCredentials = useCallback(
    (username: string, password: string) => {
      let cancelled = false;
      (async () => {
        setState({ status: "loading" });
        try {
          const boot = await fetchBootstrap(username, password);
          if (cancelled) return;
          credentialsRef.current = { username, password };
          if (boot.token && boot.user) {
            setAuth(boot.token, boot.user);
          }
          const url = deriveWsUrl(boot.ws_path, boot.ws_token);
          let client: NanobotClient;
          client = new NanobotClient({
            url,
            onReauth: async () => {
              try {
                const creds = credentialsRef.current;
                if (!creds) return null;
                const refreshed = await fetchBootstrap(creds.username, creds.password);
                if (refreshed.token && refreshed.user) {
                  setAuth(refreshed.token, refreshed.user);
                }
                const refreshedUrl = deriveWsUrl(refreshed.ws_path, refreshed.ws_token);
                const tokenExpiresAt = bootstrapTokenExpiresAt(refreshed.expires_in);
                setState((current) =>
                  current.status === "ready" && current.client === client
                    ? {
                        ...current,
                        token: refreshed.token ?? current.token,
                        tokenExpiresAt,
                        modelName: refreshed.model_name ?? current.modelName,
                      }
                    : current,
                );
                return refreshedUrl;
              } catch {
                return null;
              }
            },
          });
          client.connect();
          setState({
            status: "ready",
            client,
            token: boot.token ?? "",
            tokenExpiresAt: bootstrapTokenExpiresAt(boot.expires_in),
            modelName: boot.model_name ?? null,
          });
        } catch (e) {
          if (cancelled) return;
          const msg = (e as Error).message;
          if (msg.includes("HTTP 401") || msg.includes("HTTP 403") || msg.includes("Invalid credentials")) {
            setState({ status: "auth", failed: true });
          } else {
            setState({ status: "error", message: msg });
          }
        }
      })();
      return () => {
        cancelled = true;
      };
    },
    [],
  );

  useEffect(() => {
    if (state.status !== "ready") return;
    const client = state.client;
    const timer = window.setTimeout(async () => {
      try {
        let boot: BootPayload;
        const creds = credentialsRef.current;
        if (creds) {
          boot = await fetchBootstrap(creds.username, creds.password);
        } else {
          const storedToken = getToken();
          if (!storedToken) {
            setState({ status: "auth" });
            return;
          }
          boot = await fetchBootstrapWithToken(storedToken);
        }
        if (boot.token && boot.user) {
          setAuth(boot.token, boot.user);
        }
        const url = deriveWsUrl(boot.ws_path, boot.ws_token);
        const tokenExpiresAt = bootstrapTokenExpiresAt(boot.expires_in);
        client.updateUrl(url);
        setState((current) =>
          current.status === "ready" && current.client === client
            ? {
                ...current,
                token: boot.token ?? current.token,
                tokenExpiresAt,
                modelName: boot.model_name ?? current.modelName,
              }
            : current,
        );
      } catch (e) {
        const msg = (e as Error).message;
        if (msg.includes("HTTP 401") || msg.includes("HTTP 403") || msg.includes("Invalid credentials")) {
          clearAuth();
          credentialsRef.current = null;
          setState({ status: "auth", failed: true });
        }
      }
    }, tokenRefreshDelayMs(state.tokenExpiresAt));
    return () => window.clearTimeout(timer);
  }, [state]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { has_users } = await checkBootstrapWithoutAuth();
        if (cancelled) return;
        if (!has_users) {
          setState({ status: "setup" });
          return;
        }
        // Try stored JWT token first (survives page reload).
        const storedToken = getToken();
        if (storedToken) {
          try {
            const boot = await fetchBootstrapWithToken(storedToken);
            if (cancelled) return;
            if (boot.token && boot.user) {
              setAuth(boot.token, boot.user);
            }
            // Note: boot.ws_token is generated fresh by the server —
            // we get a new WS token on every bootstrap call.
            const url = deriveWsUrl(boot.ws_path, boot.ws_token);
            const tokenExpiresAt = bootstrapTokenExpiresAt(boot.expires_in);
            const client = new NanobotClient({
              url,
              onReauth: async () => {
                try {
                  const stored = getToken();
                  if (!stored) return null;
                  const refreshed = await fetchBootstrapWithToken(stored);
                  if (refreshed.token && refreshed.user) {
                    setAuth(refreshed.token, refreshed.user);
                  }
                  return deriveWsUrl(refreshed.ws_path, refreshed.ws_token);
                } catch {
                  return null;
                }
              },
            });
            client.connect();
            setState({
              status: "ready",
              client,
              token: boot.token ?? "",
              tokenExpiresAt,
              modelName: boot.model_name ?? null,
            });
            return;
          } catch {
            // Stored token is invalid/expired — clear and show login.
            clearAuth();
            if (cancelled) return;
            setState({ status: "auth" });
            return;
          }
        }
        // No stored token — try legacy auto-bootstrap (localhost / shared secret).
        try {
          bootstrapWithCredentials("", "");
        } catch {
          if (cancelled) return;
          setState({ status: "auth" });
        }
      } catch {
        if (cancelled) return;
        setState({ status: "auth" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [bootstrapWithCredentials]);

  const handleSetup = useCallback(
    async (username: string, password: string) => {
      try {
        await setupAdmin(username, password);
      } catch (e) {
        setState({ status: "error", message: (e as Error).message });
        return;
      }
      bootstrapWithCredentials(username, password);
    },
    [bootstrapWithCredentials],
  );

  const handleModelNameChange = useCallback((modelName: string | null) => {
    setState((current) =>
      current.status === "ready" ? { ...current, modelName } : current,
    );
  }, []);

  const handleLogout = useCallback(() => {
    setState((current) => {
      if (current.status === "ready") {
        current.client.close();
      }
      return { status: "auth" as const };
    });
    clearAuth();
    credentialsRef.current = null;
  }, []);

  if (state.status === "loading") {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="flex flex-col items-center gap-3 animate-in fade-in-0 duration-300">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-foreground/40" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-foreground/60" />
            </span>
            {t("app.loading.connecting")}
          </div>
        </div>
      </div>
    );
  }

  if (state.status === "setup") {
    return (
      <AuthForm
        mode="setup"
        failed={false}
        onSubmit={handleSetup}
      />
    );
  }
  if (state.status === "auth") {
    return (
      <AuthForm
        mode="login"
        failed={!!state.failed}
        onSubmit={(username, password) => bootstrapWithCredentials(username, password)}
      />
    );
  }
  if (state.status === "error") {
    return (
      <div className="flex h-full w-full items-center justify-center px-4 text-center">
        <div className="flex max-w-md flex-col items-center gap-3">
          <p className="text-lg font-semibold">{t("app.error.title")}</p>
          <p className="text-sm text-muted-foreground">{state.message}</p>
          <p className="text-xs text-muted-foreground">
            {t("app.error.gatewayHint")}
          </p>
        </div>
      </div>
    );
  }

  return (
    <ClientProvider
      client={state.client}
      token={state.token}
      modelName={state.modelName}
    >
      <Shell onModelNameChange={handleModelNameChange} onLogout={handleLogout} />
    </ClientProvider>
  );
}

function Shell({
  onModelNameChange,
  onLogout,
}: {
  onModelNameChange: (modelName: string | null) => void;
  onLogout: () => void;
}) {
  const { t, i18n } = useTranslation();
  const { client } = useClient();
  const { theme, toggle } = useTheme();
  const { sessions, loading, refresh, createChat, deleteChat } = useSessions();
  const { state: sidebarState, update: updateSidebarState } =
    useSidebarState(sessions, !loading);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [view, setView] = useState<ShellView>("chat");
  const [desktopSidebarOpen, setDesktopSidebarOpen] =
    useState<boolean>(readSidebarOpen);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [sessionSearchOpen, setSessionSearchOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<{
    key: string;
    label: string;
  } | null>(null);
  const [pendingRename, setPendingRename] = useState<{
    key: string;
    label: string;
  } | null>(null);
  const restartSawDisconnectRef = useRef(false);
  const [restartToast, setRestartToast] = useState<string | null>(null);
  const [isRestarting, setIsRestarting] = useState(false);
  const [runningChatIds, setRunningChatIds] = useState<Set<string>>(() => new Set());
  const [completedChatIds, setCompletedChatIds] = useState<Set<string>>(readCompletedRunChatIds);
  const runningChatIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    try {
      window.localStorage.setItem(
        SIDEBAR_STORAGE_KEY,
        desktopSidebarOpen ? "1" : "0",
      );
    } catch {
      // ignore storage errors (private mode, etc.)
    }
  }, [desktopSidebarOpen]);

  useEffect(() => {
    writeCompletedRunChatIds(completedChatIds);
  }, [completedChatIds]);

  const activeSession = useMemo<ChatSummary | null>(() => {
    if (!activeKey) return null;
    return sessions.find((s) => s.key === activeKey) ?? null;
  }, [sessions, activeKey]);
  const runningChatIdList = useMemo(() => Array.from(runningChatIds), [runningChatIds]);
  const completedChatIdList = useMemo(() => Array.from(completedChatIds), [completedChatIds]);

  useEffect(() => {
    if (loading) return;
    const knownChatIds = new Set(sessions.map((session) => session.chatId));
    setCompletedChatIds((current) => {
      const next = new Set(
        Array.from(current).filter((chatId) => knownChatIds.has(chatId)),
      );
      return next.size === current.size ? current : next;
    });
  }, [loading, sessions]);

  useEffect(() => {
    if (loading) return;
    const activeRunIds = sessions
      .filter((session) => typeof session.runStartedAt === "number")
      .map((session) => session.chatId);
    if (activeRunIds.length === 0) return;

    for (const chatId of activeRunIds) {
      client.attach(chatId);
    }
    setRunningChatIds((current) => {
      let changed = false;
      const next = new Set(current);
      for (const chatId of activeRunIds) {
        if (!next.has(chatId)) changed = true;
        next.add(chatId);
      }
      if (!changed) return current;
      runningChatIdsRef.current = next;
      return next;
    });
    setCompletedChatIds((current) => {
      let changed = false;
      const next = new Set(current);
      for (const chatId of activeRunIds) {
        if (next.delete(chatId)) changed = true;
      }
      return changed ? next : current;
    });
  }, [client, loading, sessions]);

  const closeDesktopSidebar = useCallback(() => {
    setDesktopSidebarOpen(false);
  }, []);

  const openDesktopSidebar = useCallback(() => {
    setDesktopSidebarOpen(true);
  }, []);

  const closeMobileSidebar = useCallback(() => {
    setMobileSidebarOpen(false);
  }, []);

  const toggleSidebar = useCallback(() => {
    const isDesktop =
      typeof window !== "undefined" &&
      window.matchMedia("(min-width: 1024px)").matches;
    if (isDesktop) {
      setDesktopSidebarOpen((v) => !v);
    } else {
      setMobileSidebarOpen((v) => !v);
    }
  }, []);

  const onCreateChat = useCallback(async () => {
    try {
      const chatId = await createChat();
      // Use getUser() to compute the key identically to createChat().
      // sessions state is stale here (React hasn't flushed setSessions yet),
      // so sessions.find() would miss the newly inserted row.
      const userId = getUser()?.id ?? "";
      const key = userId ? `websocket:${userId}:${chatId}` : `websocket:${chatId}`;
      setActiveKey(key);
      setView("chat");
      setMobileSidebarOpen(false);
      return chatId;
    } catch (e) {
      console.error("Failed to create chat", e);
      return null;
    }
  }, [createChat]);

  const onNewChat = useCallback(() => {
    setActiveKey(null);
    setView("chat");
    setMobileSidebarOpen(false);
  }, []);

  const onSelectChat = useCallback(
    (key: string) => {
      const selectedChatId = sessions.find((session) => session.key === key)?.chatId;
      if (selectedChatId) {
        setCompletedChatIds((current) => {
          if (!current.has(selectedChatId)) return current;
          const next = new Set(current);
          next.delete(selectedChatId);
          return next;
        });
      }
      setActiveKey(key);
      setView("chat");
      setMobileSidebarOpen(false);
    },
    [sessions],
  );

  const onTogglePin = useCallback(
    (key: string) => {
      void updateSidebarState((current) => {
        const pinned = new Set(current.pinned_keys);
        if (pinned.has(key)) {
          pinned.delete(key);
        } else {
          pinned.add(key);
        }
        return {
          ...current,
          pinned_keys: Array.from(pinned),
        };
      });
    },
    [updateSidebarState],
  );

  const onRequestRename = useCallback((key: string, label: string) => {
    setPendingRename({ key, label });
  }, []);

  const onConfirmRename = useCallback(
    (title: string) => {
      if (!pendingRename) return;
      const key = pendingRename.key;
      setPendingRename(null);
      void updateSidebarState((current) => {
        const titleOverrides = { ...current.title_overrides };
        const cleaned = title.trim();
        if (cleaned) {
          titleOverrides[key] = cleaned;
        } else {
          delete titleOverrides[key];
        }
        return {
          ...current,
          title_overrides: titleOverrides,
        };
      });
    },
    [pendingRename, updateSidebarState],
  );

  const onToggleArchive = useCallback(
    (key: string) => {
      void updateSidebarState((current) => {
        const archived = new Set(current.archived_keys);
        const pinned = current.pinned_keys.filter((item) => item !== key);
        if (archived.has(key)) {
          archived.delete(key);
        } else {
          archived.add(key);
        }
        return {
          ...current,
          pinned_keys: pinned,
          archived_keys: Array.from(archived),
        };
      });
      if (activeKey === key && !sidebarState.archived_keys.includes(key)) {
        const archived = new Set([...sidebarState.archived_keys, key]);
        const next = sessions.find((session) => !archived.has(session.key));
        setActiveKey(next?.key ?? null);
      }
    },
    [activeKey, sessions, sidebarState.archived_keys, updateSidebarState],
  );

  const onToggleArchived = useCallback(() => {
    void updateSidebarState((current) => ({
      ...current,
      view: {
        ...current.view,
        show_archived: !current.view.show_archived,
      },
    }));
  }, [updateSidebarState]);

  const onUpdateSidebarView = useCallback(
    (viewUpdate: Partial<typeof sidebarState.view>) => {
      void updateSidebarState((current) => ({
        ...current,
        view: {
          ...current.view,
          ...viewUpdate,
        },
      }));
    },
    [updateSidebarState],
  );

  const onOpenSessionSearch = useCallback(() => {
    setMobileSidebarOpen(false);
    setSessionSearchOpen(true);
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const plainCommandK =
        (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey;
      if (!plainCommandK) return;
      if (event.key.toLowerCase() !== "k") return;
      event.preventDefault();
      onOpenSessionSearch();
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onOpenSessionSearch]);

  const onSelectSearchResult = useCallback(
    (key: string) => {
      setSessionSearchOpen(false);
      onSelectChat(key);
    },
    [onSelectChat],
  );

  const onOpenSettings = useCallback(() => {
    setSessionSearchOpen(false);
    setView("settings");
    setMobileSidebarOpen(false);
  }, []);

  const onBackToChat = useCallback(() => {
    setView("chat");
    setMobileSidebarOpen(false);
    setActiveKey((current) => {
      if (!current) return null;
      if (sessions.some((session) => session.key === current)) return current;
      return sessions[0]?.key ?? null;
    });
  }, [sessions]);

  const onRestart = useCallback(() => {
    const chatId = activeSession?.chatId ?? client.defaultChatId;
    if (!chatId) return;
    restartSawDisconnectRef.current = false;
    setIsRestarting(true);
    try {
      window.localStorage.setItem(RESTART_STARTED_KEY, String(Date.now()));
    } catch {
      // ignore storage errors
    }
    client.sendMessage(chatId, "/restart");
  }, [activeSession?.chatId, client]);

  useEffect(() => {
    return client.onRuntimeModelUpdate((modelName) => {
      onModelNameChange(modelName);
    });
  }, [client, onModelNameChange]);

  useEffect(() => {
    return client.onRunStatus((chatId, startedAt) => {
      if (startedAt != null) {
        const nextRunning = new Set(runningChatIdsRef.current);
        nextRunning.add(chatId);
        runningChatIdsRef.current = nextRunning;
        setRunningChatIds(nextRunning);
        setCompletedChatIds((current) => {
          if (!current.has(chatId)) return current;
          const next = new Set(current);
          next.delete(chatId);
          return next;
        });
        return;
      }

      if (!runningChatIdsRef.current.has(chatId)) return;
      const nextRunning = new Set(runningChatIdsRef.current);
      nextRunning.delete(chatId);
      runningChatIdsRef.current = nextRunning;
      setRunningChatIds(nextRunning);
      setCompletedChatIds((current) => {
        const next = new Set(current);
        next.add(chatId);
        return next;
      });
    });
  }, [client]);

  useEffect(() => {
    return client.onStatus((status) => {
      let startedAt = 0;
      try {
        startedAt = Number(window.localStorage.getItem(RESTART_STARTED_KEY) ?? "0");
      } catch {
        startedAt = 0;
      }
      if (!startedAt) return;
      if (status !== "open") {
        restartSawDisconnectRef.current = true;
        return;
      }
      const elapsedMs = Date.now() - startedAt;
      if (!restartSawDisconnectRef.current && elapsedMs < 1500) return;
      try {
        window.localStorage.removeItem(RESTART_STARTED_KEY);
      } catch {
        // ignore storage errors
      }
      setIsRestarting(false);
      setRestartToast(t("app.restart.completed", { seconds: (elapsedMs / 1000).toFixed(1) }));
      window.setTimeout(() => setRestartToast(null), 3_500);
    });
  }, [client, t]);

  const onTurnEnd = useDeferredTitleRefresh(activeSession, refresh);

  const onConfirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const key = pendingDelete.key;
    const deletingActive = activeKey === key;
    const currentIndex = sessions.findIndex((s) => s.key === key);
    const fallbackKey = deletingActive
      ? (sessions[currentIndex + 1]?.key ?? sessions[currentIndex - 1]?.key ?? null)
      : activeKey;
    setPendingDelete(null);
    if (deletingActive) setActiveKey(fallbackKey);
    try {
      await deleteChat(key);
    } catch (e) {
      if (deletingActive) setActiveKey(key);
      console.error("Failed to delete session", e);
    }
  }, [pendingDelete, deleteChat, activeKey, sessions]);

  const headerTitle = activeSession
    ? sidebarState.title_overrides[activeSession.key] ||
      activeSession.title ||
      deriveTitle(activeSession.preview, t("chat.newChat"))
    : t("app.brand");

  useEffect(() => {
    if (view === "settings") {
      document.title = t("app.documentTitle.chat", {
        title: t("settings.sidebar.title"),
      });
      return;
    }
    document.title = activeSession
      ? t("app.documentTitle.chat", { title: headerTitle })
      : t("app.documentTitle.base");
  }, [activeSession, headerTitle, i18n.resolvedLanguage, t, view]);

  const sidebarProps = {
    sessions,
    activeKey,
    loading,
    onNewChat,
    onSelect: onSelectChat,
    onRequestDelete: (key: string, label: string) =>
      setPendingDelete({ key, label }),
    onTogglePin,
    onRequestRename,
    onToggleArchive,
    onOpenSettings,
    onOpenSearch: onOpenSessionSearch,
    onToggleArchived,
    onUpdateView: onUpdateSidebarView,
    pinnedKeys: sidebarState.pinned_keys,
    archivedKeys: sidebarState.archived_keys,
    titleOverrides: sidebarState.title_overrides,
    runningChatIds: runningChatIdList,
    completedChatIds: completedChatIdList,
    viewState: sidebarState.view,
    showArchived: sidebarState.view.show_archived,
    archivedCount: sidebarState.archived_keys.length,
  };
  const showMainSidebar = view !== "settings";

  return (
    <ThemeProvider theme={theme}>
      <div className="relative flex h-full w-full overflow-hidden">
        {/* Desktop sidebar: in normal flow, so the thread area width stays honest. */}
        {showMainSidebar ? (
          <aside
            className={cn(
              "relative z-20 hidden shrink-0 overflow-hidden lg:block",
              "transition-[width] duration-300 ease-out",
            )}
            style={{
              width: desktopSidebarOpen ? SIDEBAR_WIDTH : SIDEBAR_RAIL_WIDTH,
            }}
          >
            <div
              className="absolute inset-y-0 left-0 h-full w-full overflow-hidden bg-sidebar shadow-inner-right"
            >
              <Sidebar
                {...sidebarProps}
                collapsed={!desktopSidebarOpen}
                onCollapse={closeDesktopSidebar}
                onExpand={openDesktopSidebar}
              />
            </div>
          </aside>
        ) : null}

        {showMainSidebar ? (
          <Sheet
            open={mobileSidebarOpen}
            onOpenChange={(open) => setMobileSidebarOpen(open)}
          >
            <SheetContent
              side="left"
              showCloseButton={false}
              aria-describedby={undefined}
              className="p-0 lg:hidden"
              style={{ width: SIDEBAR_WIDTH, maxWidth: SIDEBAR_WIDTH }}
            >
              <SheetTitle className="sr-only">{t("sidebar.navigation")}</SheetTitle>
              <Sidebar
                {...sidebarProps}
                onCollapse={closeMobileSidebar}
                containActionMenus
              />
            </SheetContent>
          </Sheet>
        ) : null}

        <SessionSearchDialog
          open={sessionSearchOpen}
          onOpenChange={setSessionSearchOpen}
          sessions={sessions}
          activeKey={activeKey}
          loading={loading}
          titleOverrides={sidebarState.title_overrides}
          onSelect={onSelectSearchResult}
        />

        <main className="relative flex h-full min-w-0 flex-1 flex-col">
          <div
            className={cn(
              "absolute inset-0 flex flex-col",
              view === "settings" && "invisible pointer-events-none",
            )}
          >
            <ThreadShell
              session={activeSession}
              title={headerTitle}
              onToggleSidebar={toggleSidebar}
              onNewChat={onNewChat}
              onCreateChat={onCreateChat}
              onTurnEnd={onTurnEnd}
              theme={theme}
              onToggleTheme={toggle}
              hideSidebarToggleOnDesktop
            />
          </div>
          {view === "settings" && (
            <div className="absolute inset-0 flex flex-col">
              <SettingsView
                theme={theme}
                onToggleTheme={toggle}
                onBackToChat={onBackToChat}
                onModelNameChange={onModelNameChange}
                onLogout={onLogout}
                onRestart={onRestart}
                isRestarting={isRestarting}
              />
            </div>
          )}
        </main>

        <DeleteConfirm
          open={!!pendingDelete}
          title={pendingDelete?.label ?? ""}
          onCancel={() => setPendingDelete(null)}
          onConfirm={onConfirmDelete}
        />
        <RenameChatDialog
          open={!!pendingRename}
          title={pendingRename?.label ?? ""}
          onCancel={() => setPendingRename(null)}
          onConfirm={onConfirmRename}
        />
        {restartToast ? (
          <div
            role="status"
            className="fixed left-1/2 top-4 z-50 -translate-x-1/2 rounded-full border border-border/70 bg-popover px-4 py-2 text-sm font-medium text-popover-foreground shadow-lg"
          >
            {restartToast}
          </div>
        ) : null}
      </div>
    </ThemeProvider>
  );
}
