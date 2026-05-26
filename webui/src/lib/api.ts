import type {
  ChatSummary,
  CliAppsPayload,
  ImageGenerationSettingsUpdate,
  McpPresetsPayload,
  ModelConfigurationCreate,
  ProviderSettingsUpdate,
  SettingsPayload,
  SettingsUpdate,
  SidebarStatePayload,
  SlashCommand,
  UserGroup,
  WebSearchSettingsUpdate,
  WebuiThreadPersistedPayload,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

let _onUnauthorized: (() => void) | null = null;

/** Register a callback invoked whenever any API call receives a 401. */
export function onApiUnauthorized(cb: (() => void) | null): void {
  _onUnauthorized = cb;
}

async function request<T>(
  url: string,
  token: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(url, {
    ...(init ?? {}),
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${token}`,
    },
    credentials: "same-origin",
  });
  if (!res.ok) {
    if (res.status === 401 && _onUnauthorized) {
      _onUnauthorized();
    }
    let message = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body.error) message = body.error;
    } catch { /* ignore */ }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

function mcpValuesHeader(values: Record<string, unknown>): HeadersInit | undefined {
  const payload: Record<string, unknown> = {};
  Object.entries(values).forEach(([key, value]) => {
    if (value === null || value === undefined) return;
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (trimmed) payload[key] = trimmed;
      return;
    }
    payload[key] = value;
  });
  if (!Object.keys(payload).length) return undefined;
  return { "X-Nanobot-MCP-Values": JSON.stringify(payload) };
}

function splitKey(key: string): { channel: string; userId: string; chatId: string } {
  const parts = key.split(":");
  if (parts.length >= 3) {
    return { channel: parts[0], userId: parts[1], chatId: parts.slice(2).join(":") };
  }
  if (parts.length === 2) {
    return { channel: parts[0], userId: "", chatId: parts[1] };
  }
  return { channel: "", userId: "", chatId: key };
}

export async function listSessions(
  token: string,
  base: string = "",
): Promise<ChatSummary[]> {
  type Row = {
    key: string;
    created_at: string | null;
    updated_at: string | null;
    title?: string;
    preview?: string;
    run_started_at?: number | null;
  };
  const body = await request<{ sessions: Row[] }>(
    `${base}/api/sessions`,
    token,
  );
  return body.sessions.map((s) => ({
    key: s.key,
    ...splitKey(s.key),
    createdAt: s.created_at,
    updatedAt: s.updated_at,
    title: s.title ?? "",
    preview: s.preview ?? "",
    runStartedAt: s.run_started_at ?? null,
  }));
}

/** Disk-backed WebUI display thread snapshot (separate from agent session). */
export async function fetchWebuiThread(
  token: string,
  key: string,
  base: string = "",
): Promise<WebuiThreadPersistedPayload | null> {
  const url = `${base}/api/sessions/${encodeURIComponent(key)}/webui-thread`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "same-origin",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return (await res.json()) as WebuiThreadPersistedPayload;
}

export async function deleteSession(
  token: string,
  key: string,
  base: string = "",
): Promise<boolean> {
  const body = await request<{ deleted: boolean }>(
    `${base}/api/sessions/${encodeURIComponent(key)}/delete`,
    token,
  );
  return body.deleted;
}

export async function fetchSettings(
  token: string,
  base: string = "",
): Promise<SettingsPayload> {
  return request<SettingsPayload>(`${base}/api/settings`, token);
}

export async function fetchCliApps(
  token: string,
  base: string = "",
): Promise<CliAppsPayload> {
  return request<CliAppsPayload>(`${base}/api/settings/cli-apps`, token);
}

export async function runCliAppAction(
  token: string,
  action: "install" | "update" | "uninstall" | "test",
  name: string,
  base: string = "",
): Promise<CliAppsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<CliAppsPayload>(`${base}/api/settings/cli-apps/${action}?${query}`, token);
}

export async function fetchMcpPresets(
  token: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(`${base}/api/settings/mcp-presets`, token);
}

export async function runMcpPresetAction(
  token: string,
  action: "enable" | "remove" | "test",
  name: string,
  values: Record<string, string> = {},
  base: string = "",
): Promise<McpPresetsPayload> {
  const query = new URLSearchParams();
  query.set("name", name);
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/${action}?${query}`,
    token,
    { headers: mcpValuesHeader(values) },
  );
}

export async function saveCustomMcpServer(
  token: string,
  values: Record<string, string>,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/custom`,
    token,
    { headers: mcpValuesHeader(values) },
  );
}

export async function importMcpConfig(
  token: string,
  config: string,
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/import`,
    token,
    { headers: mcpValuesHeader({ config }) },
  );
}

export async function updateMcpServerTools(
  token: string,
  name: string,
  enabledTools: string[],
  base: string = "",
): Promise<McpPresetsPayload> {
  return request<McpPresetsPayload>(
    `${base}/api/settings/mcp-presets/tools`,
    token,
    { headers: mcpValuesHeader({ name, enabled_tools: enabledTools }) },
  );
}

export async function listSlashCommands(
  token: string,
  base: string = "",
): Promise<SlashCommand[]> {
  type Row = {
    command: string;
    title: string;
    description: string;
    icon: string;
    arg_hint?: string;
  };
  const body = await request<{ commands: Row[] }>(`${base}/api/commands`, token);
  return body.commands
    .filter((command) => !["/stop", "/restart"].includes(command.command))
    .map((command) => ({
      command: command.command,
      title: command.title,
      description: command.description,
      icon: command.icon,
      argHint: command.arg_hint ?? "",
    }));
}

export async function fetchSidebarState(
  token: string,
  base: string = "",
): Promise<SidebarStatePayload> {
  return request<SidebarStatePayload>(`${base}/api/webui/sidebar-state`, token);
}

export async function updateSidebarState(
  token: string,
  state: SidebarStatePayload,
  base: string = "",
): Promise<SidebarStatePayload> {
  const query = new URLSearchParams();
  query.set("state", JSON.stringify(state));
  return request<SidebarStatePayload>(
    `${base}/api/webui/sidebar-state/update?${query}`,
    token,
  );
}

export async function updateSettings(
  token: string,
  update: SettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (update.modelPreset !== undefined) {
    query.set("model_preset", update.modelPreset ?? "default");
  }
  if (update.model !== undefined) query.set("model", update.model);
  if (update.provider !== undefined) query.set("provider", update.provider);
  if (update.timezone !== undefined) query.set("timezone", update.timezone);
  if (update.botName !== undefined) query.set("bot_name", update.botName);
  if (update.botIcon !== undefined) query.set("bot_icon", update.botIcon);
  if (update.toolHintMaxLength !== undefined) {
    query.set("tool_hint_max_length", String(update.toolHintMaxLength));
  }
  if (update.disabledSkills !== undefined) {
    query.set("disabled_skills", JSON.stringify(update.disabledSkills));
  }
  return request<SettingsPayload>(`${base}/api/settings/update?${query}`, token);
}

export async function createModelConfiguration(
  token: string,
  configuration: ModelConfigurationCreate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  if (configuration.name !== undefined) query.set("name", configuration.name);
  query.set("label", configuration.label);
  query.set("provider", configuration.provider);
  query.set("model", configuration.model);
  return request<SettingsPayload>(
    `${base}/api/settings/model-configurations/create?${query}`,
    token,
  );
}

export async function updateProviderSettings(
  token: string,
  update: ProviderSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  if (update.apiKey !== undefined) query.set("api_key", update.apiKey);
  if (update.apiBase !== undefined) query.set("api_base", update.apiBase);
  if (update.apiType !== undefined) query.set("api_type", update.apiType);
  return request<SettingsPayload>(
    `${base}/api/settings/provider/update?${query}`,
    token,
  );
}

export async function updateWebSearchSettings(
  token: string,
  update: WebSearchSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("provider", update.provider);
  if (update.apiKey !== undefined) query.set("api_key", update.apiKey);
  if (update.baseUrl !== undefined) query.set("base_url", update.baseUrl);
  if (update.maxResults !== undefined) query.set("max_results", String(update.maxResults));
  if (update.timeout !== undefined) query.set("timeout", String(update.timeout));
  if (update.useJinaReader !== undefined) {
    query.set("use_jina_reader", String(update.useJinaReader));
  }
  return request<SettingsPayload>(
    `${base}/api/settings/web-search/update?${query}`,
    token,
  );
}

export async function updateImageGenerationSettings(
  token: string,
  update: ImageGenerationSettingsUpdate,
  base: string = "",
): Promise<SettingsPayload> {
  const query = new URLSearchParams();
  query.set("enabled", String(update.enabled));
  query.set("provider", update.provider);
  query.set("model", update.model);
  query.set("default_aspect_ratio", update.defaultAspectRatio);
  query.set("default_image_size", update.defaultImageSize);
  query.set("max_images_per_turn", String(update.maxImagesPerTurn));
  return request<SettingsPayload>(
    `${base}/api/settings/image-generation/update?${query}`,
    token,
  );
}

// -- Profile / Skills / Cron visibility endpoints ---------------------------

export interface ProfileData {
  soul: string;
  user: string;
  memory: string;
}

export async function fetchProfile(
  token: string,
  base: string = "",
): Promise<ProfileData> {
  return request<ProfileData>(`${base}/api/settings/profile`, token);
}

export interface SkillInfo {
  name: string;
  description: string;
  source: string;
  emoji?: string;
  always?: boolean;
  disabled?: boolean;
}

export interface SkillsListPayload {
  skills: SkillInfo[];
}

export async function fetchSkills(
  token: string,
  base: string = "",
): Promise<SkillsListPayload> {
  return request<SkillsListPayload>(`${base}/api/settings/skills`, token);
}

export async function deleteSkill(
  token: string,
  name: string,
  base: string = "",
): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(
    `${base}/api/settings/skills/${encodeURIComponent(name)}/delete`,
    token,
  );
}

export interface SkillContentPayload {
  name: string;
  content: string;
}

export async function fetchSkillContent(
  token: string,
  name: string,
  base: string = "",
): Promise<SkillContentPayload> {
  return request<SkillContentPayload>(
    `${base}/api/settings/skills/${encodeURIComponent(name)}/content`,
    token,
  );
}

export async function updateSkillContent(
  token: string,
  name: string,
  content: string,
  base: string = "",
): Promise<{ ok: boolean }> {
  const query = new URLSearchParams();
  query.set("content", content);
  return request<{ ok: boolean }>(
    `${base}/api/settings/skills/${encodeURIComponent(name)}/update?${query}`,
    token,
  );
}

export async function createSkill(
  token: string,
  name: string,
  content: string,
  base: string = "",
): Promise<{ created: string }> {
  const query = new URLSearchParams();
  query.set("name", name);
  query.set("content", content);
  return request<{ created: string }>(
    `${base}/api/settings/skills/create?${query}`,
    token,
  );
}

export interface CronJobInfo {
  id: string;
  name: string;
  enabled: boolean;
  schedule_kind: string;
  schedule: string;
  next_run_ms: number | null;
  last_status: string | null;
}

export interface CronListPayload {
  cron_jobs: CronJobInfo[];
}

export async function fetchCronJobs(
  token: string,
  base: string = "",
): Promise<CronListPayload> {
  return request<CronListPayload>(`${base}/api/settings/cron`, token);
}

export async function deleteCronJob(
  jobId: string,
  token: string,
  base: string = "",
): Promise<{ deleted: string }> {
  return request<{ deleted: string }>(
    `${base}/api/settings/cron/${encodeURIComponent(jobId)}/delete`,
    token,
  );
}

// -- User management (admin only) -------------------------------------------

export interface UsersListPayload {
  users: Array<{
    id: string;
    username: string;
    displayName: string;
    role: "admin" | "user";
  }>;
}

export async function fetchUsers(
  token: string,
  base: string = "",
): Promise<UsersListPayload> {
  return request<UsersListPayload>(`${base}/api/auth/users`, token);
}

export async function createUser(
  token: string,
  username: string,
  password: string,
  role: string,
  base: string = "",
): Promise<{ ok: boolean; user: { id: string; username: string; displayName: string; role: string } }> {
  const query = new URLSearchParams();
  query.set("username", username);
  query.set("password", password);
  query.set("role", role);
  return request(`${base}/api/auth/users/create?${query}`, token);
}

export async function deleteUser(
  token: string,
  userId: string,
  base: string = "",
): Promise<{ ok: boolean }> {
  return request(`${base}/api/auth/users/${encodeURIComponent(userId)}/delete`, token);
}

// -- My groups (current user) ---------------------------------------------------

export interface MyGroupsPayload {
  groups: UserGroup[];
}

export async function fetchMyGroups(
  token: string,
  base: string = "",
): Promise<MyGroupsPayload> {
  return request<MyGroupsPayload>(`${base}/api/me/groups`, token);
}

// -- Group management (admin only) --------------------------------------------

export interface GroupsListPayload {
  groups: Array<{
    id: string;
    name: string;
    displayName: string;
    settings: Record<string, unknown>;
    createdAt: string;
    updatedAt: string;
  }>;
}

export async function fetchGroups(
  token: string,
  base: string = "",
): Promise<GroupsListPayload> {
  return request<GroupsListPayload>(`${base}/api/groups`, token);
}

export async function createGroup(
  token: string,
  name: string,
  displayName: string,
  settings: Record<string, unknown> = {},
  base: string = "",
): Promise<{ id: string; name: string; displayName: string; settings: Record<string, unknown> }> {
  const query = new URLSearchParams();
  query.set("name", name);
  if (displayName) query.set("display_name", displayName);
  query.set("settings", JSON.stringify(settings));
  return request(`${base}/api/groups/create?${query}`, token);
}

export async function updateGroup(
  token: string,
  groupId: string,
  displayName?: string,
  settings?: Record<string, unknown>,
  base: string = "",
): Promise<{ id: string; name: string; displayName: string; settings: Record<string, unknown> }> {
  const query = new URLSearchParams();
  if (displayName !== undefined) query.set("display_name", displayName);
  if (settings !== undefined) query.set("settings", JSON.stringify(settings));
  return request(`${base}/api/groups/${encodeURIComponent(groupId)}/update?${query}`, token);
}

export async function deleteGroup(
  token: string,
  groupId: string,
  base: string = "",
): Promise<{ deleted: string }> {
  return request<{ deleted: string }>(
    `${base}/api/groups/${encodeURIComponent(groupId)}/delete`,
    token,
  );
}

export interface GroupDetailPayload {
  id: string;
  name: string;
  displayName: string;
  settings: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
  members: Array<{ userId: string; role: string }>;
}

export async function fetchGroupDetail(
  token: string,
  groupId: string,
  base: string = "",
): Promise<GroupDetailPayload> {
  return request<GroupDetailPayload>(`${base}/api/groups/${encodeURIComponent(groupId)}`, token);
}

export interface GroupMembersPayload {
  groupId: string;
  members: Array<{
    userId: string;
    username: string;
    displayName: string;
    role: string;
  }>;
}

export async function fetchGroupMembers(
  token: string,
  groupId: string,
  base: string = "",
): Promise<GroupMembersPayload> {
  return request<GroupMembersPayload>(
    `${base}/api/groups/${encodeURIComponent(groupId)}/members`,
    token,
  );
}

export async function addGroupMember(
  token: string,
  groupId: string,
  userId: string,
  role: string = "member",
  base: string = "",
): Promise<{ groupId: string; userId: string; role: string }> {
  const query = new URLSearchParams();
  query.set("user_id", userId);
  query.set("role", role);
  return request(
    `${base}/api/groups/${encodeURIComponent(groupId)}/members/add?${query}`,
    token,
  );
}

export async function removeGroupMember(
  token: string,
  groupId: string,
  userId: string,
  base: string = "",
): Promise<{ removed: string }> {
  return request(
    `${base}/api/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(userId)}/remove`,
    token,
  );
}

export interface GroupSkillsPayload {
  skills: Array<{ name: string; path: string; source: string }>;
}

export async function fetchGroupSkills(
  token: string,
  groupId: string,
  base: string = "",
): Promise<GroupSkillsPayload> {
  return request<GroupSkillsPayload>(
    `${base}/api/groups/${encodeURIComponent(groupId)}/skills`,
    token,
  );
}

export async function fetchGroupSkillContent(
  token: string,
  groupId: string,
  name: string,
  base: string = "",
): Promise<{ name: string; content: string }> {
  return request<{ name: string; content: string }>(
    `${base}/api/groups/${encodeURIComponent(groupId)}/skills/${encodeURIComponent(name)}/content`,
    token,
  );
}

export async function createGroupSkill(
  token: string,
  groupId: string,
  name: string,
  content: string,
  base: string = "",
): Promise<{ name: string; path: string }> {
  const query = new URLSearchParams();
  query.set("name", name);
  query.set("content", content);
  return request(
    `${base}/api/groups/${encodeURIComponent(groupId)}/skills/create?${query}`,
    token,
  );
}

export async function updateGroupSkill(
  token: string,
  groupId: string,
  name: string,
  content: string,
  base: string = "",
): Promise<{ ok: boolean; name: string }> {
  const query = new URLSearchParams();
  query.set("content", content);
  return request(
    `${base}/api/groups/${encodeURIComponent(groupId)}/skills/${encodeURIComponent(name)}/update?${query}`,
    token,
  );
}

export async function deleteGroupSkill(
  token: string,
  groupId: string,
  name: string,
  base: string = "",
): Promise<{ deleted: string }> {
  return request(
    `${base}/api/groups/${encodeURIComponent(groupId)}/skills/${encodeURIComponent(name)}/delete`,
    token,
  );
}
