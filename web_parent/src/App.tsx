import { useEffect, useRef, useState } from "react";
import { api, Child, CoParent, LibraryItem, Me, Models, Settings, StorySummary, TtsHealth, VoiceProfile, WorldBible } from "./api";

type Tab = "review" | "new" | "names" | "kids" | "admin";

// Shared single-audio player: returns the currently-playing id and a toggle. Only one story
// plays at a time; clicking the playing one pauses it (▶ ⇄ ⏸ without a page refresh).
function useAudioPlayer() {
  const ref = useRef<HTMLAudioElement | null>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const toggle = (id: string, url: string) => {
    let a = ref.current;
    if (!a) { a = new Audio(); ref.current = a; a.addEventListener("ended", () => setPlayingId(null)); }
    if (playingId === id) { a.pause(); setPlayingId(null); return; }
    a.pause(); a.src = url; a.play().then(() => setPlayingId(id)).catch(() => setPlayingId(null));
  };
  return { playingId, toggle };
}
type Player = ReturnType<typeof useAudioPlayer>;

// Child player URL — capability slug at /play/{slug}. Scheme-aware: uses the page's
// own protocol so it's https behind Cloudflare and http on the plain LAN/Tailscale (an absolute
// URL with the scheme is needed so iOS opens it from an installed standalone PWA).
function playerUrl(slug: string): string {
  return `${location.protocol}//${location.host}/play/${slug}`;
}

// Clipboard that also works on plain http (navigator.clipboard is undefined outside secure
// contexts, which is why "Copy link" silently failed). Falls back to a temp textarea + execCommand.
function copyText(text: string, showToast: (m: string) => void) {
  const done = () => showToast("🔗 Player link copied — bookmark it on the kid's device");
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done, showToast));
  } else {
    fallbackCopy(text, done, showToast);
  }
}
function fallbackCopy(text: string, done: () => void, showToast: (m: string) => void) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.focus(); ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    ok ? done() : showToast(text);  // last resort: show the link so it can be copied by hand
  } catch { showToast(text); }
}

// relative age ("just now", "2h ago", "1 day ago") — friendlier than a timestamp
function relAge(iso: string): string {
  if (!iso) return "";
  const t = Date.parse(iso); if (isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  const m = s / 60; if (m < 60) return `${Math.round(m)}m ago`;
  const h = m / 60; if (h < 24) return `${Math.round(h)}h ago`;
  const d = Math.round(h / 24); return d === 1 ? "1 day ago" : `${d} days ago`;
}

// reject/regenerate reason chips — value → friendly label
const REJECT_REASONS: [string, string][] = [
  ["boring", "Boring"], ["too_similar", "Too similar"], ["wrong_tone", "Wrong tone"],
  ["too_long", "Too long"], ["too_short", "Too short"], ["bad_fit_request", "Doesn't fit request"],
  ["safety", "Safety"], ["other", "Other"],
];

// 1–5 star rater. value null = unrated; tap a star to set. Each star is a ~44px
// touch target (the row used to be ~15px glyphs — too small to tap reliably on a phone).
function Stars({ value, onRate }: { value: number | null; onRate: (n: number) => void }) {
  return (
    <span style={{ display: "inline-flex", whiteSpace: "nowrap", margin: "-6px 0" }}>
      {[1, 2, 3, 4, 5].map((n) => (
        <span key={n} role="button" aria-label={`${n} star${n > 1 ? "s" : ""}`} title={`${n} star${n > 1 ? "s" : ""}`}
          onClick={(e) => { e.stopPropagation(); onRate(n); }}
          style={{
            cursor: "pointer", fontSize: 26, lineHeight: 1, padding: "8px 5px", minWidth: 28,
            textAlign: "center", touchAction: "manipulation", userSelect: "none",
            color: value && n <= value ? "var(--amber)" : "var(--muted)", opacity: value && n <= value ? 1 : 0.6,
          }}>
          {value && n <= value ? "★" : "☆"}
        </span>
      ))}
    </span>
  );
}

const TABS: Tab[] = ["review", "new", "names", "kids", "admin"];
const LOG_UNITS = ["tellybear-api", "tellybear-worker", "chatterbox-tts", "tellybear-batch", "tellybear-watchdog"];
function initialTab(): Tab {
  const p = new URLSearchParams(location.search);
  if (p.get("review")) return "review";           // ntfy approval deep-link
  const t = p.get("tab");
  return (t && (TABS as string[]).includes(t)) ? (t as Tab) : "review";
}

export default function App() {
  const [tab, setTab] = useState<Tab>(initialTab);
  const [children, setChildren] = useState<Child[]>([]);
  const [toast, setToast] = useState("");
  const showToast = (m: string) => { setToast(m); setTimeout(() => setToast(""), 1900); };
  const player = useAudioPlayer();

  // ---- auth gate ---- (resolve identity FIRST, then load household data)
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  useEffect(() => { api.me().then(setMe).catch(() => setMe(null)); }, []);
  useEffect(() => {
    const onUnauthorized = () => setMe(null);
    window.addEventListener("sg-unauthorized", onUnauthorized);
    return () => window.removeEventListener("sg-unauthorized", onUnauthorized);
  }, []);

  const [childId, setChildId] = useState<string>("");
  const reloadChildren = () => api.children().then((cs) => { setChildren(cs); return cs; });
  // (re)load children whenever we become signed in — the earlier "fetch on mount" ran before
  // login and 401'd, leaving Kids empty until a manual refresh.
  useEffect(() => { if (me) reloadChildren().catch(() => {}); }, [me]);
  // default the active child once children load (keep the current pick if it still exists)
  useEffect(() => {
    if (children.length && !children.some((c) => c.id === childId)) {
      setChildId((children.find((c) => c.active) || children[0]).id);
    }
  }, [children]);
  const selected = children.find((c) => c.id === childId);

  if (me === undefined) return <div id="parent" style={{ justifyContent: "center" }}><div className="center">Loading…</div></div>;
  if (me === null) return <AuthScreen onAuthed={setMe} />;
  if (me === undefined) return <div id="parent" style={{ justifyContent: "center" }}><div className="center">Loading…</div></div>;
  if (me === null) return <AuthScreen onAuthed={setMe} />;
  const isAdmin = !!me.is_admin;
  const visibleTabs = ([["review", "📥", "Review"], ["new", "✨", "New"], ["names", "🔊", "Names"], ["kids", "🧒", "Kids"], ...(isAdmin ? [["admin", "⚙️", "Admin"]] : [])] as [Tab, string, string][]);
  const safeTab: Tab = (tab === "admin" && !isAdmin) ? "review" : tab;
  const logout = async () => { try { await api.logout(); } catch {} setMe(null); };

  return (
    <div id="parent">
      <div className="p-head">
        <div className="brand" style={{ justifyContent: "space-between" }}>
          <span><span className="dot-moon" /> Tellybear</span>
          <span style={{ display: "flex", gap: 6 }}>
            <ChangePassword showToast={showToast} />
            <button className="chip" style={{ padding: "4px 12px", fontSize: 12 }} onClick={logout} title={me.email || ""}>Sign out</button>
          </span>
        </div>
        <div className="sub">{me.display_name}{isAdmin ? " · admin" : ""}{selected ? ` — for ${selected.name}` : ""}</div>
        {children.length > 1 && (tab === "review" || tab === "new") && (
          <div className="seg" style={{ marginTop: 10 }}>
            {children.map((c) => (
              <button key={c.id} className={`chip ${childId === c.id ? "sel" : ""}`} onClick={() => setChildId(c.id)}>
                {c.active ? "🧒" : "👶"} {c.name}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="p-body">
        {safeTab === "review" && <Review key={childId} childId={childId} children={children} showToast={showToast} player={player} />}
        {safeTab === "new" && <NewStory key={childId} initialChildId={childId} children={children} showToast={showToast} isAdmin={isAdmin} />}
        {safeTab === "names" && <Names showToast={showToast} />}
        {safeTab === "kids" && <Kids children={children} reload={reloadChildren} showToast={showToast} />}
        {safeTab === "admin" && isAdmin && <Admin showToast={showToast} player={player} />}
      </div>
      <div className={`toast ${toast ? "show" : ""}`}>{toast}</div>
      <div className="tabs">
        {visibleTabs.map(([id, ic, label]) => (
          <button key={id} className={`tab ${safeTab === id ? "sel" : ""}`} onClick={() => setTab(id)}>
            <span className="ic">{ic}</span>{label}
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------- Review ----------------
function Review({ childId, children, showToast, player }: { childId: string; children: Child[]; showToast: (m: string) => void; player: Player }) {
  const [stories, setStories] = useState<StorySummary[]>([]);
  const [inprog, setInprog] = useState<StorySummary[]>([]);
  const [ready, setReady] = useState<StorySummary[]>([]);
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  // reason-chip dialog for reject/regenerate
  const [reasonFor, setReasonFor] = useState<{ s: StorySummary; mode: "reject" | "regen" } | null>(null);
  const [chip, setChip] = useState("");
  const [note, setNote] = useState("");
  const cid = childId || undefined;  // scope every list to the selected child
  const load = () => {
    api.stories("needs_approval", cid).then(setStories).catch(() => {});
    api.stories("in_progress", cid).then(setInprog).catch(() => {});
    api.stories("ready", cid).then(setReady).catch(() => {});
  };
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, []);
  const nameOf = (id: string) => children.find((c) => c.id === id)?.name || "your child";
  const dateOf = (iso: string) => relAge(iso);
  const fmtLen = (s: number | null) => { if (!s) return ""; const m = Math.floor(s / 60), ss = Math.round(s % 60); return `${m}:${String(ss).padStart(2, "0")}`; };

  const labels: Record<string, string> = {
    violence: "No violence", scary: "Not scary", distressing: "Gentle theme",
    inappropriate: "Appropriate", unsafe_behavior: "Safe behavior",
    vocab_age_fit: "Age fit", ending_calm: "Calm ending", ip_characters: "Original characters",
  };

  const approve = async (s: StorySummary, force = false) => {
    try { await api.approve(s.id, overrides[s.id] || force); showToast("✓ Approved — making audio now"); load(); }
    catch (e: any) { showToast("Couldn't approve: " + e.message); }
  };
  const openReason = (s: StorySummary, mode: "reject" | "regen") => { setReasonFor({ s, mode }); setChip(""); setNote(""); };
  const confirmReason = async () => {
    if (!reasonFor) return;
    const { s, mode } = reasonFor; const r = chip || "other"; const n = note.trim() || undefined;
    setReasonFor(null);
    try {
      if (mode === "reject") { await api.reject(s.id, r, n); showToast("Rejected — won't be published"); }
      else { await api.regenerate(s.id, r, n); showToast("🔄 Regenerating — a fresh version is on the way"); }
      load();
    } catch (e: any) { showToast(e.message); }
  };
  const rate = async (s: StorySummary, n: number) => {
    try { await api.rate(s.id, n); showToast(`Rated ${n}★`); load(); }
    catch (e: any) { showToast("Couldn't rate: " + e.message); }
  };
  // preview-audio is a POST endpoint — fetch it and play the blob (new Audio(url) would GET → 405).
  const listen = async (s: StorySummary) => {
    setBusy(true); showToast("🔊 Making a short preview…");
    try {
      const r = await fetch(api.previewAudioUrl(s.id), { method: "POST" });
      if (!r.ok) throw new Error(String(r.status));
      const url = URL.createObjectURL(await r.blob());
      await new Audio(url).play();
    } catch { showToast("Couldn't play preview"); }
    finally { setBusy(false); }
  };

  const InProgress = () => inprog.length ? (
    <div className="card" style={{ background: "rgba(255,255,255,.04)" }}>
      <span className="for">In progress</span>
      {inprog.map((s) => (
        <div key={s.id} className="kidrow" style={{ marginTop: 8 }}>
          <span style={{ animation: "spin 1.2s linear infinite", display: "inline-block" }}>⏳</span>
          <div style={{ flex: 1 }}>
            <b>{s.title && s.title !== "…" ? s.title : `A story for ${nameOf(s.child_id)}`}</b>
            <div className="muted-note">{s.stage || s.status}</div>
          </div>
        </div>
      ))}
    </div>
  ) : null;

  const del = async (s: StorySummary) => {
    if (!confirm(`Remove "${s.title}"? This deletes its audio too.`)) return;
    try { await api.deleteStory(s.id); showToast("Removed"); load(); }
    catch (e: any) { showToast("Couldn't remove: " + e.message); }
  };

  // Parent-accessible "ready to play" shelf (not the owner-only Admin Library)
  const ReadyToPlay = () => ready.length ? (
    <div className="card">
      <span className="for">🎧 Ready to play · {ready.length} stor{ready.length === 1 ? "y" : "ies"}</span>
      {ready.map((s, i) => (
        <div key={s.id} className="kidrow" style={{ marginTop: 10 }}>
          <span className="muted-note" style={{ width: 22, textAlign: "right", flex: "none" }}>{i + 1}.</span>
          <button className="hear" style={{ width: 44, height: 44, fontSize: 18, flex: "none" }}
            onClick={() => player.toggle(s.id, api.audioUrl(s.id))}>{player.playingId === s.id ? "⏸" : "▶"}</button>
          <div style={{ flex: 1, minWidth: 0 }}>
            <b>{s.favorited && <span title="Favorited" style={{ color: "#ffd166", marginRight: 6 }}>★</span>}{s.title}</b>
            <div className="muted-note">For {nameOf(s.child_id)}{s.duration_seconds ? ` · ${fmtLen(s.duration_seconds)}` : ""}{s.play_count ? ` · ▶×${s.play_count}` : ""}</div>
            <Stars value={s.rating} onRate={(n) => rate(s, n)} />
          </div>
          <button className="chip" style={{ padding: "4px 9px", flex: "none" }} title="Remove" onClick={() => del(s)}>🗑</button>
        </div>
      ))}
    </div>
  ) : null;

  const reasonModal = reasonFor ? (
    <div className="modal-back" onClick={() => setReasonFor(null)}>
      <div className="card" style={{ maxWidth: 360, margin: "auto" }} onClick={(e) => e.stopPropagation()}>
        <b>{reasonFor.mode === "regen" ? "Why regenerate?" : "Why reject?"}</b>
        <p className="muted-note" style={{ marginTop: 2 }}>Pick a reason — it helps the system avoid this next time.</p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, margin: "10px 0" }}>
          {REJECT_REASONS.map(([val, lbl]) => (
            <button key={val} className={"chip" + (chip === val ? " sel" : "")} style={{ padding: "6px 12px" }}
              onClick={() => setChip(val)}>{lbl}</button>
          ))}
        </div>
        <input className="txt" placeholder="Optional note…" value={note} onChange={(e) => setNote(e.target.value)} />
        <div className="actions" style={{ marginTop: 12 }}>
          <button className="btn reject" onClick={() => setReasonFor(null)}>Cancel</button>
          <button className="btn approve" disabled={!chip} onClick={confirmReason}>
            {reasonFor.mode === "regen" ? "🔄 Regenerate" : "Reject"}</button>
        </div>
      </div>
    </div>
  ) : null;

  if (!stories.length) return (<>
    {reasonModal}
    <InProgress />
    {!inprog.length && !ready.length && (
      <div className="center" style={{ padding: "50px 24px" }}>
        <div style={{ fontSize: 40, marginBottom: 10 }}>🌙</div>
        Nothing to review right now.<br />Make one in the <b>New</b> tab — you'll get a ping when it's ready.
      </div>
    )}
    <ReadyToPlay />
  </>);

  return (<>
    {reasonModal}
    <InProgress />
    <ReadyToPlay />
    {stories.length > 0 && <div className="row-label" style={{ padding: "4px 2px 8px" }}>To review · {stories.length}</div>}
    {stories.map((s, i) => {
      const cats = s.safety?.categories || {};
      const flags = Object.entries(cats).filter(([, v]) => v === "concern").length;
      // A safety FAIL only needs an override when a CONTENT category is flagged. A fail with zero
      // flagged categories is a non-content gate (almost always the story came out shorter/longer
      // than the band's word-count bounds) — safe to play, so it approves in one tap.
      const contentFail = flags > 0;
      const noteOnly = s.safety?.verdict === "fail" && flags === 0;
      const reason = (s.safety?.reasons || "").trim();
      return (
        <div className="card" key={s.id}>
          <span className="for">{i + 1} · For {nameOf(s.child_id)}{s.created_at ? ` · ${dateOf(s.created_at)}` : ""}{s.duration_seconds ? ` · ${fmtLen(s.duration_seconds)}` : ""}</span>
          <h3>{s.title}</h3>
          <span className={`verdict ${contentFail ? "flag" : "pass"}`}>
            {contentFail ? `⚠ Needs a look — ${flags} flag${flags === 1 ? "" : "s"}` : "✓ Safe — ready to approve"}
          </span>
          <p className="summary">{s.summary}</p>
          <div className="chips">
            {Object.entries(cats).map(([k, v]) => (
              <span key={k} className={`schip ${v === "ok" ? "ok" : "bad"}`}>
                {v === "ok" ? "✓" : "⚠"} {labels[k] || k}
              </span>
            ))}
          </div>
          {noteOnly && reason && (
            <p className="muted-note">ℹ️ Note (not a safety flag): {reason}. Safe to play.</p>
          )}
          {contentFail && (
            <p className="muted-note">
              {reason && <span style={{ display: "block", marginBottom: 6 }}>⚠ {reason}</span>}
              <label><input type="checkbox" checked={overrides[s.id] || false}
                onChange={(e) => setOverrides({ ...overrides, [s.id]: e.target.checked })} />{" "}
                Approve anyway is locked while a flag is open — tick to override.</label>
            </p>
          )}
          <div className="actions">
            <button className="btn listen" onClick={() => listen(s)} disabled={busy}>Listen</button>
            <button className="btn reject" onClick={() => openReason(s, "regen")} title="Reject this and make a fresh version">🔄 Regen</button>
            <button className="btn reject" onClick={() => openReason(s, "reject")}>Reject</button>
            <button className="btn approve" disabled={contentFail && !overrides[s.id]}
              onClick={() => approve(s, noteOnly)}>Approve</button>
          </div>
        </div>
      );
    })}
  </>);
}

// ---------------- New ----------------
const STORY_TYPES = ["Adventure", "Friendship", "Bravery", "Bedtime calm", "Curiosity", "Kindness"];
function NewStory({ children, showToast, initialChildId, isAdmin }: { children: Child[]; showToast: (m: string) => void; initialChildId?: string; isAdmin?: boolean }) {
  const [childId, setChildId] = useState(initialChildId || "");
  const [chars, setChars] = useState<{ id: string; name: string; archetype: string | null }[]>([]);
  const [selChars, setSelChars] = useState<string[]>([]);
  const [type, setType] = useState("Friendship");
  const [special, setSpecial] = useState("");
  const [when, setWhen] = useState<"batch" | "now">("batch");
  const [inventing, setInventing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");
  const [newArch, setNewArch] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [series, setSeries] = useState(false);
  const [longForm, setLongForm] = useState(false);
  const [ambient, setAmbient] = useState<"off" | "under" | "tail">("off");
  const [bible, setBible] = useState<WorldBible | null>(null);
  const [genOverride, setGenOverride] = useState("");
  useEffect(() => { if (childId) api.getWorldBible(childId).then(setBible).catch(() => setBible(null)); }, [childId]);
  const [models, setModels] = useState<Models | null>(null);
  useEffect(() => { if (isAdmin) api.models().then(setModels).catch(() => {}); }, [isAdmin]);

  const reloadChars = () => childId ? api.characters(childId).then(setChars) : Promise.resolve();
  useEffect(() => { if (children.length && !childId) setChildId((children.find(c => c.active) || children[0]).id); }, [children]);
  useEffect(() => { if (childId) api.characters(childId).then(setChars).catch(() => {}); }, [childId]);

  const toggleChar = (id: string) => setSelChars((s) => s.includes(id) ? s.filter((x) => x !== id) : [...s, id]);

  const addChar = async () => {
    const name = newName.trim();
    if (!childId || !name) return;
    try {
      const c = await api.addCharacter(childId, { name, archetype: newArch.trim() || null });
      setChars((cs) => [...cs, c]); setSelChars((s) => [...s, c.id]);
      setNewName(""); setNewArch(""); setAdding(false); showToast(`✓ Added ${c.name}`);
    } catch (e: any) {
      showToast(e?.message || "Couldn't add character");
    }
  };
  const removeChar = async (c: { id: string; name: string }) => {
    if (!confirm(`Remove ${c.name}?`)) return;
    try { await api.delCharacter(c.id); setChars((cs) => cs.filter((x) => x.id !== c.id)); setSelChars((s) => s.filter((x) => x !== c.id)); }
    catch (e: any) { showToast("Couldn't remove: " + e.message); }
  };
  const importFromStories = async () => {
    if (!childId) return;
    showToast("📚 Looking through past stories…");
    try {
      const r = await api.charactersFromStories(childId);
      if (!r.suggestions.length) { showToast("No new characters found in past stories"); return; }
      const added: typeof chars = [];
      for (const s of r.suggestions) {
        try { added.push(await api.addCharacter(childId, { name: s.name, archetype: s.archetype })); } catch { /* skip */ }
      }
      if (added.length) { setChars((cs) => [...cs, ...added]); showToast(`✨ Imported ${added.map(a => a.name).join(", ")}`); }
      else showToast("Nothing new to import");
    } catch (e: any) { showToast("Couldn't import: " + e.message); }
  };
  const invent = async () => {
    if (!childId) return;
    setInventing(true); showToast("🤖 Inventing original characters…");
    try {
      const r = await api.suggest(childId, type);
      const added: typeof chars = [];
      for (const sug of r.suggestions.slice(0, 2)) {
        try { added.push(await api.addCharacter(childId, { name: sug.name, archetype: sug.archetype })); }
        catch { /* skip a name the IP filter rejects; keep the rest */ }
      }
      if (added.length) { setChars((cs) => [...cs, ...added]); showToast(`✨ Added ${added.map(a => a.name).join(", ")}`); }
      else showToast("Couldn't add suggestions — try again");
    } catch (e: any) { showToast("Couldn't invent: " + e.message); }
    finally { setInventing(false); }
  };
  const create = async () => {
    try {
      await api.createRequest({ child_id: childId, story_type: type.toLowerCase(), character_ids: selChars, special_request: special || null, priority: when, gen_model_override: genOverride || null, continue_series: series, long_form: longForm, ambient });
      showToast("📖 Queued — you'll get a ping when it's ready");
      setSpecial(""); setSelChars([]);
    } catch (e: any) { showToast(e.message); }
  };

  return (
    <div className="card">
      <label className="fl" style={{ marginTop: 2 }}>Who is it for?</label>
      <div className="seg">
        {children.map((c) => <button key={c.id} className={`chip ${childId === c.id ? "sel" : ""}`} onClick={() => setChildId(c.id)}>{c.name}</button>)}
      </div>

      <label className="fl">Characters</label>
      <div className="seg">
        {chars.map((c) => (
          <span key={c.id} className={`cchip ${selChars.includes(c.id) ? "sel" : ""}`}>
            <button className="pick" onClick={() => toggleChar(c.id)} title={c.archetype || ""}>{c.name}{c.archetype ? " ✨" : ""}</button>
            <button className="del" onClick={() => removeChar(c)} aria-label={`Remove ${c.name}`}>✕</button>
          </span>
        ))}
        <button className="chip" onClick={() => setAdding((a) => !a)}>➕ add</button>
        <button className="chip" onClick={invent} disabled={inventing}>{inventing ? "✨ inventing…" : "✨ invent"}</button>
        <button className="chip" onClick={importFromStories}>📚 from past stories</button>
      </div>
      {adding && (
        <div className="seg" style={{ marginTop: 10, gap: 8 }}>
          <input className="txt" style={{ flex: "1 1 130px" }} placeholder="Name (original)" value={newName}
            onChange={(e) => setNewName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addChar()} />
          <input className="txt" style={{ flex: "1 1 130px" }} placeholder="archetype (optional)" value={newArch}
            onChange={(e) => setNewArch(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addChar()} />
          <button className="hear" onClick={addChar} aria-label="Save character">✓</button>
        </div>
      )}
      {!chars.length && !adding && <p className="muted-note" style={{ marginTop: 6 }}>No characters yet — tap ➕ add or ✨ invent, or the story will star just {children.find(c => c.id === childId)?.name || "your child"}.</p>}

      <label className="fl">Story type</label>
      <div className="seg">
        {STORY_TYPES.map((t) => <button key={t} className={`chip ${type === t ? "sel" : ""}`} onClick={() => setType(t)}>{t}</button>)}
      </div>

      <label className="fl">Special request <span style={{ textTransform: "none", fontWeight: 700 }}>(optional)</span></label>
      <textarea rows={2} placeholder="e.g. a story about sharing a new toy" value={special} onChange={(e) => setSpecial(e.target.value)} />

      <label className="fl">When</label>
      <div className="seg">
        <button className={`chip ${when === "batch" ? "sel" : ""}`} onClick={() => setWhen("batch")}>🌙 Tomorrow night</button>
        <button className={`chip ${when === "now" ? "sel" : ""}`} onClick={() => setWhen("now")}>⚡ Make it now</button>
      </div>

      <div className="kidrow" style={{ marginTop: 14 }}>
        <span className="muted-note" style={{ flex: 1 }}>⭐ Continue the series
          {series && (bible?.characters?.length
            ? <span> — with {bible.characters.map(c => c.name).join(", ")}</span>
            : <span> — the recurring cast grows from this story on</span>)}
        </span>
        <div className={`toggle ${series ? "on" : ""}`} onClick={() => setSeries(s => !s)}><b /></div>
      </div>

      <div className="kidrow" style={{ marginTop: 12 }}>
        <span className="muted-note" style={{ flex: 1 }}>📖 Long story (~10 min, chaptered)
          {longForm && <span> — best made overnight; takes a few minutes</span>}
        </span>
        <div className={`toggle ${longForm ? "on" : ""}`} onClick={() => setLongForm(v => !v)}><b /></div>
      </div>

      <label className="fl">🌧 Rain sound</label>
      <div className="seg">
        {([["off", "Off"], ["under", "Under the story"], ["tail", "At the end"]] as const).map(([v, lbl]) =>
          <button key={v} className={`chip ${ambient === v ? "sel" : ""}`} onClick={() => setAmbient(v)}>{lbl}</button>)}
      </div>

      {isAdmin && <p className="muted-note" style={{ marginTop: 16, cursor: "pointer" }} onClick={() => setAdvanced(a => !a)}>
        {advanced ? "▾" : "▸"} Advanced — generate with a specific model
      </p>}
      {isAdmin && advanced && (
        <div className="seg" style={{ marginTop: 4 }}>
          <select className="txt" value={genOverride} onChange={(e) => setGenOverride(e.target.value)}>
            <option value="">Default (Admin's active model)</option>
            {(models?.local || []).map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
            {(models?.cloud || []).map((m) => <option key={m.id} value={m.id}>{m.id} (cloud)</option>)}
          </select>
        </div>
      )}

      <button className="btn primary" onClick={create} disabled={!childId}>Create story</button>
    </div>
  );
}

// ---------------- Names ----------------
function Names({ showToast }: { showToast: (m: string) => void }) {
  const [prons, setProns] = useState<{ id: string; surface: string; ipa: string | null }[]>([]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [newName, setNewName] = useState("");
  const load = () => api.pronunciations().then((p) => setProns(p as any)).catch(() => {});
  useEffect(() => { load(); }, []);

  const hear = (surface: string, ipa?: string | null) => {
    fetch(api.previewPronUrl(), { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ surface, ipa: ipa || undefined }) })
      .then((r) => r.blob()).then((b) => new Audio(URL.createObjectURL(b)).play()).catch(() => {});
    showToast(`🔊 ${surface}`);
  };
  const save = async (surface: string, ipa: string) => {
    try { await api.savePron({ surface, ipa, source: "manual" }); showToast("✓ Pronunciation saved"); load(); }
    catch (e: any) { showToast(e.message); }
  };

  return (
    <div className="card">
      <h3 style={{ marginBottom: 2 }}>Say names the right way</h3>
      <p className="muted-note" style={{ marginBottom: 14 }}>
        Type a name, tap ▶ to hear it, adjust until it sounds right. Saved per family.
        This fixes the <i>sounds</i>, not the narrator's accent.
      </p>
      {prons.map((p) => (
        <div key={p.id} style={{ marginBottom: 18 }}>
          <div className="pron">
            <input className="txt" value={draft[p.id] ?? (p.ipa || "")} placeholder="IPA e.g. ˈmaɪloʊ"
              onChange={(e) => setDraft({ ...draft, [p.id]: e.target.value })} />
            <button className="hear" onClick={() => hear(p.surface, draft[p.id] ?? p.ipa)}>▶</button>
          </div>
          <p className="muted-note">{p.surface} — stored: <span className="ipa">{p.ipa || "—"}</span>{" "}
            <button className="chip" style={{ padding: "3px 10px" }} onClick={() => save(p.surface, draft[p.id] ?? (p.ipa || ""))}>Save</button></p>
        </div>
      ))}
      <div className="pron" style={{ marginTop: 8 }}>
        <input className="txt" placeholder="Add a name…" value={newName} onChange={(e) => setNewName(e.target.value)} />
        <button className="hear" onClick={() => newName && hear(newName)}>▶</button>
      </div>
      <button className="btn primary" disabled={!newName} onClick={() => { save(newName, ""); setNewName(""); }}>Add name</button>
    </div>
  );
}

// ---------------- Kids ----------------
// ---------------- Parent-voice cloning ----------------
function VoiceCloner({ voices, reload, delVoice, showToast, count }:
  { voices: VoiceProfile[]; reload: () => void; delVoice: (v: VoiceProfile) => void; showToast: (m: string) => void; count: number }) {
  const [script, setScript] = useState("");
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [consent, setConsent] = useState(false);
  const [blob, setBlob] = useState<Blob | null>(null);
  const [recording, setRecording] = useState(false);
  const [secs, setSecs] = useState(0);
  const [busy, setBusy] = useState(false);
  const recRef = useRef<MediaRecorder | null>(null);
  const timerRef = useRef<any>(null);
  useEffect(() => { api.voices().then((r) => setScript(r.reading_script)).catch(() => {}); }, []);

  const startRec = async () => {
    setBlob(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream);
      const chunks: BlobPart[] = [];
      mr.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      mr.onstop = () => { setBlob(new Blob(chunks, { type: mr.mimeType || "audio/webm" })); stream.getTracks().forEach((t) => t.stop()); clearInterval(timerRef.current); setRecording(false); };
      mr.start(); recRef.current = mr; setRecording(true); setSecs(0);
      timerRef.current = setInterval(() => setSecs((x) => { const n = x + 1; if (n >= 75 && recRef.current?.state === "recording") recRef.current.stop(); return n; }), 1000);
    } catch { showToast("Microphone blocked — allow mic access, or use Upload a file"); }
  };
  const stopRec = () => { if (recRef.current?.state === "recording") recRef.current.stop(); };
  const onFile = (e: any) => { const f = e.target.files?.[0]; if (f) { setBlob(f); setSecs(0); } };
  const save = async () => {
    if (!blob || !consent || !label.trim()) { showToast("Add a name, record/upload audio, and tick consent"); return; }
    setBusy(true);
    const form = new FormData();
    form.append("file", blob, "voice.webm");
    form.append("label", label.trim());
    form.append("consent", "true");
    try { await api.uploadVoice(form); showToast("✓ Voice saved — assign it to a child in Edit"); setBlob(null); setLabel(""); setConsent(false); setOpen(false); reload(); }
    catch (e: any) { showToast("Couldn't save: " + String(e.message).slice(0, 90)); }
    finally { setBusy(false); }
  };

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>🎙 Parent voices <span className="muted-note">· {count}</span></h3>
      <p className="muted-note">Record your own voice so stories can be read by <b>you</b>. Assign it to a child in their <b>Edit</b> panel.</p>
      {voices.map((v) => (
        <div key={v.id} className="kidrow" style={{ marginTop: 8 }}>
          <div style={{ flex: 1 }}><b>{v.label}</b> <span className="muted-note">{v.kind === "clone" ? "your recording" : v.kind}</span></div>
          <button className="chip" style={{ padding: "3px 10px" }} onClick={() => delVoice(v)}>✕</button>
        </div>
      ))}
      {!open ? (
        <button className="btn primary" onClick={() => setOpen(true)}>+ Add a parent voice</button>
      ) : (
        <div style={{ marginTop: 10 }}>
          <p className="muted-note" style={{ fontWeight: 700, marginBottom: 6 }}>Read this slowly and warmly, like a bedtime story — quiet room, phone ~20 cm away. <b>15–25 seconds is plenty (max 30 s).</b></p>
          <div style={{ background: "rgba(255,255,255,.06)", borderRadius: 14, padding: "12px 14px", fontSize: 14, lineHeight: 1.55, color: "#dfe2ff", maxHeight: 200, overflow: "auto", whiteSpace: "pre-wrap" }}>{script || "…"}</div>
          <label className="fl">Voice name</label>
          <input className="txt" placeholder="e.g. Mom, Dad" value={label} onChange={(e) => setLabel(e.target.value)} />
          <div className="actions" style={{ marginTop: 12 }}>
            {!recording
              ? <button className="btn approve" onClick={startRec}>🎙 Record</button>
              : <button className="btn reject" onClick={stopRec}>⏹ Stop · {secs}s</button>}
            <label className="btn listen" style={{ textAlign: "center", cursor: "pointer" }}>Upload file
              <input type="file" accept="audio/*" style={{ display: "none" }} onChange={onFile} /></label>
          </div>
          {blob && !recording && <p className="muted-note" style={{ marginTop: 8 }}>✓ Audio ready ({Math.round(blob.size / 1024)} KB{secs ? `, ${secs}s` : ""})</p>}
          <p className="muted-note" style={{ marginTop: 10 }}>
            <label><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />{" "}
              I consent to cloning this voice for private family use, and I own or have permission to use it.</label>
          </p>
          <div className="actions">
            <button className="btn reject" onClick={() => { setOpen(false); setBlob(null); }}>Cancel</button>
            <button className="btn approve" disabled={!blob || !consent || !label.trim() || busy} onClick={save}>{busy ? "Saving…" : "Save voice"}</button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------- Kids ----------------
// Per-child recurring series cast. Auto-grows from "Continue the series" stories; this
// editor lets the parent curate — remove a one-off that crept in, add one, or note a fact.
function SeriesEditor({ childId, showToast }: { childId: string; showToast: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [b, setB] = useState<WorldBible | null>(null);
  const [name, setName] = useState(""); const [desc, setDesc] = useState("");
  const load = () => api.getWorldBible(childId).then(setB).catch(() => {});
  useEffect(() => { if (open && !b) load(); }, [open]);
  const save = async (nb: WorldBible) => {
    try { const r = await api.putWorldBible(childId, nb); setB(r); showToast("⭐ Series saved"); }
    catch (e: any) { showToast(e.message); }
  };
  const removeChar = (n: string) => b && save({ ...b, characters: b.characters.filter(c => c.name !== n) });
  const addChar = () => {
    if (!name.trim() || !b) return;
    save({ ...b, characters: [...b.characters, { name: name.trim(), description: desc.trim() }] });
    setName(""); setDesc("");
  };
  const count = b?.characters?.length || 0;
  return (
    <div style={{ marginTop: 8 }}>
      <p className="muted-note" style={{ cursor: "pointer" }} onClick={() => setOpen(o => !o)}>
        {open ? "▾" : "▸"} ⭐ Series characters{count ? ` (${count})` : ""}
      </p>
      {open && b && (
        <div style={{ marginTop: 4 }}>
          {!b.characters.length && <p className="muted-note">None yet — tick “Continue the series” when making a story and the cast collects here.</p>}
          <div className="seg" style={{ flexWrap: "wrap", gap: 6 }}>
            {b.characters.map((ch) => (
              <span key={ch.name} className="cchip" title={ch.description}>{ch.name}
                <button onClick={() => removeChar(ch.name)} aria-label={`Remove ${ch.name}`}>✕</button>
              </span>
            ))}
          </div>
          <div className="kidrow" style={{ marginTop: 8, gap: 6, flexWrap: "wrap" }}>
            <input className="txt" style={{ flex: "1 1 110px" }} placeholder="name" value={name}
              onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addChar()} />
            <input className="txt" style={{ flex: "1 1 150px" }} placeholder="description (optional)" value={desc}
              onChange={(e) => setDesc(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addChar()} />
            <button className="hear" onClick={addChar} aria-label="Add series character">✓</button>
          </div>
        </div>
      )}
    </div>
  );
}

function Kids({ children, reload, showToast }: { children: Child[]; reload: () => void; showToast: (m: string) => void }) {
  const [banned, setBanned] = useState<{ id: string; term: string; replacement: string | null }[]>([]);
  const [term, setTerm] = useState(""); const [rep, setRep] = useState("");
  // edit + add child state
  const [editId, setEditId] = useState<string>("");
  const [ef, setEf] = useState<{ name: string; age_band: string; pronouns: string; notes: string }>({ name: "", age_band: "early", pronouns: "", notes: "" });
  const [addOpen, setAddOpen] = useState(false);
  const [af, setAf] = useState<{ name: string; age_band: string; pronouns: string }>({ name: "", age_band: "early", pronouns: "" });
  // co-parents
  const [coparents, setCoparents] = useState<CoParent[]>([]);
  const [cp, setCp] = useState<{ email: string; password: string; display_name: string }>({ email: "", password: "", display_name: "" });
  const [cpOpen, setCpOpen] = useState(false);
  const loadCo = () => api.coparents().then(setCoparents).catch(() => {});
  // voices (parent-voice cloning)
  const [voices, setVoices] = useState<VoiceProfile[]>([]);
  const loadVoices = () => api.voices().then((r) => setVoices(r.voices)).catch(() => {});
  useEffect(() => { api.banned().then(setBanned).catch(() => {}); loadCo(); loadVoices(); }, []);
  const assignVoice = async (c: Child, vid: string) => {
    try { await api.patchChild(c.id, { narrator_voice: vid || "default" }); reload(); showToast("✓ Voice set"); }
    catch (e: any) { showToast("Couldn't set voice: " + e.message); }
  };
  const addCoparent = async () => {
    if (!cp.email.trim() || cp.password.length < 8) { showToast("Email + 8-char password needed"); return; }
    try { await api.addCoparent({ email: cp.email.trim(), password: cp.password, display_name: cp.display_name.trim() || null }); setCp({ email: "", password: "", display_name: "" }); setCpOpen(false); loadCo(); showToast("✓ Co-parent added — they can sign in now"); }
    catch (e: any) { showToast("Couldn't add: " + String(e.message).replace(/^\d+\s*/, "")); }
  };

  const rotateLink = async (c: Child) => {
    if (!confirm(`Make a new player link for ${c.name}? The old link — and any Tellybear device set up with it — will stop working.`)) return;
    try { await api.rotateSlug(c.id); reload(); showToast("✓ New link created"); } catch (e: any) { showToast(e.message); }
  };
  const toggle = async (c: Child) => { try { await api.patchChild(c.id, { active: !c.active }); reload(); } catch (e: any) { showToast(e.message); } };
  const startEdit = (c: Child) => { setEditId(c.id); setEf({ name: c.name, age_band: c.age_band, pronouns: c.pronouns || "", notes: c.notes || "" }); };
  const saveEdit = async (c: Child) => {
    if (!ef.name.trim()) return;
    try { await api.patchChild(c.id, { name: ef.name.trim(), age_band: ef.age_band, pronouns: ef.pronouns.trim() || null, notes: ef.notes.trim() || null }); setEditId(""); reload(); showToast("✓ Saved"); }
    catch (e: any) { showToast("Couldn't save: " + e.message); }
  };
  const addChild = async () => {
    if (!af.name.trim()) return;
    try { await api.addChild({ name: af.name.trim(), age_band: af.age_band, pronouns: af.pronouns.trim() || null, active: true }); setAf({ name: "", age_band: "early", pronouns: "" }); setAddOpen(false); reload(); showToast(`✓ Added ${af.name.trim()}`); }
    catch (e: any) { showToast("Couldn't add: " + e.message); }
  };
  const delChild = async (c: Child) => {
    if (!confirm(`Delete ${c.name}? This permanently removes ${c.name} and ALL of their stories and audio. This cannot be undone.`)) return;
    try { await api.delChild(c.id); setEditId(""); reload(); showToast(`Deleted ${c.name}`); }
    catch (e: any) { showToast("Couldn't delete: " + e.message); }
  };
  const addBanned = async () => {
    if (!term) return;
    try { await api.addBanned({ term, replacement: rep || null }); setTerm(""); setRep(""); api.banned().then(setBanned); showToast("Saved"); }
    catch (e: any) { showToast(e.message); }
  };
  const delBanned = async (id: string) => { await api.delBanned(id); api.banned().then(setBanned); };

  return (<>
    {children.map((c) => (
      <div className="card" key={c.id}>
        <div className="kidrow">
          <div className="av" style={{ background: "linear-gradient(150deg,#3a3f86,#5b4fa0)" }}>{c.active ? "🧒" : "👶"}</div>
          <div style={{ flex: 1, minWidth: 0 }}><h3 style={{ margin: 0 }}>{c.name}</h3>
            <div className="muted-note">band {c.age_band === "early" ? "3–5" : "6–8"} · {c.active ? "active" : "inactive · usable as a character"}</div></div>
          <button className="chip" style={{ padding: "4px 10px", flex: "none" }} onClick={() => editId === c.id ? setEditId("") : startEdit(c)}>{editId === c.id ? "Close" : "Edit"}</button>
          <div className={`toggle ${c.active ? "on" : ""}`} title="active" onClick={() => toggle(c)}><b /></div>
        </div>
        {editId === c.id && (
          <div style={{ marginTop: 12 }}>
            <label className="fl">Name</label>
            <input className="txt" value={ef.name} onChange={(e) => setEf({ ...ef, name: e.target.value })} />
            <label className="fl">Age band</label>
            <div className="seg">
              {[["early", "3–5"], ["older", "6–8"]].map(([b, l]) => (
                <button key={b} className={`chip ${ef.age_band === b ? "sel" : ""}`} onClick={() => setEf({ ...ef, age_band: b })}>{l}</button>
              ))}
            </div>
            <label className="fl">Pronouns <span style={{ textTransform: "none", fontWeight: 700 }}>(optional)</span></label>
            <input className="txt" placeholder="e.g. he/him" value={ef.pronouns} onChange={(e) => setEf({ ...ef, pronouns: e.target.value })} />
            <label className="fl">About {ef.name || "them"} <span style={{ textTransform: "none", fontWeight: 700 }}>(optional — goes into every story)</span></label>
            <textarea className="txt" rows={3} maxLength={500} placeholder="e.g. loves trains and dinosaurs, just started school, a bit scared of thunder"
              value={ef.notes} onChange={(e) => setEf({ ...ef, notes: e.target.value })} />
            <button className="btn primary" style={{ marginTop: 12 }} onClick={() => saveEdit(c)}>Save changes</button>
            <p className="muted-note" style={{ marginTop: 14, color: "var(--peach)", cursor: "pointer", textAlign: "center" }}
               onClick={() => delChild(c)}>🗑 Delete {c.name} permanently</p>
          </div>
        )}
        <div className="kidrow" style={{ marginTop: 12, gap: 8 }}>
          <span className="muted-note" style={{ flex: "none" }}>🎙 Story voice</span>
          <select className="txt" style={{ flex: 1, padding: "8px 34px 8px 10px" }} value={c.narrator_voice || ""}
            onChange={(e) => assignVoice(c, e.target.value)}>
            <option value="">Default — a different voice each story</option>
            {voices.map((v) => <option key={v.id} value={v.id}>{v.label}{v.kind === "clone" ? " (your recording)" : ""}</option>)}
          </select>
        </div>
        <div className="kidrow" style={{ marginTop: 10, gap: 8 }}>
          <span className="muted-note" style={{ flex: "none" }}>🗣 Language</span>
          <select className="txt" style={{ flex: 1, padding: "8px 34px 8px 10px" }} value={c.language || "en"}
            onChange={async (e) => {
              try { await api.patchChild(c.id, { language: e.target.value }); reload(); showToast("✓ Language set"); }
              catch (err: any) { showToast("Couldn't set language: " + err.message); }
            }}>
            <option value="en">English</option>
            <option value="hi">Hindi / Hinglish</option>
          </select>
        </div>
        <SeriesEditor childId={c.id} showToast={showToast} />
        <div className="actions" style={{ marginTop: 10 }}>
          <a className="btn listen" style={{ textDecoration: "none", textAlign: "center" }}
             href={playerUrl(c.public_slug)} target="_blank" rel="noopener">▶ Open {c.name}'s player</a>
          <button className="btn listen" onClick={() => copyText(playerUrl(c.public_slug), showToast)}>Copy link</button>
          <button className="btn listen" onClick={() => rotateLink(c)}>New link</button>
        </div>
      </div>
    ))}

    <VoiceCloner reload={loadVoices} showToast={showToast} count={voices.length} delVoice={async (v) => { if (confirm(`Delete "${v.label}"?`)) { await api.delVoice(v.id); loadVoices(); reload(); } }} voices={voices} />

    {/* Add a child */}
    {addOpen ? (
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Add a child</h3>
        <label className="fl">Name</label>
        <input className="txt" placeholder="Their name (used inside stories)" value={af.name} onChange={(e) => setAf({ ...af, name: e.target.value })} />
        <label className="fl">Age band</label>
        <div className="seg">
          {[["early", "3–5"], ["older", "6–8"]].map(([b, l]) => (
            <button key={b} className={`chip ${af.age_band === b ? "sel" : ""}`} onClick={() => setAf({ ...af, age_band: b })}>{l}</button>
          ))}
        </div>
        <label className="fl">Pronouns <span style={{ textTransform: "none", fontWeight: 700 }}>(optional)</span></label>
        <input className="txt" placeholder="e.g. she/her" value={af.pronouns} onChange={(e) => setAf({ ...af, pronouns: e.target.value })} />
        <div className="actions" style={{ marginTop: 10 }}>
          <button className="btn reject" onClick={() => setAddOpen(false)}>Cancel</button>
          <button className="btn approve" disabled={!af.name.trim()} onClick={addChild}>Add child</button>
        </div>
      </div>
    ) : (
      <div className="card" style={{ textAlign: "center", color: "var(--muted)", fontWeight: 700, cursor: "pointer" }} onClick={() => setAddOpen(true)}>+ Add a child</div>
    )}

    {/* Family — co-parents share these kids, stories & approvals */}
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Family</h3>
      <p className="muted-note" style={{ marginBottom: 10 }}>Everyone here shares these children, their stories and approvals.</p>
      {coparents.map((p) => (
        <div key={p.id} className="kidrow" style={{ marginTop: 6 }}>
          <span style={{ flex: 1, minWidth: 0 }}><b>{p.display_name}</b>{p.is_admin ? " · admin" : ""}<div className="muted-note">{p.email}</div></span>
        </div>
      ))}
      {cpOpen ? (
        <div style={{ marginTop: 10 }}>
          <label className="fl" style={{ marginTop: 0 }}>Their name</label>
          <input className="txt" placeholder="e.g. Maya" value={cp.display_name} onChange={(e) => setCp({ ...cp, display_name: e.target.value })} />
          <label className="fl">Email</label>
          <input className="txt" type="email" autoCapitalize="off" placeholder="them@example.com" value={cp.email} onChange={(e) => setCp({ ...cp, email: e.target.value })} />
          <label className="fl">Temporary password <span style={{ textTransform: "none", fontWeight: 700 }}>(8+ chars — share it with them)</span></label>
          <input className="txt" placeholder="at least 8 characters" value={cp.password} onChange={(e) => setCp({ ...cp, password: e.target.value })} />
          <div className="actions" style={{ marginTop: 10 }}>
            <button className="btn reject" onClick={() => setCpOpen(false)}>Cancel</button>
            <button className="btn approve" onClick={addCoparent}>Add co-parent</button>
          </div>
        </div>
      ) : (
        <button className="btn primary" onClick={() => setCpOpen(true)}>+ Add a co-parent</button>
      )}
    </div>

    <div className="card">
      <h3>Banned words</h3>
      <p className="muted-note">word → replacement (leave replacement empty to forbid entirely)</p>
      {banned.map((b) => (
        <div key={b.id} className="kidrow" style={{ marginTop: 8 }}>
          <div><b>{b.term}</b> → {b.replacement || <i>forbidden</i>}</div>
          <button className="chip" style={{ marginLeft: "auto", padding: "3px 10px" }} onClick={() => delBanned(b.id)}>✕</button>
        </div>
      ))}
      <div className="pron" style={{ marginTop: 10 }}>
        <input className="txt" placeholder="word" value={term} onChange={(e) => setTerm(e.target.value)} />
        <input className="txt" placeholder="replacement" value={rep} onChange={(e) => setRep(e.target.value)} />
      </div>
      <button className="btn primary" onClick={addBanned} disabled={!term}>Add banned word</button>
    </div>
  </>);
}

// ---------------- Admin (owner-only: model & engine control + Library) ----------------
// Audition every predefined voice (15s sample) and keep only the good ones (curated pool).
function VoiceAudition({ showToast }: { showToast: (m: string) => void }) {
  const [voices, setVoices] = useState<{ id: string; enabled: boolean; has_sample: boolean }[]>([]);
  const [enabled, setEnabled] = useState<Set<string>>(new Set());
  const [playing, setPlaying] = useState<string>("");
  useEffect(() => {
    api.adminVoices().then((r) => {
      setVoices(r.voices);
      setEnabled(new Set(r.curated ? r.voices.filter(v => v.enabled).map(v => v.id) : []));
    }).catch(() => {});
  }, []);
  const play = (id: string) => {
    const a = new Audio(api.voiceSampleUrl(id));
    setPlaying(id);
    a.onended = () => setPlaying("");
    a.play().catch(() => { showToast("No sample yet — run gen_voice_samples"); setPlaying(""); });
  };
  const toggle = (id: string) => setEnabled((e) => { const n = new Set(e); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const save = async () => {
    try { await api.setVoicePool([...enabled]); showToast(`✓ Voice pool saved (${enabled.size || "all"})`); }
    catch (e: any) { showToast("Couldn't save: " + e.message); }
  };
  if (!voices.length) return null;
  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>🎚 Audition voices <span className="muted-note">· tick the good ones</span></h3>
      <p className="muted-note" style={{ marginBottom: 8 }}>Stories pick a random voice from the ticked set. None ticked = all are used.</p>
      {voices.map((v) => (
        <div key={v.id} className="kidrow" style={{ marginTop: 6 }}>
          <button className="chip" style={{ padding: "6px 12px", flex: "none" }}
            onClick={() => play(v.id)} disabled={!v.has_sample}>{playing === v.id ? "▶ …" : "▶"}</button>
          <span style={{ flex: 1 }}>{v.id.replace(/\.wav$/, "")}{!v.has_sample && <span className="muted-note"> · no sample</span>}</span>
          <div className={`toggle ${enabled.has(v.id) ? "on" : ""}`} onClick={() => toggle(v.id)}><b /></div>
        </div>
      ))}
      <button className="btn primary" style={{ marginTop: 12 }} onClick={save}>Save voice pool</button>
    </div>
  );
}

function Admin({ showToast, player }: { showToast: (m: string) => void; player: Player }) {
  const [models, setModels] = useState<Models | null>(null);
  const [s, setS] = useState<Settings | null>(null);
  const [loaded, setLoaded] = useState<string>("");
  const [lib, setLib] = useState<LibraryItem[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [logUnit, setLogUnit] = useState(LOG_UNITS[0]);
  const [logLines, setLogLines] = useState(200);
  const [logText, setLogText] = useState("");
  const [logErr, setLogErr] = useState("");
  const [logBusy, setLogBusy] = useState(false);

  const reloadLib = () => { api.library().then(setLib).catch(() => {}); api.stats().then(setStats).catch(() => {}); };
  useEffect(() => {
    api.models().then(setModels).catch(() => {});
    api.getSettings().then(setS).catch(() => {});
    // llama-swap reports which models are loaded; other gateways don't, and then the line is hidden
    api.systemLoaded().then((r: any) => {
      const names = (Array.isArray(r?.running) ? r.running : []).map((m: any) => m?.model ?? m).filter(Boolean);
      setLoaded(names.join(", "));
    }).catch(() => {});
    reloadLib();
  }, []);

  if (!s) return <div className="center">Loading…</div>;
  const localIds = (models?.local || []).map((m) => m.id);
  const cloudIds = (models?.cloud || []).map((m) => m.id);
  const allIds = [...localIds, ...cloudIds];
  const set = (patch: Partial<Settings>) => setS({ ...s, ...patch });
  const twoLocal = s.active_gen_model !== s.active_judge_model
    && localIds.includes(s.active_gen_model) && localIds.includes(s.active_judge_model);

  const save = async () => {
    try {
      const next = await api.putSettings({
        active_gen_model: s.active_gen_model, active_judge_model: s.active_judge_model,
        gen_use_opus: cloudIds.includes(s.active_gen_model),
        judge_use_opus: cloudIds.includes(s.active_judge_model),
        tts_engine: s.tts_engine, tts_voice: s.tts_voice, tts_exaggeration: s.tts_exaggeration,
        daily_story_limit: s.daily_story_limit,
        chatterbox_url: s.chatterbox_url, kokoro_url: s.kokoro_url,
      });
      setS(next); showToast("✓ Settings saved — applies to the next story");
    } catch (e: any) { showToast("Couldn't save: " + e.message); }
  };
  const del = async (it: LibraryItem) => {
    if (!confirm(`Delete "${it.title}"? Removes its audio too.`)) return;
    try { await api.deleteStory(it.id); reloadLib(); showToast("🗑 Deleted"); }
    catch (e: any) { showToast("Couldn't delete: " + e.message); }
  };
  const refreshLogs = async () => {
    setLogBusy(true); setLogErr("");
    try {
      const r = await api.logs(logUnit, logLines);
      setLogText(r.text || "(no log output)");
    } catch (e: any) {
      setLogErr(e.message || "Couldn't load logs");
    } finally { setLogBusy(false); }
  };

  const Sel = ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <select className="txt" value={value} onChange={(e) => onChange(e.target.value)}>
      {!allIds.includes(value) && <option value={value}>{value}</option>}
      {localIds.map((id) => <option key={id} value={id}>{id}</option>)}
      {cloudIds.map((id) => <option key={id} value={id}>{id} (cloud)</option>)}
    </select>
  );

  return (<>
    <div className="card">
      <h3>Models</h3>
      <label className="fl">Generation model</label>
      <Sel value={s.active_gen_model} onChange={(v) => set({ active_gen_model: v })} />
      <label className="fl">Safety / judge model</label>
      <Sel value={s.active_judge_model} onChange={(v) => set({ active_judge_model: v })} />
      {twoLocal && (
        <p className="muted-note" style={{ marginTop: 8 }}>
          ⚠ Two different local models — batch runs them as separate GPU phases (generate-all →
          judge-all) to avoid per-story swaps. Pick a cloud judge to avoid swapping entirely.
        </p>
      )}
      {loaded && <p className="muted-note" style={{ marginTop: 8 }}>Loaded on the GPU now: {loaded}</p>}

      <h3 style={{ marginTop: 18 }}>Voice (TTS)</h3>
      <label className="fl">Engine</label>
      <div className="seg">
        {["chatterbox", "kokoro"].map((e) => (
          <button key={e} className={`chip ${s.tts_engine === e ? "sel" : ""}`} onClick={() => set({ tts_engine: e })}>{e}</button>
        ))}
      </div>
      <label className="fl">Voice</label>
      <input className="txt" value={s.tts_voice} onChange={(e) => set({ tts_voice: e.target.value })}
        placeholder={s.tts_engine === "chatterbox" ? "e.g. Emily.wav" : "e.g. af_bella"} />
      <label className="fl">Warmth (exaggeration) — {s.tts_exaggeration.toFixed(2)}</label>
      <input type="range" min={0} max={1} step={0.05} value={s.tts_exaggeration}
        style={{ width: "100%" }} onChange={(e) => set({ tts_exaggeration: parseFloat(e.target.value) })} />
      <label className="fl">Chatterbox server URL</label>
      <input className="txt" value={s.chatterbox_url} autoCapitalize="off" spellCheck={false}
        onChange={(e) => set({ chatterbox_url: e.target.value })}
        placeholder={s.chatterbox_url_default || "e.g. http://gpu-box:8890"} />
      <label className="fl">Kokoro server URL</label>
      <input className="txt" value={s.kokoro_url} autoCapitalize="off" spellCheck={false}
        onChange={(e) => set({ kokoro_url: e.target.value })}
        placeholder={s.kokoro_url_default || "e.g. http://localhost:8880"} />
      <p className="muted-note" style={{ marginTop: 6 }}>
        Leave a URL empty to use the server's .env setting (shown grayed out).
      </p>
      <TtsCheck showToast={showToast} />
      <label className="fl">Daily story limit</label>
      <input className="txt" type="number" min={1} value={s.daily_story_limit}
        onChange={(e) => set({ daily_story_limit: Math.max(1, Number(e.target.value) || 1) })} />

      <button className="btn primary" onClick={save}>Save settings</button>
    </div>

    <VoiceAudition showToast={showToast} />

    <div className="card">
      <h3>Logs</h3>
      <label className="fl">Service</label>
      <select className="txt" value={logUnit} onChange={(e) => setLogUnit(e.target.value)}>
        {LOG_UNITS.map((u) => <option key={u} value={u}>{u}</option>)}
      </select>
      <label className="fl">Lines</label>
      <input className="txt" type="number" min={1} max={1000} value={logLines}
        onChange={(e) => setLogLines(Math.max(1, Math.min(Number(e.target.value) || 1, 1000)))} />
      <button className="btn primary" onClick={refreshLogs} disabled={logBusy}>{logBusy ? "Loading…" : "Refresh logs"}</button>
      {logErr && <p className="muted-note" style={{ color: "var(--peach)" }}>{logErr}</p>}
      <pre style={{
        margin: "12px 0 0", maxHeight: 360, overflow: "auto", whiteSpace: "pre-wrap",
        background: "rgba(0,0,0,.24)", border: "1px solid rgba(255,255,255,.08)",
        borderRadius: 14, padding: 12, color: "#dfe2ff", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 12, lineHeight: 1.45,
      }}>{logText || "Choose a service and refresh to load recent logs."}</pre>
    </div>

    <div className="card">
      <h3>Library {stats && <span className="muted-note">· {stats.total_ready} ready · {stats.total_failed} failed</span>}</h3>
      {stats && Object.keys(stats.by_gen_model || {}).length > 0 && (
        <p className="muted-note">by model: {Object.entries(stats.by_gen_model).map(([k, v]) => `${k}:${v}`).join("  ")} · by engine: {Object.entries(stats.by_engine).map(([k, v]) => `${k}:${v}`).join("  ")}</p>
      )}
      {stats?.gen_time?.by_model && Object.keys(stats.gen_time.by_model).length > 0 && (
        <p className="muted-note">speed (avg/median): {Object.entries(stats.gen_time.by_model).map(([k, v]: any) => `${k} ${v.avg_s}s/${v.median_s}s`).join("  ")}</p>
      )}
      {!lib.length && <p className="muted-note">No stories yet.</p>}
      {lib.map((it) => (
        <div key={it.id} className="kidrow" style={{ marginTop: 10, alignItems: "flex-start" }}>
          <div style={{ flex: 1 }}>
            <b>{it.favorited && <span title="Favorited" style={{ color: "#ffd166", marginRight: 6 }}>★</span>}{it.title}</b>
            <div className="muted-note">{it.child_name} · {it.gen_model}{it.engine ? ` · ${it.engine}` : ""} · {it.status}
              {it.generation_seconds ? ` · ${Math.round(it.generation_seconds)}s gen` : ""}
              {it.duration_seconds ? ` · ${Math.round(it.duration_seconds / 60)}m` : ""}</div>
            {it.nearest_sim != null && (
              <div className="muted-note" style={{ color: it.nearest_sim >= 0.82 ? "#e0564f" : it.nearest_sim >= 0.75 ? "#d6a23a" : undefined }}>
                similarity {Math.round(it.nearest_sim * 100)}%{it.nearest_title ? ` · closest to "${it.nearest_title}"` : ""}
                {it.nearest_sim >= 0.82 ? " — near-duplicate" : ""}
              </div>
            )}
            <div className="muted-note">
              {it.craft_overall != null ? `craft ${it.craft_overall.toFixed(1)}/10 · ` : ""}
              {it.rating ? `${"★".repeat(it.rating)} · ` : "unrated · "}
              {`▶×${it.play_count}`}
              {it.reject_reason ? ` · rejected: ${it.reject_reason}` : ""}
            </div>
          </div>
          {it.status === "ready" && (
            <button className="chip" style={{ padding: "4px 10px" }}
              onClick={() => player.toggle(it.id, api.audioUrl(it.id))}>{player.playingId === it.id ? "⏸" : "▶"}</button>
          )}
          <button className="chip" style={{ padding: "4px 10px" }} onClick={() => del(it)}>🗑</button>
        </div>
      ))}
    </div>
  </>);
}

// ---------------- Auth (login / signup) ----------------
function TtsCheck({ showToast }: { showToast: (m: string) => void }) {
  const [res, setRes] = useState<{ chatterbox: TtsHealth; kokoro: TtsHealth } | null>(null);
  const [busy, setBusy] = useState(false);
  const check = async () => {
    setBusy(true);
    try { setRes(await api.ttsHealth()); }
    catch (e: any) { showToast("Couldn't check: " + e.message); }
    finally { setBusy(false); }
  };
  const line = (name: string, h: TtsHealth) =>
    !h.url ? `${name}: not configured`
      : h.ok ? `${name}: ✓ reachable${h.model_loaded === false ? " (model loads on demand)" : ""}`
      : `${name}: ✗ can't reach ${h.url}`;
  return (
    <div style={{ marginTop: 8 }}>
      <button className="btn listen" onClick={check} disabled={busy}>
        {busy ? "Checking…" : "Check connection (saved URLs)"}
      </button>
      {res && (
        <p className="muted-note" style={{ marginTop: 6 }}>
          {line("Chatterbox", res.chatterbox)}<br />{line("Kokoro", res.kokoro)}
        </p>
      )}
    </div>
  );
}

function ChangePassword({ showToast }: { showToast: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const close = () => { setOpen(false); setCur(""); setNext(""); setErr(""); };
  const save = async () => {
    setErr("");
    if (next.length < 8) { setErr("New password must be at least 8 characters."); return; }
    setBusy(true);
    try {
      await api.changePassword(cur, next);
      close(); showToast("Password updated ✓");
    } catch (e: any) {
      setErr(String(e?.message || "Couldn't update password").replace(/^\d+\s*/, ""));
    } finally { setBusy(false); }
  };
  return (
    <>
      <button className="chip" style={{ padding: "4px 12px", fontSize: 12 }} onClick={() => setOpen(true)} title="Change your password">🔑</button>
      {open && (
        <div onClick={close} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50, padding: 22 }}>
          <div className="card" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 360, width: "100%" }}>
            <div className="fl" style={{ marginTop: 0, fontWeight: 800, fontSize: 16 }}>Change password</div>
            <label className="fl">Current password</label>
            <input className="txt" type="password" autoFocus value={cur} onChange={(e) => setCur(e.target.value)} placeholder="current password" />
            <label className="fl">New password</label>
            <input className="txt" type="password" value={next} onChange={(e) => setNext(e.target.value)}
              placeholder="at least 8 characters" onKeyDown={(e) => e.key === "Enter" && save()} />
            {err && <p className="muted-note" style={{ color: "var(--peach)", marginTop: 10 }}>⚠ {err}</p>}
            <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
              <button className="btn primary" disabled={busy || !cur || !next} onClick={save} style={{ flex: 1 }}>{busy ? "…" : "Update"}</button>
              <button className="btn" onClick={close} style={{ flex: 1 }}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}


function AuthScreen({ onAuthed }: { onAuthed: (m: Me) => void }) {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [cfg, setCfg] = useState<{ open_signup: boolean } | null>(null);
  useEffect(() => { api.authConfig().then(setCfg).catch(() => {}); }, []);
  const inviteOnly = cfg ? !cfg.open_signup : false;
  const submit = async () => {
    setErr(""); setBusy(true);
    try {
      const m = mode === "login" ? await api.login(email.trim(), password)
                                  : await api.signup(email.trim(), password, name.trim());
      onAuthed(m);
    } catch (e: any) {
      const msg = String(e?.message || "").replace(/^\d+\s*/, "");
      setErr(msg || (mode === "login" ? "Couldn't sign in" : "Couldn't sign up"));
    } finally { setBusy(false); }
  };
  return (
    <div id="parent" style={{ justifyContent: "center" }}>
      <div style={{ padding: "0 22px" }}>
        <div className="brand" style={{ justifyContent: "center", fontSize: 26, marginBottom: 6 }}>
          <span className="dot-moon" /> Tellybear
        </div>
        <p className="center" style={{ padding: "0 0 18px", color: "var(--muted)" }}>
          {mode === "login" ? "Welcome back — sign in to your family." : "Create your family account."}
        </p>
        <div className="card">
          {mode === "signup" && (<>
            <label className="fl" style={{ marginTop: 0 }}>Your name</label>
            <input className="txt" placeholder="e.g. Alex" value={name} onChange={(e) => setName(e.target.value)} />
          </>)}
          <label className="fl" style={{ marginTop: mode === "signup" ? undefined : 0 }}>Email</label>
          <input className="txt" type="email" autoCapitalize="off" placeholder="you@example.com" value={email}
            onChange={(e) => setEmail(e.target.value)} />
          <label className="fl">Password</label>
          <input className="txt" type="password" placeholder={mode === "signup" ? "at least 8 characters" : "your password"}
            value={password} onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()} />
          {err && <p className="muted-note" style={{ color: "var(--peach)", marginTop: 10 }}>⚠ {err}</p>}
          <button className="btn primary" disabled={busy || !email || !password || (mode === "signup" && !name)} onClick={submit}>
            {busy ? "…" : mode === "login" ? "Sign in" : "Create account"}
          </button>
        </div>
        {inviteOnly ? (
          <p className="center" style={{ color: "var(--muted)", fontWeight: 700 }}>
            🔒 Invite-only — ask your family admin to add you.
          </p>
        ) : (
          <p className="center" style={{ color: "var(--muted)", fontWeight: 700 }}>
            {mode === "login" ? "New here? " : "Already have an account? "}
            <a style={{ color: "var(--lav)", cursor: "pointer" }} onClick={() => { setErr(""); setMode(mode === "login" ? "signup" : "login"); }}>
              {mode === "login" ? "Create an account" : "Sign in"}
            </a>
          </p>
        )}
      </div>
    </div>
  );
}
