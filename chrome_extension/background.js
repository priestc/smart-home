// Sends what amazon.js reads to smart-home. When smart-home can't be reached
// (away from home), orders wait in a queue that's retried every few minutes
// and whenever another order is sent. The toolbar badge shows the number
// waiting, or "!" after an error; the popup lists what happened.

const DEFAULT_SERVER = 'http://smart-home.lan';
const LOG_SIZE = 40;
const RETRY_MINUTES = 5;

async function server() {
  const {server} = await chrome.storage.sync.get('server');
  return (server || DEFAULT_SERVER).replace(/\/+$/, '');
}

async function getLocal(key, fallback) {
  const v = (await chrome.storage.local.get(key))[key];
  return v === undefined ? fallback : v;
}

async function addLog(entry) {
  const log = await getLocal('log', []);
  log.unshift({ts: new Date().toISOString(), ...entry});
  await chrome.storage.local.set({log: log.slice(0, LOG_SIZE), lastError: entry.outcome === 'error'});
  await updateBadge();
}

async function updateBadge() {
  const queue = await getLocal('queue', []);
  const lastError = await getLocal('lastError', false);
  const text = queue.length ? String(queue.length) : lastError ? '!' : '';
  await chrome.action.setBadgeText({text});
  await chrome.action.setBadgeBackgroundColor({color: lastError ? '#c62828' : '#e07820'});
}

class Unreachable extends Error {}

// POST to smart-home. Throws Unreachable when it can't be reached or
// answers 5xx (worth retrying), Error for any other refusal.
async function post(path, body) {
  let resp;
  try {
    resp = await fetch(`${await server()}${path}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
  } catch (e) {
    const hint = /NetworkError/.test(e.message) ? ' (In Firefox: Settings \u2192 Privacy & Security \u2192 HTTPS-Only Mode must be off, or have an exception for this address; and about:addons \u2192 this extension \u2192 Permissions must allow it.)' : '';
    throw new Unreachable(`Can't reach ${await server()}: ${e.message}${hint}`);
  }
  let data = {};
  try { data = await resp.json(); } catch (_) {}
  if (resp.status >= 500) throw new Unreachable(data.error || `smart-home answered HTTP ${resp.status}`);
  if (!resp.ok) throw new Error(data.error || `smart-home answered HTTP ${resp.status}`);
  return data;
}

async function markDone(id) {
  const done = await getLocal('done', []);
  if (!done.includes(id)) await chrome.storage.local.set({done: [id, ...done].slice(0, 500)});
}

// Returns {status: 'sent' | 'already' | 'queued' | 'error', rejected?, error?}
async function sendOrder(order, fromQueue = false) {
  try {
    const r = await post('/api/receipts/amazon/order', order);
    await markDone(order.order_id);
    await addLog({order_id: order.order_id, outcome: r.outcome, receipt_id: r.receipt_id});
    return {status: r.outcome === 'already' ? 'already' : 'sent'};
  } catch (e) {
    if (e instanceof Unreachable) {
      if (!fromQueue) await enqueue({kind: 'order', body: order});
      return {status: 'queued', error: e.message};
    }
    await addLog({order_id: order.order_id, outcome: 'error', error: e.message});
    return {status: 'error', rejected: true, error: e.message};
  }
}

async function sendUnreadable(msg, fromQueue = false) {
  const body = {order_id: msg.order_id, url: msg.url, error: msg.error, html: msg.html};
  if (!fromQueue && !msg.logged) await addLog({order_id: msg.order_id, outcome: 'error', error: msg.error});
  try {
    await post('/api/receipts/amazon/order-error', body);
  } catch (e) {
    if (e instanceof Unreachable && !fromQueue) {
      await enqueue({kind: 'unreadable', body});
      return;
    }
    throw e;
  }
}

async function enqueue(item) {
  const queue = await getLocal('queue', []);
  // One entry per order
  const key = item.body.order_id;
  const rest = queue.filter(q => !(q.kind === item.kind && key && q.body.order_id === key));
  await chrome.storage.local.set({queue: [...rest, {...item, queuedAt: new Date().toISOString()}]});
  await updateBadge();
}

let flushing = false;
async function flushQueue() {
  if (flushing) return;
  flushing = true;
  try {
    const queue = await getLocal('queue', []);
    const left = [];
    for (let i = 0; i < queue.length; i++) {
      const q = queue[i];
      try {
        if (q.kind === 'order') {
          const r = await sendOrder(q.body, true);
          if (r.status === 'queued') { left.push(...queue.slice(i)); break; }
        } else {
          await sendUnreadable(q.body, true);
        }
      } catch (e) {
        if (e instanceof Unreachable) { left.push(...queue.slice(i)); break; }
        // refused for good: logged by sendOrder; drop it
      }
    }
    await chrome.storage.local.set({queue: left});
    await updateBadge();
  } finally {
    flushing = false;
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    switch (msg.type) {
      case 'order': {
        await flushQueue();
        return sendOrder(msg.order);
      }
      case 'unreadable':
        try { await sendUnreadable(msg); } catch (_) {}
        return {ok: true};
      case 'skipped': {
        const skipped = await getLocal('skipped', []);
        if (!skipped.includes(msg.order_id)) await chrome.storage.local.set({skipped: [msg.order_id, ...skipped].slice(0, 500)});
        await addLog({order_id: msg.order_id, outcome: 'skipped', error: msg.reason});
        return {ok: true};
      }
      case 'skipped-list':
        return getLocal('skipped', []);
      case 'is-done':
        return (await getLocal('done', [])).includes(msg.id);
      case 'states':
        try {
          return await post('/api/receipts/amazon/order-states', {order_ids: msg.ids});
        } catch (e) {
          return {error: e.message};
        }
      case 'sync-progress':
        await chrome.storage.local.set({sync: {...msg, ts: new Date().toISOString()}});
        if (msg.state === 'done' && msg.error) await chrome.storage.local.set({lastError: true});
        await updateBadge();
        return {ok: true};
      case 'categories-needed':
        try {
          const r = await fetch(`${await server()}/api/receipts/amazon/categories-needed`);
          if (!r.ok) throw new Error(`smart-home answered HTTP ${r.status}`);
          return await r.json();
        } catch (e) {
          return {error: e.message};
        }
      case 'category':
        try {
          await post('/api/receipts/amazon/category', {asin: msg.asin, path: msg.path, error: msg.error});
        } catch (e) {
          // A product page you opened that smart-home has no product for: nothing to do
          if (!msg.passive) await addLog({outcome: 'error', error: `Saving the category of ${msg.asin}: ${e.message}`});
        }
        return {ok: true};
      case 'flush':
        await flushQueue();
        return {ok: true};
      default:
        return {error: `Unknown message ${msg.type}`};
    }
  })().then(reply, e => reply({status: 'error', error: e.message}));
  return true;  // reply comes later
});

chrome.alarms.create('flush', {periodInMinutes: RETRY_MINUTES});
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'flush') flushQueue(); });
chrome.runtime.onStartup.addListener(() => { updateBadge(); flushQueue(); });
chrome.runtime.onInstalled.addListener(() => updateBadge());
