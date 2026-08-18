// Payment routing orchestrator — demo UI.
// Talks to the FastAPI backend at the same origin under /api/.

const $ = (id) => document.getElementById(id);

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
  wireStaticHandlers();

  const [metaRes, casesRes] = await Promise.allSettled([
    getJSON('/api/meta'),
    getJSON('/api/cases'),
  ]);

  if (metaRes.status !== 'fulfilled') {
    showError($('decide-error'), `Could not load engine metadata: ${metaRes.reason.message}`);
    $('decision').innerHTML = '<p class="hint on-dark">The engine is not answering.</p>';
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
    $('cases').innerHTML = '<p class="hint">Curated scenarios are unavailable; the controls below still work.</p>';
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
    const lift = num(bt.headline_lift_pp);
    if (lift !== null) {
      const sign = lift > 0 ? '+' : '';
      $('stat-lift').textContent = `${sign}${lift.toFixed(1)} pp approval`;
    } else {
      $('stat-lift').textContent = 'out-of-sample replay';
    }
  } catch {
    $('stat-lift').textContent = 'out-of-sample replay';
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
  else if (si.issuer_bucket === 'OTHER') opt.textContent = `${state.bin6} — issuer not seen in training`;
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
    showError($('decide-error'), 'Enter an amount greater than zero to re-decide.');
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
      'earlier in this chain: ' +
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
      opt.textContent = `${humanize(last.error_class)} (unrecognized)`;
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
      const t = c.txn || {};
      const down = c.down || c.psps_down || [];
      const bits = [
        money(num(t.amount)),
        t.funding,
        t.gateway,
        `attempt ${t.attempt_number || 1}`,
        `bias ${Number(c.cost_bias || 0).toFixed(2)}`,
      ];
      if (down.length) bits.push(`${down.join(', ')} down`);
      const title = CASE_TITLES[i] || String(c.why_interesting || '').split('. ')[0];
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
    renderDecision(d);
  } catch (err) {
    if (seq !== decideSeq) return;
    showError($('decide-error'), `Could not decide this transaction: ${err.message}`);
    panel.innerHTML = '<p class="hint on-dark">No decision — see the message below.</p>';
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
      <p class="kicker">Route to</p>
      <p class="route-psp"><span>${esc(d.route_psp ?? '—')}</span></p>
      ${segLine.length ? `<p class="seg-line">${esc(segLine.join(' · '))}</p>` : ''}
      ${d.static_default ? '<span class="flag">static default — insufficient data</span>' : ''}
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
          <div><dt>observed</dt><dd>${pct(num(v.p_hat))}</dd></div>
          <div><dt>fee</dt><dd>${fee(num(v.fee_pct))}</dd></div>
          <div><dt>level</dt><dd>${level(v.segment_used)}</dd></div>
          <div><dt>n</dt><dd>${count(num(v.n_support))}</dd></div>
          ${v.insufficient_data ? '<div><dd class="thin">thin data</dd></div>' : ''}
        </dl>
      </div>`;
    })
    .join('');

  const out = excluded
    .map(
      (p) => `<div class="psp out">
        <div class="psp-head"><span class="psp-id">${esc(p)}</span><span class="psp-net">excluded</span></div>
      </div>`
    )
    .join('');

  const r = d.retry_policy || {};
  let retryLine;
  if (r.stop_reason) {
    retryLine = `<span class="step">Stop.</span> ${esc(r.stop_reason)}`;
  } else if (r.should_retry_on_fail) {
    const cands = (r.next_psp_candidates || []).map((p) => esc(p)).join('<span class="arrow">·</span>');
    retryLine =
      `If it fails<span class="arrow">→</span><span class="step">retry</span>` +
      (cands ? `<span class="arrow">→</span>${cands}` : '') +
      (r.when ? `<span class="arrow">→</span>${esc(r.when)}` : '');
  } else {
    retryLine = `<span class="step">No retry.</span>${r.when ? ' ' + esc(r.when) : ''}`;
  }

  const retry = `<div class="retry">
      <p class="kicker">Retry plan</p>
      <p class="retry-line">${retryLine}</p>
      ${r.note ? `<p class="retry-note">${esc(r.note)}</p>` : ''}
    </div>`;

  const wide = window.matchMedia('(min-width: 1000px)').matches;
  const trail = `<details class="trail" ${wide ? 'open' : ''}>
      <summary>Reasoning trail</summary>
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
    renderSimulate(s);
  } catch (err) {
    if (seq !== simulateSeq) return;
    $('whatif').hidden = true;
    showError($('simulate-error'), `Could not run the what-if sweep: ${err.message}`);
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
        `bias ${Number(x.cost_bias).toFixed(2)}`,
        x.route_psp,
        money(num(x.expected_net)),
        x.route_psp !== baseRoute
      )
    )
    .join('');

  const downs = s.psps_down_scenarios || [];
  $('down-scenarios').innerHTML = downs
    .map((x) => {
      const label = (x.psps_down || []).length ? `without ${x.psps_down.join(', ')}` : 'all up';
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
    showError($('normalize-error'), `Could not load the curated declines: ${err.message}`);
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
        showError($('normalize-error'), 'Pick a curated decline, or type a code or message of your own.');
        return;
      }
      body = { psp: s.psp, raw_code: s.raw_code, raw_message: s.raw_message };
    }
    try {
      const res = await postJSON('/api/normalize', body);
      showError($('normalize-error'), null);
      renderNormalized(res, body);
    } catch (err) {
      showError($('normalize-error'), `Could not normalize that decline: ${err.message}`);
    }
  });
}

function renderNormalized(res, sent) {
  const source = String(res.source || 'fallback');
  const badgeClass =
    source === 'table' ? 'badge-table' : source === 'llm' ? 'badge-llm' : 'badge-fallback';
  const conf = num(res.confidence);
  const badge = `<span class="badge ${badgeClass}">${esc(source)}${
    source === 'llm' && res.provider ? ` · ${esc(res.provider)}` : ''
  }${conf !== null ? `<span class="badge-conf">${conf.toFixed(2)}</span>` : ''}</span>`;

  const raw = [sent.psp, sent.raw_code, sent.raw_message].filter(Boolean).join(' · ');
  const policy = RETRY_POLICY_COPY[res.error_class] || RETRY_POLICY_DEFAULT;
  const vocab = Array.isArray(res.error_class_options) ? res.error_class_options : [];

  $('norm-result').innerHTML = `
    <p class="kicker">Normalized to</p>
    <p class="norm-class">${esc(res.error_class ?? '—')}</p>
    <p>${badge}</p>
    <p class="norm-raw">from ${esc(raw)}</p>
    ${res.reasoning ? `<p class="norm-reason">${esc(res.reasoning)}</p>` : ''}
    ${
      vocab.length
        ? `<p class="norm-raw" style="margin-top:var(--s3)">closed vocabulary: ${esc(vocab.join(' · '))}</p>`
        : ''
    }
    <div class="norm-policy">
      <p class="kicker">What the retry machine does with it</p>
      <p class="norm-reason">${esc(policy)}</p>
    </div>`;
}
