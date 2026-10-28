// app.js — wires the stage, the backend (HTTP + /ws), the Play tab and the panels.
import { Avatar } from './avatar.js';
import { Player, Mic } from './audio.js';
import { Panels } from './panel.js';

const $ = (id) => document.getElementById(id);

// ---------- tiny helpers ----------
const api = {
  async get(p) { const r = await fetch(p); return r.json(); },
  async post(p, body) {
    const r = await fetch(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.statusText);
    return j;
  },
  async upload(p, blob, fields = {}) {
    const fd = new FormData(); fd.append('file', blob, 'utterance.wav');
    for (const [k, v] of Object.entries(fields)) fd.append(k, v);
    const r = await fetch(p, { method: 'POST', body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.statusText);
    return j;
  },
};
const bus = {
  _h: {},
  on(ev, fn) { (this._h[ev] = this._h[ev] || []).push(fn); },
  emit(ev, data) { (this._h[ev] || []).forEach((fn) => fn(data)); },
};
function toast(m, bad = false) {
  const d = document.createElement('div'); d.textContent = m; if (bad) d.className = 'bad';
  $('toast').appendChild(d); setTimeout(() => d.remove(), bad ? 7000 : 3500);
}
bus.on('toast', ({ m, bad }) => toast(m, bad));

// ---------- loading screen ----------
// stage weights sum to 1: world (procedural build), avatar (16 MB VRM download+parse), motion (idle clip + bake)
const LOAD_STAGES = { world: [0.12, 'building the street…'], avatar: [0.58, 'loading Sakura…'], motion: [0.30, 'teaching her to breathe…'] };
const loadState = { world: 0, avatar: 0, motion: 0 };
function loadingProgress(stage, frac) {
  if (!(stage in loadState)) return;
  loadState[stage] = Math.max(loadState[stage], Math.min(1, frac));
  const total = Object.entries(LOAD_STAGES).reduce((s, [k, [w]]) => s + w * loadState[k], 0);
  $('loading-bar').style.width = `${Math.round(total * 100)}%`;
  const current = Object.keys(LOAD_STAGES).find((k) => loadState[k] < 1);
  $('loading-step').textContent = current ? `${LOAD_STAGES[current][1]} ${Math.round(total * 100)}%` : 'ready';
}
function loadingDone() { $('loading-bar').style.width = '100%'; $('loading').classList.add('done'); setTimeout(() => { $('loading').remove(); setTimeout(() => showDonate('first'), DONATE_FIRST_MS); }, 900); }

// ---------- donation popup: on load and every 30 minutes of use, unless "never show this again" ----------
const DONATE_FIRST_MS = 2 * 60 * 1000;     // first ask: 2 minutes after she's loaded
const DONATE_EVERY_MS = 30 * 60 * 1000;    // then every 30 minutes of use
function donateNever() { try { return localStorage.getItem('sakura.donateNever') === '1'; } catch (e) { return false; } }
function showDonate(why) {
  if (KIOSK || donateNever()) return;
  const m = $('donate-modal'); if (!m || !m.classList.contains('hidden')) return;
  m.classList.remove('hidden');
  m.dataset.why = why;
}
function hideDonate() { $('donate-modal')?.classList.add('hidden'); }
$('donate-close').onclick = hideDonate;
$('donate-later').onclick = hideDonate;
$('donate-never').onclick = () => { try { localStorage.setItem('sakura.donateNever', '1'); } catch (e) { /* private mode */ } hideDonate(); toast('okay — we won\'t ask again'); };
$('donate-modal').addEventListener('click', (e) => { if (e.target === $('donate-modal')) hideDonate(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideDonate(); });
setInterval(() => { if (!document.hidden) showDonate('interval'); }, DONATE_EVERY_MS);
function loadingFailed(msg) {
  const el = $('loading'); el.classList.add('failed');
  $('loading-title').textContent = 'She could not wake up';
  $('loading-step').textContent = msg;
  const b = document.createElement('button'); b.className = 'retry primary'; b.textContent = 'Retry'; b.onclick = () => location.reload();
  el.querySelector('.box').appendChild(b);
}

// ---------- kiosk mode (?kiosk=1): stage only, no UI, no popups — for clean captures ----------
const KIOSK = new URLSearchParams(location.search).has('kiosk');
if (KIOSK) document.body.classList.add('kiosk');

// ---------- stage / audio ----------
loadingProgress('world', 0);
// No WebGL, no sakura :(
function noAvatar() {
  return { speaking: false, clipNames: [], setEmotion() {}, setMouth() {}, setExpression() {}, playOnce() {}, playChain() {}, load: async () => {} };
}
let avatar;
try {
  avatar = new Avatar($('stage'), { onStatus: (s) => { $('avatar-status').textContent = s; }, onProgress: loadingProgress });
} catch (e) {
  console.error('3D view unavailable:', e);
  avatar = noAvatar();
  $('avatar-status').textContent = '3D view unavailable (WebGL is off or blocked). Chat still works: try another browser or turn on hardware acceleration.';
}
loadingProgress('world', 1);   // the environment is built synchronously in the constructor
const player = new Player();
let mode = 'web';

player.onStart = (meta) => { avatar.speaking = true; $('play-status').textContent = `🔊 ${meta.emotion || ''}`; };
player.onEnd = () => { avatar.speaking = false; $('play-status').textContent = ''; avatar.setMouth(0); };
(function lipsync() { avatar.setMouth(player.playing ? player.level() : 0); requestAnimationFrame(lipsync); })();

// audio unlock on first gesture
const gesture = $('gesture');
async function unlock() { if (await player.unlock()) gesture.classList.add('hidden'); }
document.addEventListener('pointerdown', unlock, { once: false });
player.unlock().then((ok) => { if (!ok) gesture.classList.remove('hidden'); }).catch(() => gesture.classList.remove('hidden'));

// ---------- transcript ----------
const transcript = $('transcript');
function addMsg(kind, text, meta = '', imageUrl = null) {
  const d = document.createElement('div'); d.className = `msg ${kind}`;
  d.textContent = text;
  if (imageUrl) { const img = document.createElement('img'); img.src = imageUrl; d.appendChild(img); }
  if (meta) { const m = document.createElement('span'); m.className = 'meta'; m.textContent = meta; d.appendChild(m); }
  transcript.appendChild(d); transcript.scrollTop = transcript.scrollHeight;
  return d;
}
let thinkingEl = null;

// ---------- sending ----------
async function sendText(text) {
  text = text.trim(); if (!text) return;
  if (mode !== 'web') { toast('Sakura is in Discord mode', true); return; }
  addMsg('user', text);
  $('chat-input').value = '';
  try { await api.post('/api/chat', { text }); }
  catch (e) { toast(`send failed: ${e.message}`, true); }
}
$('btn-send').onclick = () => sendText($('chat-input').value);
$('chat-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') sendText(e.target.value); });

// ---------- microphone ----------
const mic = new Mic({
  onLevel: (l) => { $('mic-level').firstElementChild.style.width = `${Math.round(l * 100)}%`; },
  onSpeechStart: () => { if ($('opt-bargein').checked && player.playing) { player.stop(); avatar.setMouth(0); } $('play-status').textContent = '🎙 listening…'; },
  onState: ({ active, mode: m }) => { $('btn-mic').classList.toggle('active', active && m === 'vad'); $('btn-ptt').classList.toggle('active', active && m === 'ptt'); },
  onUtterance: async (blob, secs) => {
    if (mode !== 'web') return;
    $('play-status').textContent = `⏳ transcribing ${secs.toFixed(1)}s…`;
    try {
      const r = await api.upload('/api/voice', blob);
      if (!r.ok && r.skipped) { $('play-status').textContent = r.skipped === 'no speech' ? '(no speech)' : r.skipped; }
    } catch (e) { toast(`voice failed: ${e.message}`, true); $('play-status').textContent = ''; }
  },
});
$('btn-mic').onclick = async () => {
  if (mic.active && mic.mode === 'vad') { mic.stop(); return; }
  if (mic.active) mic.stop();
  try { await mic.start('vad'); toast('auto-listen on — just talk'); } catch (e) { toast(`mic: ${e.message}`, true); }
};
const ptt = $('btn-ptt');
async function pttDown(e) {
  e.preventDefault();
  if (!mic.active || mic.mode !== 'ptt') { if (mic.active) mic.stop(); try { await mic.start('ptt'); } catch (err) { toast(`mic: ${err.message}`, true); return; } }
  ptt.classList.add('rec'); mic.pttStart();
}
function pttUp() { if (ptt.classList.contains('rec')) { ptt.classList.remove('rec'); mic.pttStop(); } }
ptt.addEventListener('pointerdown', pttDown); ptt.addEventListener('pointerup', pttUp); ptt.addEventListener('pointerleave', pttUp);
document.addEventListener('keydown', (e) => { if (e.code === 'Space' && e.target === document.body && !e.repeat) pttDown(e); });
document.addEventListener('keyup', (e) => { if (e.code === 'Space' && e.target === document.body) pttUp(); });

// ---------- panel show / hide + voice bar status ----------
const panelTab = $('panel-tab');
function setPanelHidden(hidden) {
  document.body.classList.toggle('panel-hidden', hidden);
  panelTab.title = hidden ? 'show the panel' : 'hide the panel';
  panelTab.textContent = hidden ? '❮' : '❯';   // glyph only — the tab itself never mirrors
  try { localStorage.setItem('sakura.panelHidden', hidden ? '1' : '0'); } catch (e) { /* private mode */ }
}
panelTab.onclick = () => setPanelHidden(!document.body.classList.contains('panel-hidden'));
try { if (localStorage.getItem('sakura.panelHidden') === '1') setPanelHidden(true); } catch (e) { /* noop */ }
new MutationObserver(() => { $('voice-status').textContent = $('play-status').textContent; })
  .observe($('play-status'), { childList: true, characterData: true, subtree: true });

// ---------- fullscreen ----------
const fsBtn = $('btn-fullscreen');
const fsEl = document.documentElement;
function isFullscreen() { return !!(document.fullscreenElement || document.webkitFullscreenElement); }
async function toggleFullscreen() {
  try {
    if (isFullscreen()) await (document.exitFullscreen?.() || document.webkitExitFullscreen?.());
    else await (fsEl.requestFullscreen?.({ navigationUI: 'hide' }) || fsEl.webkitRequestFullscreen?.());
  } catch (e) { toast(`fullscreen: ${e.message}`, true); }
}
function syncFullscreenBtn() { const on = isFullscreen(); fsBtn.textContent = on ? '🡼' : '⛶'; fsBtn.title = on ? 'Exit fullscreen (F / Esc)' : 'Fullscreen (F)'; }
fsBtn.onclick = toggleFullscreen;
document.addEventListener('fullscreenchange', syncFullscreenBtn);
document.addEventListener('webkitfullscreenchange', syncFullscreenBtn);
document.addEventListener('keydown', (e) => { if ((e.key === 'f' || e.key === 'F') && e.target === document.body && !e.repeat && !e.metaKey && !e.ctrlKey) toggleFullscreen(); });

// ---------- tabs ----------
document.querySelectorAll('.tab').forEach((b) => b.onclick = () => {
  document.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x === b));
  document.querySelectorAll('.tabpane').forEach((p) => p.classList.toggle('active', p.id === `tab-${b.dataset.tab}`));
  if (b.dataset.tab === 'setup') bus.emit('refresh-health');
  if (b.dataset.tab === 'state') refreshState();
});

// ---------- panels ----------
const panels = new Panels(api, bus);
bus.on('mode', (m) => {
  mode = m;
  $('mode-pill').textContent = m; $('mode-pill').classList.toggle('discord', m === 'discord');
  document.body.classList.toggle('discord', m === 'discord');
  $('play-disabled').classList.toggle('hidden', m !== 'discord');
  if (m === 'discord' && mic.active) mic.stop();
});
async function refreshSettings() { try { panels.renderSettings(await api.get('/api/settings')); } catch (e) { toast(`settings: ${e.message}`, true); } }
async function refreshHealth() { try { panels.renderHealth(await api.get('/api/health')); } catch (e) { toast(`health: ${e.message}`, true); } }
async function refreshState() { try { const st = await api.get('/api/state'); panels.renderState(st); $('brand-name').textContent = st.name || 'Sakura'; } catch (e) { /* offline */ } }
async function refreshPersona() { try { panels.renderPersona((await api.get('/api/persona')).persona || {}); } catch (e) { /* noop */ } }
bus.on('refresh-health', refreshHealth);
bus.on('refresh-all', () => { refreshSettings(); refreshHealth(); refreshState(); refreshPersona(); });

// avatar test controls (State tab)
function fillClipSelect() {
  const sel = $('clip-select'); sel.innerHTML = avatar.clipNames.map((n) => `<option>${n}</option>`).join('');
}
$('btn-clip-play').onclick = () => avatar.playOnce($('clip-select').value, 4);
$('btn-expr').onclick = () => { const n = $('expr-select').value; if (n === 'neutral') avatar.setEmotion('neutral', 0); else avatar.setExpression(n, 1); };

// ---------- websocket ----------
let ws = null, wsTimer = null;
function connectWs() {
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
  ws = new WebSocket(url);
  ws.onopen = () => { $('conn').className = 'dot on'; };
  ws.onclose = () => { $('conn').className = 'dot off'; clearTimeout(wsTimer); wsTimer = setTimeout(connectWs, 2000); };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => { let m; try { m = JSON.parse(ev.data); } catch (e) { return; } handle(m); };
}
function handle(m) {
  switch (m.type) {
    case 'hello':
      $('brand-name').textContent = m.name || 'Sakura';
      if (m.settings) panels.renderSettings(m.settings);
      if (m.state) { panels.renderState(m.state); avatar.setEmotion(m.state.dominant?.emotion, m.state.dominant?.intensity, { pulse: false }); }
      break;
    case 'settings': panels.renderSettings(m.settings); break;
    case 'state': panels.renderState(m.state); avatar.setEmotion(m.state.dominant?.emotion, m.state.dominant?.intensity, { pulse: false }); break;
    case 'thinking':
      if (m.source === 'discord') break;
      thinkingEl = addMsg('sys', '…'); $('play-status').textContent = '💭 thinking…';
      break;
    case 'stt':
      if (m.empty) { $('play-status').textContent = '(no speech)'; break; }
      addMsg('user', m.text, `🎙 ${m.lang || ''} ${m.emotion ? '· ' + m.emotion : ''} · ${m.ms}ms`);
      break;
    case 'reply': {
      if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
      const c = m.content || {}; const emo = c.emotion || 'neutral'; const inten = m.llm_input?.intensity ?? 0.5;
      const meta = [emo, m.model ? `${m.provider}/${m.model}` : '', m.latency_ms ? `${m.latency_ms}ms` : '', m.source === 'discord' ? 'discord' : ''].filter(Boolean).join(' · ');
      addMsg('sakura', c.text, meta, m.image_url || null);
      avatar.setEmotion(emo, inten);
      $('play-status').textContent = '';
      refreshState();
      break;
    }
    case 'audio':
      if ($('opt-autoplay').checked) player.play(m.audio_data, { format: m.audio_format, meta: { emotion: m.emotion, turn_id: m.turn_id } });
      break;
    case 'face':
      if (m.expression) { if (m.expression === 'neutral') avatar.setEmotion('neutral', 0); else avatar.setExpression(m.expression, m.weight ?? 1); }
      else if (m.emotion) avatar.setEmotion(m.emotion, m.intensity ?? 0.8);
      break;
    case 'action_chain':
    case 'followup_action_chain':
      if (m.action_chain?.actions?.length) avatar.playChain(m.action_chain.actions);
      break;
    case 'error':
      if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
      toast(`${m.where}: ${m.error}${m.halted ? ' (halted — see Setup → Danger → Un-halt)' : ''}`, true);
      $('play-status').textContent = '';
      break;
    default: break;
  }
}

// ---------- boot ----------
(async () => {
  connectWs();
  bus.emit('refresh-all');
  try { await avatar.load(); fillClipSelect(); loadingDone(); }
  catch (e) { toast(`avatar: ${e.message}`, true); $('avatar-status').textContent = `avatar failed: ${e.message}`; loadingFailed(e.message); }
  setInterval(refreshState, 15000);
})();
