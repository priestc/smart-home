const DEFAULT_SERVER = 'http://smart-home.lan';
const SYNC_URL = 'https://www.amazon.com/your-orders/orders?timeFilter=months-3#smart-home-sync';
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

async function serverUrl() {
  const {server} = await chrome.storage.sync.get('server');
  return (server || DEFAULT_SERVER).replace(/\/+$/, '');
}

const WORDS = {filled: 'items filled in', created: 'receipt added', already: 'already had its items', error: 'error'};

async function render() {
  const base = await serverUrl();
  const {log = [], queue = [], sync} = await chrome.storage.local.get(['log', 'queue', 'sync']);
  $('queue').textContent = queue.length ? `${queue.length} waiting to send (smart-home unreachable)` : '';
  $('queue').className = queue.length ? 'err' : 'muted';
  if (sync) {
    $('sync-status').className = sync.error ? 'err' : 'muted';
    $('sync-status').textContent = `${new Date(sync.ts).toLocaleString()}: ${sync.message}`;
    $('sync').disabled = sync.state === 'running' && Date.now() - new Date(sync.ts) < 120000;
  }
  $('log').innerHTML = log.length ? log.slice(0, 12).map(e => {
    const what = e.receipt_id
      ? `<a href="${esc(base)}/receipts/${e.receipt_id}" target="_blank">${esc(e.order_id || 'receipt')}</a>`
      : esc(e.order_id || 'order');
    return `<li class="${e.outcome === 'error' ? 'err' : ''}"><span class="muted">${esc(new Date(e.ts).toLocaleString())}</span> `
      + `${what}: ${esc(WORDS[e.outcome] || e.outcome)}${e.error ? ` — ${esc(e.error)}` : ''}</li>`;
  }).join('') : '<li class="muted">Nothing sent yet.</li>';
}

$('sync').addEventListener('click', async () => {
  await chrome.storage.local.set({sync: {state: 'running', message: 'Opening your Amazon order history…', ts: new Date().toISOString()}});
  await chrome.tabs.create({url: SYNC_URL});
  render();
});

$('save').addEventListener('click', async () => {
  const status = $('server-status');
  let url;
  try {
    url = new URL($('server').value.trim() || DEFAULT_SERVER);
  } catch (_) {
    status.className = 'err'; status.textContent = 'Not a valid address';
    return;
  }
  const origin = url.origin;
  // smart-home.lan is allowed already; any other address needs permission
  if (origin !== DEFAULT_SERVER) {
    const granted = await chrome.permissions.request({origins: [`${origin}/*`]});
    if (!granted) { status.className = 'err'; status.textContent = 'Permission to reach that address was refused'; return; }
  }
  await chrome.storage.sync.set({server: origin});
  status.className = 'ok'; status.textContent = 'Saved';
  chrome.runtime.sendMessage({type: 'flush'});
});

$('test').addEventListener('click', async () => {
  const status = $('server-status');
  status.className = 'muted'; status.textContent = 'Testing…';
  try {
    const resp = await fetch(`${await serverUrl()}/api/receipts/amazon`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const d = await resp.json();
    status.className = 'ok'; status.textContent = `Connected (${d.n_imported} order emails imported)`;
    chrome.runtime.sendMessage({type: 'flush'});
  } catch (e) {
    status.className = 'err'; status.textContent = `Can't connect: ${e.message}`;
  }
});

// Firefox grants host permissions only when asked; without them nothing runs
async function neededOrigins() {
  return ['https://www.amazon.com/*', `${await serverUrl()}/*`];
}
async function checkAccess() {
  const ok = await chrome.permissions.contains({origins: await neededOrigins()});
  $('access').style.display = ok ? 'none' : '';
  return ok;
}
$('grant').addEventListener('click', async () => {
  // Must be called straight from the click, before anything else is awaited
  const granted = await chrome.permissions.request({origins: ['https://www.amazon.com/*', `${$('server').value.trim().replace(/\/+$/, '') || DEFAULT_SERVER}/*`]});
  if (granted) chrome.runtime.sendMessage({type: 'flush'});
  checkAccess();
});

chrome.storage.onChanged.addListener(render);
(async () => {
  $('server').value = await serverUrl();
  // Opening the popup clears the error badge (the errors stay listed)
  await chrome.storage.local.set({lastError: false});
  chrome.runtime.sendMessage({type: 'flush'});
  checkAccess();
  render();
})();
