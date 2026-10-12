// Typed API client for the parent endpoints.
const BASE = "/api/parent";

async function j<T>(r: Response): Promise<T> {
  if (r.status === 401 && !new URL(r.url, location.origin).pathname.startsWith("/api/auth/")) {
    // session expired / revoked mid-use: let the app drop back to the sign-in screen
    window.dispatchEvent(new Event("sg-unauthorized"));
  }
  if (!r.ok) {
    // Surface a SHORT, meaningful message. FastAPI errors are {"detail": "..."} or
    // {"detail": [{msg}]}; gateway/proxy errors (502/504) return an HTML page — never dump that
    // blob into the UI. Fall back to a friendly status-based line.
    const body = await r.text().catch(() => "");
    let msg = "";
    try {
      const o = JSON.parse(body);
      if (typeof o?.detail === "string") msg = o.detail;
      else if (Array.isArray(o?.detail)) msg = o.detail.map((d: any) => d?.msg).filter(Boolean).join(", ");
    } catch { /* non-JSON (e.g. an HTML gateway page) — ignore the body */ }
    if (!msg) {
      msg = r.status >= 500 ? `Server unavailable (${r.status}) — it may be starting up. Try again in a moment.`
        : r.status === 401 ? "Wrong email or password."
        : r.status === 403 ? "You don't have access to that."
        : `Something went wrong (${r.status}).`;
    }
    throw new Error(msg);
  }
  return r.status === 204 ? (undefined as T) : r.json();
}

export interface Child {
  id: string; public_slug: string; name: string; age_band: string; pronouns: string | null;
  active: boolean; notes: string | null; narrator_voice: string | null; language?: string;
}
export interface ModelInfo { id: string; state: string | null; }
export interface Models { local: ModelInfo[]; cloud: ModelInfo[]; }
export interface Settings {
  active_gen_model: string; active_judge_model: string; judge_use_opus: boolean;
  gen_use_opus: boolean; tts_engine: string; tts_voice: string; tts_exaggeration: number;
  daily_story_limit: number;
  chatterbox_url: string; kokoro_url: string;
  chatterbox_url_default?: string; kokoro_url_default?: string;
}
export interface TtsHealth { url: string; ok: boolean; model_loaded?: boolean; error?: string; }
export interface Stats {
  total_ready: number; total_failed: number; total_rejected: number;
  by_gen_model: Record<string, number>; by_engine: Record<string, number>;
}
export interface AdminLogs { unit: string; lines: number; text: string; }
export interface LibraryItem {
  id: string; title: string; child_id: string; child_name: string;
  status: string; gen_model: string; engine: string | null;
  generation_seconds: number | null; duration_seconds: number | null; created_at: string;
  nearest_sim: number | null; nearest_title: string | null;
  rating: number | null; play_count: number; reject_reason: string | null; craft_overall: number | null;
  favorited: boolean;
}
export interface Character {
  id: string; child_id: string; name: string; archetype: string | null;
  linked_child_id: string | null; description: string | null;
}
export interface Pronunciation {
  id: string; surface: string; ipa: string | null; respelling: string | null; source: string;
}
export interface Banned { id: string; term: string; replacement: string | null; }
export interface VoiceProfile { id: string; label: string; kind: string; engine: string; has_clip: boolean; created_at: string; }
export interface Safety {
  verdict: string; categories: Record<string, string>; reasons: string | null;
  banned_hits: any[]; judge_model: string;
}
export interface StorySummary {
  id: string; title: string; summary: string | null; status: string; stage: string | null;
  child_id: string; safety: Safety | null; created_at: string;
  duration_seconds: number | null; generation_seconds: number | null;
  gen_model: string | null; engine: string | null;
  rating: number | null; play_count: number; reject_reason: string | null; craft_overall: number | null;
  favorited: boolean;
}

export interface Me { id: string; email: string | null; display_name: string; is_admin: boolean; household_id: string; }
export interface CoParent { id: string; email: string | null; display_name: string; is_admin: boolean; }

export interface WorldBible { characters: { name: string; description: string }[]; facts: string[]; }

export const api = {
  // ---- auth ----
  me: () => fetch(`/api/auth/me`).then(j<Me>),
  authConfig: () => fetch(`/api/auth/config`).then(j<{ open_signup: boolean }>),

  voices: () => fetch(`${BASE}/voices`).then(j<{ voices: VoiceProfile[]; reading_script: string }>),
  uploadVoice: (form: FormData) => fetch(`${BASE}/voices`, { method: "POST", body: form }).then(j<VoiceProfile>),
  delVoice: (id: string) => fetch(`${BASE}/voices/${id}`, { method: "DELETE" }).then(j<void>),
  login: (email: string, password: string) => fetch(`/api/auth/login`, { method: "POST", headers: hd, body: JSON.stringify({ email, password }) }).then(j<Me>),
  signup: (email: string, password: string, display_name: string) => fetch(`/api/auth/signup`, { method: "POST", headers: hd, body: JSON.stringify({ email, password, display_name }) }).then(j<Me>),
  logout: () => fetch(`/api/auth/logout`, { method: "POST" }).then(j<void>),
  changePassword: (current_password: string, new_password: string) =>
    fetch(`/api/auth/change-password`, { method: "POST", headers: hd, body: JSON.stringify({ current_password, new_password }) }).then(j<void>),
  coparents: () => fetch(`${BASE}/coparents`).then(j<CoParent[]>),
  addCoparent: (b: any) => fetch(`${BASE}/coparents`, { method: "POST", headers: hd, body: JSON.stringify(b) }).then(j<CoParent>),

  children: () => fetch(`${BASE}/children`).then(j<Child[]>),
  addChild: (b: any) => fetch(`${BASE}/children`, { method: "POST", headers: hd, body: JSON.stringify(b) }).then(j<Child>),
  patchChild: (id: string, b: any) => fetch(`${BASE}/children/${id}`, { method: "PATCH", headers: hd, body: JSON.stringify(b) }).then(j<Child>),
  rotateSlug: (id: string) => fetch(`${BASE}/children/${id}/rotate-slug`, { method: "POST" }).then(j<Child>),
  delChild: (id: string) => fetch(`${BASE}/children/${id}`, { method: "DELETE" }).then(j<void>),

  characters: (childId: string) => fetch(`${BASE}/children/${childId}/characters`).then(j<Character[]>),
  addCharacter: (childId: string, b: any) => fetch(`${BASE}/children/${childId}/characters`, { method: "POST", headers: hd, body: JSON.stringify(b) }).then(j<Character>),
  delCharacter: (id: string) => fetch(`${BASE}/characters/${id}`, { method: "DELETE" }).then(j<void>),
  suggest: (childId: string, theme: string) => fetch(`${BASE}/children/${childId}/characters/suggest`, { method: "POST", headers: hd, body: JSON.stringify({ theme }) }).then(j<{ suggestions: { name: string; archetype: string | null }[] }>),
  charactersFromStories: (childId: string) => fetch(`${BASE}/children/${childId}/characters/from-stories`).then(j<{ suggestions: { name: string; archetype: string | null }[] }>),

  pronunciations: () => fetch(`${BASE}/pronunciations`).then(j<Pronunciation[]>),
  savePron: (b: any) => fetch(`${BASE}/pronunciations`, { method: "POST", headers: hd, body: JSON.stringify(b) }).then(j<Pronunciation>),
  previewPronUrl: () => `${BASE}/pronunciations/preview`,

  banned: () => fetch(`${BASE}/banned`).then(j<Banned[]>),
  addBanned: (b: any) => fetch(`${BASE}/banned`, { method: "POST", headers: hd, body: JSON.stringify(b) }).then(j<Banned>),
  delBanned: (id: string) => fetch(`${BASE}/banned/${id}`, { method: "DELETE" }).then(j<void>),

  createRequest: (b: any) => fetch(`${BASE}/requests`, { method: "POST", headers: hd, body: JSON.stringify(b) }).then(j<any>),

  getWorldBible: (childId: string) => fetch(`${BASE}/children/${childId}/world_bible`).then(j<WorldBible>),
  putWorldBible: (childId: string, b: WorldBible) => fetch(`${BASE}/children/${childId}/world_bible`, { method: "PUT", headers: hd, body: JSON.stringify(b) }).then(j<WorldBible>),

  stories: (status?: string, childId?: string) => {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    if (childId) q.set("child_id", childId);
    const qs = q.toString();
    return fetch(`${BASE}/stories${qs ? `?${qs}` : ""}`).then(j<StorySummary[]>);
  },
  regenerate: (id: string, reason?: string, note?: string) => fetch(`${BASE}/stories/${id}/regenerate`, { method: "POST", headers: hd, body: JSON.stringify({ reason, note }) }).then(j<any>),
  approve: (id: string, override_fail: boolean) => fetch(`${BASE}/stories/${id}/approve`, { method: "POST", headers: hd, body: JSON.stringify({ override_fail }) }).then(j<StorySummary>),
  reject: (id: string, reason?: string, note?: string) => fetch(`${BASE}/stories/${id}/reject`, { method: "POST", headers: hd, body: JSON.stringify({ reason, note }) }).then(j<StorySummary>),
  rate: (id: string, rating: number, note?: string) => fetch(`${BASE}/stories/${id}/rate`, { method: "POST", headers: hd, body: JSON.stringify({ rating, note }) }).then(j<StorySummary>),
  previewAudioUrl: (id: string) => `${BASE}/stories/${id}/preview-audio`,
  audioUrl: (storyId: string) => `/api/child/audio/${storyId}`,

  // --- admin ---
  models: () => fetch(`${BASE}/models`).then(j<Models>),
  getSettings: () => fetch(`${BASE}/settings`).then(j<Settings>),
  putSettings: (b: Partial<Settings>) => fetch(`${BASE}/settings`, { method: "PUT", headers: hd, body: JSON.stringify(b) }).then(j<Settings>),
  ttsHealth: () => fetch(`${BASE}/admin/tts-health`).then(j<{ chatterbox: TtsHealth; kokoro: TtsHealth }>),
  systemLoaded: () => fetch(`${BASE}/system/loaded`).then(j<any>),
  adminVoices: () => fetch(`${BASE}/admin/voices`).then(j<{ voices: { id: string; enabled: boolean; has_sample: boolean }[]; curated: boolean }>),
  setVoicePool: (enabled: string[]) => fetch(`${BASE}/admin/voices/pool`, { method: "PUT", headers: hd, body: JSON.stringify({ enabled }) }).then(j<any>),
  voiceSampleUrl: (voice: string) => `${BASE}/admin/voice_sample?voice=${encodeURIComponent(voice)}`,
  logs: (unit: string, lines: number) => {
    const q = new URLSearchParams({ unit, lines: String(lines) });
    return fetch(`${BASE}/admin/logs?${q}`).then(j<AdminLogs>);
  },
  stats: () => fetch(`${BASE}/stats`).then(j<Stats>),
  library: () => fetch(`${BASE}/library`).then(j<LibraryItem[]>),
  deleteStory: (id: string) => fetch(`${BASE}/stories/${id}`, { method: "DELETE" }).then(j<void>),
};

const hd = { "content-type": "application/json" };
