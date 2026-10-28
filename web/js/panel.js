// panel.js — Setup + State tabs (settings, health, persona, emotions).
const $ = (id) => document.getElementById(id);
const ROLES = ['actor', 'director', 'action', 'mod'];
const SOON = ' (Discord mode, coming soon)';   // toggles only the not-yet-ported Discord bot reads
const REPLY_TOGGLES = [
  ['output_in_chat', 'post text replies' + SOON], ['output_in_voice', 'speak replies (TTS)'],
  ['respond_to_text', 'web: respond to typed text'], ['respond_to_voice', 'web: respond to voice'],
  ['safety_filter', 'sensitive-word filter'], ['show_ai_disclaimer', 'show AI disclaimer' + SOON],
  ['intimacy_mode', 'Intimacy mode (owner flirting, 18+)'],
  ['respond_to_bots', 'discord: respond to bots' + SOON],
];

export function tagState(el, ok, text) {
  if (!el) return;
  el.className = 'tag ' + (ok === true ? 'ok' : ok === false ? 'bad' : 'warn');
  el.textContent = text;
}

export class Panels {
  constructor(api, bus) {
    this.api = api; this.bus = bus;
    this.settings = null;
    this.health = null;
    this._wire();
  }

  // ---------- rendering ----------
  renderSettings(s) {
    this.settings = s;
    document.querySelectorAll('input[name=mode]').forEach((r) => { r.checked = r.value === s.mode; });
    $('ol-url').value = s.llm.ollama.url;
    $('oa-url').value = s.llm.openai?.url || '';
    tagState($('oa-key-status'), s._secrets?.OPENAI_COMPAT_API_KEY ? true : null, s._secrets?.OPENAI_COMPAT_API_KEY ? 'key set' : 'no key (ok for local)');
    tagState($('or-status'), s._secrets?.OPENROUTER_API_KEY ? true : false, s._secrets?.OPENROUTER_API_KEY ? 'key set' : 'no key');
    const tb = $('roles-body'); tb.innerHTML = '';
    for (const r of ROLES) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${r}</td>
        <td><select data-role="${r}"><option value="openrouter">OpenRouter</option><option value="ollama">Ollama</option><option value="openai">OpenAI-compatible</option></select></td>
        <td><input data-or="${r}" type="text"></td><td><input data-ol="${r}" type="text"></td><td><input data-oa="${r}" type="text" placeholder="(server default)"></td>`;
      tb.appendChild(tr);
      tr.querySelector('select').value = s.llm.roles[r] || 'openrouter';
      tr.querySelector('[data-or]').value = s.llm.openrouter[`${r}_model`] || '';
      tr.querySelector('[data-ol]').value = s.llm.ollama[`${r}_model`] || '';
      tr.querySelector('[data-oa]').value = s.llm.openai?.[`${r}_model`] || '';
    }
    $('stt-enabled').checked = !!s.stt.enabled; $('stt-url').value = s.stt.url; $('stt-lang').value = s.stt.lang || 'auto';
    $('tts-enabled').checked = !!s.tts.enabled; $('tts-url').value = s.tts.url; $('tts-lang').value = s.tts.language || 'English';
    this._fillChars([s.tts.character_id], s.tts.character_id);
    const tg = $('reply-toggles'); tg.innerHTML = '';
    for (const [k, label] of REPLY_TOGGLES) {
      const l = document.createElement('label'); l.className = 'check';
      l.innerHTML = `<input type="checkbox" data-reply="${k}"> ${label}`;
      l.querySelector('input').checked = !!s.reply[k];
      tg.appendChild(l);
    }
    const act = document.createElement('label'); act.className = 'check';
    act.innerHTML = `<input type="checkbox" id="actions-enabled"> body actions (animation)`;
    act.querySelector('input').checked = !!s.actions.enabled; tg.appendChild(act);
    $('discord-trigger').value = s.reply.discord_trigger || 'smart';
    $('dc-guild').value = s.discord.guild_id || ''; $('dc-channels').value = (s.discord.channel_ids || []).join(', ');
    $('dc-stage').value = s.discord.stage_channel_id || ''; $('dc-error').value = s.discord.error_channel_id || '';
    tagState($('dc-status'), s._secrets?.DISCORD_BOT_TOKEN ? true : null, s._secrets?.DISCORD_BOT_TOKEN ? 'token set' : 'no token');
    this.bus.emit('mode', s.mode);
  }

  _fillChars(list, current) {
    const sel = $('tts-char'); const names = Array.from(new Set([...(list || []), current].filter(Boolean)));
    sel.innerHTML = names.map((n) => `<option value="${n}">${n}</option>`).join('');
    sel.value = current;
  }

  renderHealth(h) {
    this.health = h;
    const g = $('health-grid'); g.innerHTML = '';
    const row = (label, ok, detail) => {
      const d = document.createElement('div'); d.className = 'h';
      d.innerHTML = `<span class="dot ${ok === true ? 'on' : ok === false ? 'off' : 'warn'}"></span><span>${label}</span><small title="${detail || ''}">${detail || ''}</small>`;
      g.appendChild(d);
    };
    const or = h.llm.openrouter, ol = h.llm.ollama;
    if (or.ok) { this.orAccount = { usage: or.usage, limit: or.limit }; tagState($('usage-account'), true, `account: $${(or.usage || 0).toFixed(2)} used${or.limit != null ? ' of $' + or.limit : ''}`); }
    row('OpenRouter', or.configured ? or.ok : null, or.configured ? (or.ok ? (or.label || 'ok') : or.error) : 'no key');
    row('Ollama', ol.ok, ol.ok ? `${ol.models.length} models` : ol.error);
    const oa = h.llm.openai || { ok: false, models: [] };
    if (Object.values(h.llm.roles || {}).includes('openai')) row('OpenAI-compatible', oa.ok, oa.ok ? `${oa.url}${oa.models.length ? ' · ' + oa.models.length + ' models' : ''}` : (oa.error || 'offline'));
    tagState($('oa-status'), oa.configured ? oa.ok : null, oa.ok ? (oa.models.length ? `${oa.models.length} models` : 'online') : (oa.error === 'not used by any role' ? 'not used' : 'offline'));
    row('STT · SenseVoice (MekaHime build)', h.stt.enabled ? h.stt.ok : null, h.stt.enabled ? (h.stt.ok ? h.stt.url : h.stt.error) : 'disabled');
    row('TTS · GPT-SoVITS (MekaHime build)', h.tts.enabled ? h.tts.ok : null, h.tts.enabled ? (h.tts.ok ? `${h.tts.characters.length} voices` : h.tts.error) : 'disabled');
    row('Discord', h.discord.token_set ? (h.discord.running ? true : null) : false, h.discord.token_set ? (h.discord.running ? 'bot running' : 'token set, bot idle') : 'no token');
    row('LLM roles', true, ROLES.map((r) => `${r}:${h.llm.models[r].provider}`).join(' '));
    tagState($('ol-status'), ol.ok, ol.ok ? `${ol.models.length} models` : 'offline');
    tagState($('stt-status'), h.stt.ok, h.stt.ok ? 'online' : 'offline');
    tagState($('tts-status'), h.tts.ok, h.tts.ok ? 'online' : 'offline');
    if (h.tts.characters?.length) this._fillChars(h.tts.characters, this.settings?.tts.character_id || h.tts.character_id);
    if (or.configured) tagState($('or-status'), or.ok, or.ok ? (or.label || 'ok') : 'invalid');
    tagState($('dc-status'), h.discord.token_set ? (h.discord.running ? true : null) : false, h.discord.token_set ? (h.discord.running ? 'running' : 'token set') : 'no token');
    // ollama model datalist for role inputs
    let dl = $('ol-models'); if (!dl) { dl = document.createElement('datalist'); dl.id = 'ol-models'; document.body.appendChild(dl); }
    dl.innerHTML = (ol.models || []).map((m) => `<option value="${m}">`).join('');
    document.querySelectorAll('[data-ol]').forEach((i) => i.setAttribute('list', 'ol-models'));
    let dl2 = $('oa-models'); if (!dl2) { dl2 = document.createElement('datalist'); dl2.id = 'oa-models'; document.body.appendChild(dl2); }
    dl2.innerHTML = (oa.models || []).map((m) => `<option value="${m}">`).join('');
    document.querySelectorAll('[data-oa]').forEach((i) => i.setAttribute('list', 'oa-models'));
  }

  renderState(st) {
    const em = $('emotions'); em.innerHTML = '';
    const entries = Object.entries(st.emotions || {}).sort((a, b) => b[1] - a[1]);
    for (const [k, v] of entries) {
      const d = document.createElement('div'); d.className = 'ebar';
      d.innerHTML = `<span>${k}</span><span class="b"><i style="width:${Math.round(v * 100)}%"></i></span><span class="v">${v.toFixed(2)}</span>`;
      em.appendChild(d);
    }
    tagState($('dominant'), true, `${st.dominant?.emotion} ${st.dominant?.intensity?.toFixed(2)} · arousal ${st.arousal?.toFixed(2)}`);
    const o = st.owner_profile || {};
    $('relationship').textContent = `${o.display_name || 'you'} — ${o.relationship} · favorability ${(o.favorability ?? 0).toFixed(3)} · ${o.message_count || 0} messages · she calls you “${o.nickname || '…'}” · ${st.turns} turns this session · uptime ${Math.round(st.uptime_s || 0)}s${st.halted ? ' · HALTED: ' + st.halt_reason : ''}`;
    if (st.last_turn?.reply) $('lastturn').textContent = `you: ${st.last_turn.text}\nher: ${st.last_turn.reply} [${st.last_turn.emotion}]`;
    if (st.action_history) $('actions-log').textContent = 'actions: ' + st.action_history.join(' → ');
    if (st.usage) this.renderUsage(st.usage);
  }

  renderUsage(u) {
    const fmt = (n) => (n || 0).toLocaleString();
    const usd = (c) => '$' + (c || 0).toFixed(4);
    const row = (label, b) => `<tr><td>${label}</td><td class="num">${fmt(b.calls)}</td><td class="num">${fmt(b.prompt_tokens)}</td><td class="num">${fmt(b.completion_tokens)}</td><td class="cost">${usd(b.cost_usd)}${b.estimated_calls ? '*' : ''}</td></tr>`;
    const s = u.session || {}, l = u.lifetime || {};
    const or = s.openrouter || {}, orL = l.openrouter || {}, ol = s.ollama || {}, olL = l.ollama || {}, oa = s.openai || {}, oaL = l.openai || {};
    const roles = Object.entries(or.by_role || {}).map(([r, b]) => `${r} ${usd(b.cost_usd)} (${fmt(b.prompt_tokens + b.completion_tokens)} tok)`).join(' · ');
    const since = u.session_started ? new Date(u.session_started * 1000).toLocaleTimeString() : '';
    $('usage').innerHTML = `<table>
      <thead><tr><th>OpenRouter</th><th>calls</th><th>in</th><th>out</th><th>cost</th></tr></thead>
      <tbody>${row('session', or)}${row('lifetime', orL)}</tbody>
      <thead><tr><th>Ollama (free)</th><th>calls</th><th>in</th><th>out</th><th></th></tr></thead>
      <tbody>${row('session', { ...ol, cost_usd: 0 }).replace(/\$0\.0000/, '—')}${row('lifetime', { ...olL, cost_usd: 0 }).replace(/\$0\.0000/, '—')}</tbody>
      ${(oa.calls || oaL.calls) ? `<thead><tr><th>OpenAI-compatible (cost not tracked)</th><th>calls</th><th>in</th><th>out</th><th></th></tr></thead>
      <tbody>${row('session', { ...oa, cost_usd: 0 }).replace(/\$0\.0000/, '—')}${row('lifetime', { ...oaL, cost_usd: 0 }).replace(/\$0\.0000/, '—')}</tbody>` : ''}
    </table>
    <div class="roles">${roles ? 'session by role: ' + roles : ''}</div>
    <div class="sub">session since ${since}${u.last ? ` · last call: ${u.last.provider}/${u.last.role} ${fmt(u.last.prompt_tokens)}+${fmt(u.last.completion_tokens)} tok${u.last.cost_usd != null ? ' ' + usd(u.last.cost_usd) : ''}` : ''}${(or.estimated_calls || orL.estimated_calls) ? ' · * includes estimated costs (model gave no cost)' : ''}</div>`;
    if (this.orAccount) tagState($('usage-account'), true, `account: ${usd(this.orAccount.usage)} used${this.orAccount.limit != null ? ' of $' + this.orAccount.limit : ''}`);
  }

  renderPersona(p) {
    $('p-name').value = p.name || ''; $('p-desc').value = p.description || '';
    $('p-traits').value = (p.personality_traits || []).join(', '); $('p-quirks').value = (p.quirks || []).join(', ');
    $('p-base').value = p.base_prompt || '';
  }

  // ---------- wiring ----------
  _wire() {
    const api = this.api, toast = (m, bad) => this.bus.emit('toast', { m, bad });
    document.querySelectorAll('input[name=mode]').forEach((r) => r.addEventListener('change', async () => {
      if (!r.checked) return;
      this.renderSettings(await api.post('/api/settings', { mode: r.value }));
      toast(`mode → ${r.value}`);
    }));
    $('btn-health').onclick = () => this.bus.emit('refresh-health');
    $('btn-or-save').onclick = async () => {
      const key = $('or-key').value.trim();
      await api.post('/api/secrets', { OPENROUTER_API_KEY: key });
      $('or-key').value = ''; toast(key ? 'OpenRouter key saved' : 'OpenRouter key cleared'); this.bus.emit('refresh-all');
    };
    $('btn-ol-test').onclick = async () => {
      await api.post('/api/settings', { llm: { ollama: { url: $('ol-url').value.trim() } } });
      const h = await api.get('/api/llm/ollama/models');
      tagState($('ol-status'), h.ok, h.ok ? `${h.models.length} models` : (h.error || 'offline'));
      toast(h.ok ? `Ollama ok: ${h.models.join(', ') || 'no models yet'}` : `Ollama: ${h.error}`, !h.ok);
    };
    const lite = $('gfx-lite');
    try {
      const q = new URLSearchParams(location.search).get('lite');
      lite.checked = q !== null ? q !== '0' : localStorage.getItem('sakura.lite') === '1';
    } catch { /* storage blocked: leave unchecked */ }
    lite.onchange = () => {
      try {
        localStorage.setItem('sakura.lite', lite.checked ? '1' : '0');
        const u = new URL(location.href); u.searchParams.delete('lite'); location.href = u.toString();   // reload with the new setting
      } catch { toast('Could not save this setting (browser storage is blocked)', true); }
    };
    $('btn-oa-save').onclick = async () => {
      const key = $('oa-key').value.trim();
      await api.post('/api/secrets', { OPENAI_COMPAT_API_KEY: key });
      $('oa-key').value = ''; toast(key ? 'API key saved' : 'API key cleared'); this.bus.emit('refresh-all');
    };
    $('btn-oa-test').onclick = async () => {
      await api.post('/api/settings', { llm: { openai: { url: $('oa-url').value.trim() } } });
      const h = await api.get('/api/llm/openai/models');
      tagState($('oa-status'), h.ok, h.ok ? (h.models.length ? `${h.models.length} models` : 'online') : (h.error || 'offline'));
      toast(h.ok ? `Server ok${h.models.length ? ': ' + h.models.slice(0, 5).join(', ') : ''}` : `OpenAI-compatible: ${h.error}`, !h.ok);
    };
    $('btn-ol-pull').onclick = async () => {
      const m = $('ol-pull').value.trim(); if (!m) return;
      await api.post('/api/llm/ollama/pull', { model: m }); toast(`pulling ${m} in background…`);
    };
    $('btn-llm-save').onclick = async () => {
      const roles = {}, or = {}, ol = { url: $('ol-url').value.trim() }, oa = { url: $('oa-url').value.trim() };
      for (const r of ROLES) {
        roles[r] = document.querySelector(`select[data-role=${r}]`).value;
        or[`${r}_model`] = document.querySelector(`[data-or=${r}]`).value.trim();
        ol[`${r}_model`] = document.querySelector(`[data-ol=${r}]`).value.trim();
        oa[`${r}_model`] = document.querySelector(`[data-oa=${r}]`).value.trim();
      }
      this.renderSettings(await api.post('/api/settings', { llm: { roles, openrouter: or, ollama: ol, openai: oa } }));
      toast('LLM settings saved'); this.bus.emit('refresh-health');
    };
    $('btn-speech-save').onclick = async () => {
      this.renderSettings(await api.post('/api/settings', {
        stt: { enabled: $('stt-enabled').checked, url: $('stt-url').value.trim(), lang: $('stt-lang').value },
        tts: { enabled: $('tts-enabled').checked, url: $('tts-url').value.trim(), character_id: $('tts-char').value, language: $('tts-lang').value },
      }));
      toast('speech settings saved'); this.bus.emit('refresh-health');
    };
    $('btn-tts-test').onclick = async () => {
      toast('synthesizing…');
      const r = await api.post('/api/control/say', { text: $('tts-test-text').value });
      if (!r.ok) toast(`TTS failed: ${r.error}`, true);
    };
    $('btn-reply-save').onclick = async () => {
      const reply = { discord_trigger: $('discord-trigger').value };
      document.querySelectorAll('[data-reply]').forEach((i) => { reply[i.dataset.reply] = i.checked; });
      this.renderSettings(await api.post('/api/settings', { reply, actions: { enabled: $('actions-enabled').checked } }));
      toast('behaviour saved');
    };
    $('btn-dc-save').onclick = async () => {
      const t = $('dc-token').value.trim();
      await api.post('/api/secrets', { DISCORD_BOT_TOKEN: t }); $('dc-token').value = '';
      toast(t ? 'Discord token saved' : 'Discord token cleared'); this.bus.emit('refresh-all');
    };
    $('btn-dc-settings-save').onclick = async () => {
      this.renderSettings(await api.post('/api/settings', { discord: {
        guild_id: $('dc-guild').value.trim(), channel_ids: $('dc-channels').value.split(',').map((s) => s.trim()).filter(Boolean),
        stage_channel_id: $('dc-stage').value.trim(), error_channel_id: $('dc-error').value.trim() } }));
      toast('Discord settings saved');
    };
    $('btn-persona-save').onclick = async () => {
      const cur = (await api.get('/api/persona')).persona || {};
      const persona = { ...cur, name: $('p-name').value.trim() || 'Sakura', description: $('p-desc').value.trim(),
        personality_traits: $('p-traits').value.split(',').map((s) => s.trim()).filter(Boolean),
        quirks: $('p-quirks').value.split(',').map((s) => s.trim()).filter(Boolean), base_prompt: $('p-base').value };
      await api.post('/api/persona', { persona }); toast('persona saved'); this.bus.emit('refresh-all');
    };
    $('btn-reset').onclick = async () => {
      if (!confirm('Wipe all memory, user profiles and emotions?')) return;
      await api.post('/api/control/reset', {}); toast('memory wiped'); this.bus.emit('refresh-all');
    };
    $('btn-unhalt').onclick = async () => { await api.post('/api/control/unhalt', {}); toast('un-halted'); this.bus.emit('refresh-all'); };
    $('btn-usage-reset').onclick = async () => { const r = await api.post('/api/control/usage_reset', {}); this.renderUsage(r.usage); toast('session cost counter reset'); };
  }
}
