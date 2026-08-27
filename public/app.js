// Payment routing orchestrator — demo UI.
// Talks to the FastAPI backend at the same origin under /api/.

const $ = (id) => document.getElementById(id);

/* ---------------- language toggle ---------------- */

let lang = 'en';

function initLangToggle() {
  const nodes = [...document.querySelectorAll('[data-es], [data-es-html]')];
  nodes.forEach((n) => {
    if (n.dataset.esHtml !== undefined) n.dataset.enHtml = n.innerHTML;
    else n.dataset.en = n.textContent;
  });
  const btnEN = $('lang-en');
  const btnES = $('lang-es');
  function setLang(l) {
    nodes.forEach((n) => {
      if (n.dataset.esHtml !== undefined) n.innerHTML = l === 'es' ? n.dataset.esHtml : n.dataset.enHtml;
      else n.textContent = l === 'es' ? n.dataset.es : n.dataset.en;
    });
    btnEN.classList.toggle('on', l === 'en');
    btnES.classList.toggle('on', l === 'es');
    btnEN.setAttribute('aria-pressed', String(l === 'en'));
    btnES.setAttribute('aria-pressed', String(l === 'es'));
    document.documentElement.lang = l;
    try { localStorage.setItem('orch-lang', l); } catch {}
    lang = l;
    window.currentLang = l;
    onLangChange(l);
  }
  window.setOrchLang = setLang;
  btnEN.addEventListener('click', () => setLang('en'));
  btnES.addEventListener('click', () => setLang('es'));
  let saved = 'en';
  try { saved = localStorage.getItem('orch-lang') || 'en'; } catch {}
  setLang(saved);
}

let engineDown = false;

// Re-render whatever is already on screen from cached API responses — never re-fetch.
function onLangChange() {
  if (engineDown) $('decision').innerHTML = `<p class="hint on-dark">${esc(t('The engine is not answering.'))}</p>`;
  renderLift();
  if (cases.length) renderCases();
  if (meta) reflectDerivedUI();
  if (lastDecision) renderDecision(lastDecision);
  if (lastSimulate) renderSimulate(lastSimulate);
  if (lastNormalizeResult) renderNormalized(lastNormalizeResult, lastNormalizeSent);
}

/* ---------------- helpers ---------------- */

const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const num = (v) => (typeof v === 'number' && isFinite(v) ? v : null);
const pct = (v) => (num(v) === null ? '—' : (v * 100).toFixed(1) + '%');
const fee = (v) => (num(v) === null ? '—' : (v * 100).toFixed(2) + '%');
const money = (v) =>
  num(v) === null ? '—' : v.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
const count = (v) => (num(v) === null ? '—' : Math.round(v).toLocaleString('en-US'));
const humanize = (s) => String(s ?? '').replace(/_/g, ' ');

async function getJSON(path) {
  const r = await fetch(path, { headers: { Accept: 'application/json' } });
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

async function postJSON(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

function debounce(fn, ms) {
  let t;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}

function showError(el, msg) {
  if (!msg) {
    el.hidden = true;
    el.textContent = '';
    return;
  }
  el.hidden = false;
  el.textContent = msg;
}

function fillSelect(sel, values, labeller) {
  sel.innerHTML = values
    .map((v) => `<option value="${esc(v)}">${esc(labeller ? labeller(v) : v)}</option>`)
    .join('');
}

/* ---------------- copy the UI owns ---------------- */

// One human line per curated case, in the order the API returns them.
const CASE_TITLES = [
  'A plain first attempt, scored on the finest evidence that actually holds up',
  'The same transaction with the fee knob wide open — the cheaper provider wins',
  'Low funds off-session: the retry goes back to the same provider, later',
  'Authentication needed and nobody there: move it to a channel with a user',
  'An error class the machine does not know, on thin evidence besides',
  'Fraud flagged earlier in the chain: a permanent stop',
  'A provider marked down mid-incident, in the thinnest corner of the data',
  'An issuer never seen in training, on an exact amount-band edge',
];

// What the retry state machine does with each normalized class.
const RETRY_POLICY_COPY = {
  insufficient_funds:
    'Funding is an account problem, not a provider problem. Off-session the retry goes back to the same provider on the next billing window, which has the highest marginal recovery of any decline reason. With the customer present, one immediate failover is allowed first.',
  bank_auth_required:
    'Off-session there is no user to complete a step-up, so a blind retry cannot succeed on any provider: the scheduled retries stop and the transaction is rescheduled to a user-present channel with a notification. On checkout or POS the same provider is retried once, with the authentication step prompted.',
  fraud_risk:
    'Hard stop. Marginal recovery on a fraud-flagged card is low and every extra attempt costs a fee, scheme retry-rule risk and customer goodwill. A fraud_risk anywhere in the attempt history kills the chain, not just as the last error.',
  invalid_card_info:
    'Stop, and ask the customer for a new card. Retrying bad card data on a different provider changes nothing about the data.',
  generic_decline:
    'One immediate failover to the next-best provider by expected net — never a blind retry on the provider that just declined. A second consecutive generic decline stops the chain.',
  other:
    'One immediate failover to the next-best provider by expected net. A second consecutive decline in this family stops the chain.',
};
const RETRY_POLICY_DEFAULT =
  'Unrecognized classes degrade to the generic failover policy — one move to the next-best provider — and the reasoning trail names the fallback explicitly instead of silently guessing.';

const CASE_TITLES_ES = [
  'Un primer intento simple, puntuado sobre la evidencia más fina que realmente se sostiene',
  'La misma transacción con la perilla de comisión totalmente abierta — gana el proveedor más barato',
  'Fondos bajos fuera de sesión: el reintento vuelve al mismo proveedor, más tarde',
  'Se necesita autenticación y no hay nadie: se mueve a un canal con usuario presente',
  'Una clase de error que la máquina no conoce, además sobre evidencia escasa',
  'Fraude marcado antes en la cadena: una parada permanente',
  'Un proveedor marcado como caído a media incidencia, en el rincón más escaso de los datos',
  'Un emisor nunca visto en entrenamiento, justo en el borde de una banda de monto',
];

const RETRY_POLICY_COPY_ES = {
  insufficient_funds:
    'El fondeo es un problema de cuenta, no de proveedor. Fuera de sesión, el reintento vuelve al mismo proveedor en la siguiente ventana de cobro, que tiene la recuperación marginal más alta de cualquier razón de rechazo. Con el cliente presente, se permite primero un failover inmediato.',
  bank_auth_required:
    'Fuera de sesión no hay usuario para completar un paso reforzado, así que un reintento ciego no puede tener éxito en ningún proveedor: los reintentos programados se detienen y la transacción se reprograma a un canal con usuario presente con una notificación. En checkout o POS se reintenta una vez con el mismo proveedor, solicitando el paso de autenticación.',
  fraud_risk:
    'Parada dura. La recuperación marginal en una tarjeta marcada por fraude es baja y cada intento extra cuesta comisión, riesgo de regla de reintento del esquema y buena voluntad del cliente. Un fraud_risk en cualquier punto del historial de intentos mata la cadena, no solo como último error.',
  invalid_card_info:
    'Parar, y pedirle al cliente una tarjeta nueva. Reintentar datos de tarjeta incorrectos en otro proveedor no cambia nada sobre los datos.',
  generic_decline:
    'Un failover inmediato al siguiente mejor proveedor por neto esperado — nunca un reintento ciego en el proveedor que acaba de rechazar. Un segundo rechazo genérico consecutivo detiene la cadena.',
  other:
    'Un failover inmediato al siguiente mejor proveedor por neto esperado. Un segundo rechazo consecutivo de esta familia detiene la cadena.',
};
const RETRY_POLICY_DEFAULT_ES =
  'Las clases no reconocidas degradan a la política de failover genérica — un movimiento al siguiente mejor proveedor — y el rastro de razonamiento nombra el fallback explícitamente en vez de adivinar en silencio.';

// Microcopy used inside dynamically rendered HTML. Keys are the English string.
const I18N_ES = {
  'Could not load engine metadata: ': 'No se pudo cargar la metadata del motor: ',
  'The engine is not answering.': 'El motor no está respondiendo.',
  'Curated scenarios are unavailable; the controls below still work.':
    'Los escenarios seleccionados no están disponibles; los controles de abajo siguen funcionando.',
  'pp approval': 'pp aprobación',
  'out-of-sample replay': 'repetición fuera de muestra',
  'Enter an amount greater than zero to re-decide.': 'Ingresa un monto mayor a cero para redecidir.',
  'earlier in this chain: ': 'antes en esta cadena: ',
  'issuer not seen in training': 'emisor no visto en entrenamiento',
  'attempt ': 'intento ',
  'bias ': 'sesgo ',
  ' down': ' caído(s)',
  ' (unrecognized)': ' (no reconocido)',
  'Route to': 'Enrutar a',
  'static default — insufficient data': 'valor por defecto estático — datos insuficientes',
  'Retry plan': 'Plan de reintento',
  'Stop.': 'Detener.',
  'If it fails': 'Si falla',
  retry: 'reintentar',
  'No retry.': 'Sin reintento.',
  'Reasoning trail': 'Rastro de razonamiento',
  observed: 'observado',
  fee: 'comisión',
  level: 'nivel',
  'thin data': 'datos escasos',
  excluded: 'excluido',
  'Could not run the what-if sweep: ': 'No se pudo correr el barrido qué-pasaría-si: ',
  'without ': 'sin ',
  'all up': 'todos activos',
  'Could not load the curated declines: ': 'No se pudieron cargar los rechazos seleccionados: ',
  'Pick a curated decline, or type a code or message of your own.':
    'Elige un rechazo seleccionado, o escribe tu propio código o mensaje.',
  'Could not normalize that decline: ': 'No se pudo normalizar ese rechazo: ',
  'Could not decide this transaction: ': 'No se pudo decidir esta transacción: ',
  'No decision — see the message below.': 'Sin decisión — ver el mensaje abajo.',
  'Normalized to': 'Normalizado a',
  table: 'tabla',
  fallback: 'respaldo',
  'confidence ': 'confianza ',
  'below the 0.60 confidence threshold, or no model available — the retry machine gets the safe default':
    'bajo el umbral de confianza de 0.60, o no hay modelo disponible — la máquina de reintentos recibe el valor seguro por defecto',
  'from ': 'de ',
  'closed vocabulary: ': 'vocabulario cerrado: ',
  'What the retry machine does with it': 'Qué hace la máquina de reintentos con esto',
};

// Translate a UI-owned copy string; data from the API (raw codes, PSP names, etc.) passes through untouched.
const t = (s) => (lang === 'es' ? I18N_ES[s] ?? s : s);

/* ---------------- state ---------------- */

const state = {
  amount: 250,
  bin6: '596546',
  funding: 'debit',
  gateway: 'checkout',
  attempt_number: 1,
  cost_bias: 0,
  psps_down: [],
  error_history: [],
};

let meta = null;
let cases = [];
let activeCase = -1;
let decideSeq = 0;
let simulateSeq = 0;
let lastDecision = null;
let lastSimulate = null;
let lastNormalizeResult = null;
let lastNormalizeSent = null;
let lastLiftPp; // undefined = not loaded yet, null = loaded but no headline figure

const payload = () => ({
  amount: state.amount,
  bin6: state.bin6,
  funding: state.funding,
  gateway: state.gateway,
  attempt_number: state.attempt_number,
  cost_bias: state.cost_bias,
  psps_down: state.psps_down,
  error_history: state.error_history,
});

/* ---------------- boot ---------------- */

boot();

async function boot() {
  initLangToggle();
  wireStaticHandlers();

  const [metaRes, casesRes] = await Promise.allSettled([
    getJSON('/api/meta'),
    getJSON('/api/cases'),
  ]);

  if (metaRes.status !== 'fulfilled') {
    engineDown = true;
    showError($('decide-error'), `${t('Could not load engine metadata: ')}${metaRes.reason.message}`);
    $('decision').innerHTML = `<p class="hint on-dark">${esc(t('The engine is not answering.'))}</p>`;
    $('decision').setAttribute('aria-busy', 'false');
    return;
  }
  meta = metaRes.value;
  buildControls();

  if (casesRes.status === 'fulfilled' && Array.isArray(casesRes.value)) {
    cases = casesRes.value;
    renderCases();
    loadCase(0);
  } else {
    $('cases').innerHTML = `<p class="hint">${esc(t('Curated scenarios are unavailable; the controls below still work.'))}</p>`;
    syncFormFromState();
    runDecide();
    runSimulate();
  }

  loadBacktest();
  loadNormalizeSamples();
}

async function loadBacktest() {
  try {
    const bt = await getJSON('/api/backtest');
    lastLiftPp = num(bt.headline_lift_pp);
  } catch {
    lastLiftPp = null;
  }
  renderLift();
}

function renderLift() {
  if (lastLiftPp === undefined) return;
  if (lastLiftPp !== null) {
    const sign = lastLiftPp > 0 ? '+' : '';
    $('stat-lift').textContent = `${sign}${lastLiftPp.toFixed(1)} ${t('pp approval')}`;
  } else {
    $('stat-lift').textContent = t('out-of-sample replay');
  }
}

/* ---------------- controls ---------------- */

function buildControls() {
  const bins = Array.isArray(meta.sample_bins) ? meta.sample_bins : [];
  $('bin6').innerHTML = bins
    .map((b) => `<option value="${esc(b.bin6)}">${esc(b.bin6)} — ${esc(b.issuer)}</option>`)
    .join('');

  fillSelect($('funding'), meta.funding || []);
  fillSelect($('gateway'), meta.gateways || []);
  fillSelect($('last-error'), meta.error_classes || [], humanize);
  fillSelect($('last-psp'), meta.psps || []);
  fillSelect($('own-psp'), meta.psps || []);

  $('psps-down').innerHTML = (meta.psps || [])
    .map(
      (p) => `<label class="check"><input type="checkbox" name="down" value="${esc(p)}">${esc(p)}</label>`
    )
    .join('');

  $('adjust').addEventListener('input', onControlInput);
  $('adjust').addEventListener('change', onControlInput);
  $('adjust').addEventListener('submit', (e) => e.preventDefault());
}

// /api/meta ships a short curated bin list, so a case (or a hand-typed bin) can
// reference a bin that is not in it. Add it unlabelled — only the engine knows
// whether it resolves to an issuer, and labelBinFromSegment fills that in.
function ensureBinOption(bin6) {
  const sel = $('bin6');
  if (![...sel.options].some((o) => o.value === bin6)) {
    const opt = document.createElement('option');
    opt.value = bin6;
    opt.textContent = bin6;
    sel.appendChild(opt);
  }
}

function labelBinFromSegment(si) {
  const opt = $('bin6').selectedOptions[0];
  if (!opt || opt.value !== state.bin6) return;
  if (si.issuer) opt.textContent = `${state.bin6} — ${si.issuer}`;
  else if (si.issuer_bucket === 'OTHER') opt.textContent = `${state.bin6} — ${t('issuer not seen in training')}`;
}

function onControlInput() {
  const raw = $('amount').value.trim();
  const amount = parseFloat(raw);
  const amountOk = raw !== '' && isFinite(amount) && amount > 0;
  if (amountOk) state.amount = amount;
  $('amount').setAttribute('aria-invalid', String(!amountOk));

  state.bin6 = $('bin6').value;
  state.funding = $('funding').value;
  state.gateway = $('gateway').value;

  const attempt = parseInt($('attempt').value, 10);
  state.attempt_number = isFinite(attempt) && attempt >= 1 ? attempt : 1;

  state.cost_bias = parseFloat($('cost-bias').value);
  state.psps_down = [...$('psps-down').querySelectorAll('input:checked')].map((i) => i.value);

  // The controls edit the LAST decline; anything earlier in the chain is preserved.
  if (state.attempt_number <= 1) {
    state.error_history = [];
  } else {
    const head = state.error_history.slice(0, -1);
    state.error_history = [
      ...head,
      { psp: $('last-psp').value, error_class: $('last-error').value },
    ];
  }

  reflectDerivedUI();
  markCaseDirty();

  // Don't decide on an amount the engine would reject: say so instead of
  // showing a stale decision next to a field the reader just changed.
  if (!amountOk) {
    showError($('decide-error'), t('Enter an amount greater than zero to re-decide.'));
    return;
  }
  scheduleDecide();
  scheduleSimulate();
}

function reflectDerivedUI() {
  const multi = state.attempt_number > 1;
  $('last-error-field').hidden = !multi;
  $('last-psp-field').hidden = !multi;

  $('cost-bias-value').textContent = state.cost_bias.toFixed(2);

  const earlier = state.error_history.slice(0, -1);
  const el = $('earlier-history');
  if (multi && earlier.length) {
    el.hidden = false;
    el.textContent =
      t('earlier in this chain: ') +
      earlier.map((e) => `${e.psp} ${e.error_class}`).join(' · ');
  } else {
    el.hidden = true;
    el.textContent = '';
  }
}

function syncFormFromState() {
  $('amount').value = state.amount;
  ensureBinOption(state.bin6);
  $('bin6').value = state.bin6;
  $('funding').value = state.funding;
  $('gateway').value = state.gateway;
  $('attempt').value = state.attempt_number;
  $('cost-bias').value = state.cost_bias;

  const last = state.error_history[state.error_history.length - 1];
  if (last) {
    if ([...$('last-error').options].some((o) => o.value === last.error_class)) {
      $('last-error').value = last.error_class;
    } else {
      // A class the engine does not key on (e.g. do_not_honor) — keep it visible.
      const opt = document.createElement('option');
      opt.value = last.error_class;
      opt.textContent = `${humanize(last.error_class)}${t(' (unrecognized)')}`;
      $('last-error').appendChild(opt);
      $('last-error').value = last.error_class;
    }
    if (last.psp) $('last-psp').value = last.psp;
  }

  $('psps-down').querySelectorAll('input').forEach((i) => {
    i.checked = state.psps_down.includes(i.value);
  });

  reflectDerivedUI();
}

/* ---------------- cases ---------------- */

function renderCases() {
  $('cases').innerHTML = cases
    .map((c, i) => {
      const txn = c.txn || {};
      const down = c.down || c.psps_down || [];
      const bits = [
        money(num(txn.amount)),
        txn.funding,
        txn.gateway,
        `${t('attempt ')}${txn.attempt_number || 1}`,
        `${t('bias ')}${Number(c.cost_bias || 0).toFixed(2)}`,
      ];
      if (down.length) bits.push(`${down.join(', ')}${t(' down')}`);
      const title = (lang === 'es' ? CASE_TITLES_ES[i] : CASE_TITLES[i]) || String(c.why_interesting || '').split('. ')[0];
      return `<button type="button" class="case" data-i="${i}" role="listitem" aria-pressed="false">
        <span class="case-n">${String(i + 1).padStart(2, '0')}</span>
        <span class="case-title">${esc(title)}</span>
        <span class="case-meta">${esc(bits.join(' · '))}</span>
      </button>`;
    })
    .join('');

  $('cases').addEventListener('click', (e) => {
    const btn = e.target.closest('.case');
    if (btn) loadCase(Number(btn.dataset.i));
  });
}

function loadCase(i) {
  const c = cases[i];
  if (!c) return;
  const t = c.txn || {};

  state.amount = num(t.amount) ?? 250;
  state.bin6 = String(t.bin6 ?? '596546');
  state.funding = t.funding || (meta.funding || [])[0];
  state.gateway = t.gateway || (meta.gateways || [])[0];
  state.attempt_number = t.attempt_number || 1;
  state.cost_bias = Number(c.cost_bias || 0);
  state.psps_down = (c.down || c.psps_down || []).slice();
  state.error_history = (t.error_history || []).map((e) => ({ ...e }));

  activeCase = i;
  document.querySelectorAll('.case').forEach((b) => {
    b.setAttribute('aria-pressed', String(Number(b.dataset.i) === i));
  });

  syncFormFromState();
  runDecide();
  runSimulate();
}

function markCaseDirty() {
  if (activeCase < 0) return;
  activeCase = -1;
  document.querySelectorAll('.case').forEach((b) => b.setAttribute('aria-pressed', 'false'));
}

/* ---------------- decide ---------------- */

const scheduleDecide = debounce(() => runDecide(), 220);
const scheduleSimulate = debounce(() => runSimulate(), 420);

async function runDecide() {
  const seq = ++decideSeq;
  const panel = $('decision');
  panel.setAttribute('aria-busy', 'true');
  try {
    const d = await postJSON('/api/decide', payload());
    if (seq !== decideSeq) return;
    showError($('decide-error'), null);
    lastDecision = d;
    renderDecision(d);
  } catch (err) {
    if (seq !== decideSeq) return;
    lastDecision = null;
    showError($('decide-error'), `${t('Could not decide this transaction: ')}${err.message}`);
    panel.innerHTML = `<p class="hint on-dark">${esc(t('No decision — see the message below.'))}</p>`;
  } finally {
    if (seq === decideSeq) panel.setAttribute('aria-busy', 'false');
  }
}

function renderDecision(d) {
  const eligible = d.eligible_psps || {};
  const rows = Object.entries(eligible).sort(
    (a, b) => (num(b[1].expected_net) ?? 0) - (num(a[1].expected_net) ?? 0)
  );
  const max = Math.max(1e-9, ...rows.map(([, v]) => num(v.expected_net) ?? 0));
  const excluded = (meta.psps || []).filter((p) => !(p in eligible));

  // "L1 (gateway_group/funding/issuer_bucket)" -> "L1", full text on hover.
  const level = (s) => {
    const full = String(s ?? '—');
    return `<span title="${esc(full)}">${esc(full.split(' ')[0])}</span>`;
  };

  const si = d.segment_inputs || {};
  labelBinFromSegment(si);
  const segLine = [si.gateway_group, si.funding, si.issuer_bucket, si.amount_band].filter(Boolean);

  const head = `<div class="route-head">
      <p class="kicker">${esc(t('Route to'))}</p>
      <p class="route-psp"><span>${esc(d.route_psp ?? '—')}</span></p>
      ${segLine.length ? `<p class="seg-line">${esc(segLine.join(' · '))}</p>` : ''}
      ${d.static_default ? `<span class="flag">${esc(t('static default — insufficient data'))}</span>` : ''}
    </div>`;

  const winnerNet = num((eligible[d.route_psp] || {}).expected_net);

  const list = rows
    .map(([psp, v]) => {
      const net = num(v.expected_net) ?? 0;
      const w = Math.max(1, (net / max) * 100);
      // Expected-net spreads are often fractions of a dollar, so the bar alone
      // cannot be read. The gap to the chosen PSP carries the precision.
      const delta =
        psp === d.route_psp || winnerNet === null
          ? ''
          : ` <span class="psp-delta">${net - winnerNet >= 0 ? '+' : '−'}${money(
              Math.abs(net - winnerNet)
            )}</span>`;
      return `<div class="psp ${psp === d.route_psp ? 'win' : ''}">
        <div class="psp-head"><span class="psp-id">${esc(psp)}</span><span class="psp-net">${money(net)}${delta}</span></div>
        <div class="bar"><span style="width:${w.toFixed(1)}%"></span></div>
        <dl class="psp-stats">
          <div><dt>wilson</dt><dd>${pct(num(v.p_wilson))}</dd></div>
          <div><dt>${esc(t('observed'))}</dt><dd>${pct(num(v.p_hat))}</dd></div>
          <div><dt>${esc(t('fee'))}</dt><dd>${fee(num(v.fee_pct))}</dd></div>
          <div><dt>${esc(t('level'))}</dt><dd>${level(v.segment_used)}</dd></div>
          <div><dt>n</dt><dd>${count(num(v.n_support))}</dd></div>
          ${v.insufficient_data ? `<div><dd class="thin">${esc(t('thin data'))}</dd></div>` : ''}
        </dl>
      </div>`;
    })
    .join('');

  const out = excluded
    .map(
      (p) => `<div class="psp out">
        <div class="psp-head"><span class="psp-id">${esc(p)}</span><span class="psp-net">${esc(t('excluded'))}</span></div>
      </div>`
    )
    .join('');

  const r = d.retry_policy || {};
  let retryLine;
  if (r.stop_reason) {
    retryLine = `<span class="step">${esc(t('Stop.'))}</span> ${esc(r.stop_reason)}`;
  } else if (r.should_retry_on_fail) {
    const cands = (r.next_psp_candidates || []).map((p) => esc(p)).join('<span class="arrow">·</span>');
    retryLine =
      `${esc(t('If it fails'))}<span class="arrow">→</span><span class="step">${esc(t('retry'))}</span>` +
      (cands ? `<span class="arrow">→</span>${cands}` : '') +
      (r.when ? `<span class="arrow">→</span>${esc(r.when)}` : '');
  } else {
    retryLine = `<span class="step">${esc(t('No retry.'))}</span>${r.when ? ' ' + esc(r.when) : ''}`;
  }

  const retry = `<div class="retry">
      <p class="kicker">${esc(t('Retry plan'))}</p>
      <p class="retry-line">${retryLine}</p>
      ${r.note ? `<p class="retry-note">${esc(r.note)}</p>` : ''}
    </div>`;

  const wide = window.matchMedia('(min-width: 1000px)').matches;
  const trail = `<details class="trail" ${wide ? 'open' : ''}>
      <summary>${esc(t('Reasoning trail'))}</summary>
      <ol>${(d.reasoning || []).map((l) => `<li>${esc(l)}</li>`).join('')}</ol>
    </details>`;

  $('decision').innerHTML = head + `<div class="psp-list">${list}${out}</div>` + retry + trail;
}

/* ---------------- simulate ---------------- */

async function runSimulate() {
  const seq = ++simulateSeq;
  try {
    const s = await postJSON('/api/simulate', payload());
    if (seq !== simulateSeq) return;
    showError($('simulate-error'), null);
    lastSimulate = s;
    renderSimulate(s);
  } catch (err) {
    if (seq !== simulateSeq) return;
    lastSimulate = null;
    $('whatif').hidden = true;
    showError($('simulate-error'), `${t('Could not run the what-if sweep: ')}${err.message}`);
  }
}

function cell(k, v, n, changed) {
  return `<div class="cell ${changed ? 'changed' : ''}">
    <span class="cell-k">${esc(k)}</span>
    <span class="cell-v">${esc(v)}</span>
    <span class="cell-n">${esc(n)}</span>
  </div>`;
}

function renderSimulate(s) {
  const sweep = s.cost_bias_sweep || [];
  const baseRoute = sweep.length ? sweep[0].route_psp : null;
  $('sweep').innerHTML = sweep
    .map((x) =>
      cell(
        `${t('bias ')}${Number(x.cost_bias).toFixed(2)}`,
        x.route_psp,
        money(num(x.expected_net)),
        x.route_psp !== baseRoute
      )
    )
    .join('');

  const downs = s.psps_down_scenarios || [];
  $('down-scenarios').innerHTML = downs
    .map((x) => {
      const label = (x.psps_down || []).length ? `${t('without ')}${x.psps_down.join(', ')}` : t('all up');
      return cell(label, x.route_psp, money(num(x.expected_net)), false);
    })
    .join('');

  $('whatif').hidden = !(sweep.length || downs.length);
}

/* ---------------- normalize ---------------- */

let samples = [];

async function loadNormalizeSamples() {
  try {
    samples = await getJSON('/api/normalize/samples');
  } catch (err) {
    showError($('normalize-error'), `${t('Could not load the curated declines: ')}${err.message}`);
    return;
  }
  const byPsp = new Map();
  samples.forEach((s, i) => {
    if (!byPsp.has(s.psp)) byPsp.set(s.psp, []);
    byPsp.get(s.psp).push({ ...s, i });
  });
  $('sample').innerHTML = [...byPsp.entries()]
    .map(
      ([psp, items]) =>
        `<optgroup label="${esc(psp)}">` +
        items
          .map(
            (it) =>
              `<option value="${it.i}">${esc(it.label || `${it.raw_code} — ${it.raw_message}`)}</option>`
          )
          .join('') +
        `</optgroup>`
    )
    .join('');
}

function wireStaticHandlers() {
  // A typed code wins over the curated list, so picking from the list clears it
  // — otherwise a collapsed "type your own" would silently override the choice.
  $('sample').addEventListener('change', () => {
    $('own-code').value = '';
    $('own-message').value = '';
  });

  $('normalize-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const code = $('own-code').value.trim();
    const message = $('own-message').value.trim();
    let body;
    if (code || message) {
      body = { psp: $('own-psp').value, raw_code: code || null, raw_message: message || null };
    } else {
      const s = samples[Number($('sample').value)];
      if (!s) {
        showError($('normalize-error'), t('Pick a curated decline, or type a code or message of your own.'));
        return;
      }
      body = { psp: s.psp, raw_code: s.raw_code, raw_message: s.raw_message };
    }
    try {
      const res = await postJSON('/api/normalize', body);
      showError($('normalize-error'), null);
      lastNormalizeResult = res;
      lastNormalizeSent = body;
      renderNormalized(res, body);
    } catch (err) {
      lastNormalizeResult = null;
      showError($('normalize-error'), `${t('Could not normalize that decline: ')}${err.message}`);
    }
  });
}

function renderNormalized(res, sent) {
  const source = String(res.source || 'fallback');
  const badgeClass =
    source === 'table' ? 'badge-table' : source === 'llm' ? 'badge-llm' : 'badge-fallback';
  const conf = num(res.confidence);
  const badge = `<span class="badge ${badgeClass}">${esc(t(source))}${
    source === 'llm' && res.provider ? ` · ${esc(res.provider)}` : ''
  }</span>`;
  const confLine = conf !== null ? ` <span class="mono badge-conf">${esc(t('confidence '))}${conf.toFixed(2)}</span>` : '';

  const raw = [sent.psp, sent.raw_code, sent.raw_message].filter(Boolean).join(' · ');
  const policy = (lang === 'es' ? RETRY_POLICY_COPY_ES[res.error_class] : RETRY_POLICY_COPY[res.error_class])
    || (lang === 'es' ? RETRY_POLICY_DEFAULT_ES : RETRY_POLICY_DEFAULT);
  const vocab = Array.isArray(res.error_class_options) ? res.error_class_options : [];

  $('norm-result').innerHTML = `
    <p class="kicker">${esc(t('Normalized to'))}</p>
    <p class="norm-class">${esc(res.error_class ?? '—')}</p>
    <p>${badge}${confLine}</p>
    ${source === 'fallback' ? `<p class="norm-reason">${esc(t('below the 0.60 confidence threshold, or no model available — the retry machine gets the safe default'))}</p>` : ''}
    <p class="norm-raw">${esc(t('from '))}${esc(raw)}</p>
    ${res.reasoning ? `<p class="norm-reason">${esc(res.reasoning)}</p>` : ''}
    ${
      vocab.length
        ? `<p class="norm-raw" style="margin-top:var(--s3)">${esc(t('closed vocabulary: '))}${esc(vocab.join(' · '))}</p>`
        : ''
    }
    <div class="norm-policy">
      <p class="kicker">${esc(t('What the retry machine does with it'))}</p>
      <p class="norm-reason">${esc(policy)}</p>
    </div>`;
}
