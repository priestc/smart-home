// Runs on amazon.com pages:
//  - an order's details page: read it and send it to smart-home
//  - the "Order placed, thanks!" page after checkout: fetch each new order's
//    details page (with your login, like opening it) and send those
//  - the order history page opened by the popup's "Sync recent orders":
//    send every recent order smart-home doesn't have the items for yet
// Sending is done by background.js, which queues orders when smart-home
// can't be reached.

const SH_DETAILS_URL = id => `https://www.amazon.com/your-orders/order-details?orderID=${encodeURIComponent(id)}`;
const SH_SYNC_HASH = '#smart-home-sync';
const SH_SYNC_PAGES = 20;         // order history pages read by a sync at most (10 orders each)
const SH_NEW_ORDER_TRIES = 4;     // a just-placed order's page can take a moment to fill in
const SH_RETRY_MS = 20000;

// In Firefox a content script's own fetch() doesn't go out as the page (so
// without your Amazon login); content.fetch does
const shFetch = (typeof content !== 'undefined' && content.fetch) ? content.fetch.bind(content) : fetch;
const shSleep = ms => new Promise(r => setTimeout(r, ms));
const shSend = msg => chrome.runtime.sendMessage(msg);

function shPageHtml(doc) {
  const clone = doc.documentElement.cloneNode(true);
  clone.querySelectorAll('script, style, noscript, link, svg, iframe').forEach(e => e.remove());
  return clone.outerHTML;
}

// Read one order (from this page, or by fetching its details page) and send it.
// `tries` > 1 is for an order placed moments ago, whose page may not be ready.
// Returns 'sent', 'queued', 'already', 'skipped' or 'error'.
async function shProcessOrder(id, doc, url, tries = 1) {
  let order, lastError, lastDoc = doc;
  for (let n = 0; n < tries; n++) {
    if (!doc) {
      if (n) await shSleep(SH_RETRY_MS);
      try {
        const resp = await shFetch(SH_DETAILS_URL(id), {credentials: 'include'});
        if (!resp.ok) { lastError = new Error(`amazon.com returned HTTP ${resp.status} for the order page`); continue; }
        url = resp.url;
        lastDoc = new DOMParser().parseFromString(await resp.text(), 'text/html');
        // Orders Amazon won't show a details page for (some digital and
        // subscription orders): nothing to read
        if (/unable to load your order details/i.test(lastDoc.body ? lastDoc.body.textContent : '') && n === tries - 1) {
          await shSend({type: 'skipped', order_id: id, reason: 'Amazon has no details page for this order (a digital or subscription order?)'});
          return 'skipped';
        }
      } catch (e) {
        lastError = new Error(`Could not load the order page: ${e.message}`);
        continue;
      }
    }
    try {
      order = shParseOrderDetails(lastDoc, url);
      break;
    } catch (e) {
      lastError = e;
    }
  }
  if (!order) {
    await shSend({type: 'unreadable', order_id: id, url, error: lastError.message, html: lastDoc ? shPageHtml(lastDoc) : ''});
    return 'error';
  }
  const r = await shSend({type: 'order', order});
  if (r && r.rejected && lastDoc) {
    // smart-home didn't accept what was read: keep the page so the reader can be fixed
    await shSend({type: 'unreadable', order_id: id, url, error: r.error, html: shPageHtml(lastDoc), logged: true});
  }
  return r ? r.status : 'error';
}

async function shOrderIdsOnPage(doc, tries) {
  for (let n = 0; n < tries; n++) {
    const ids = shFindOrderIds(doc);
    if (ids.length) return ids;
    await shSleep(1500);
  }
  return [];
}

// The order history's "Next" page link, or null on the last page
function shNextPageUrl(doc) {
  const links = [...doc.querySelectorAll('.a-pagination a, a[href*="startIndex="]')];
  const next = links.find(a => /^\s*Next/i.test(a.textContent || '')) ;
  const href = next && next.getAttribute('href');
  return href ? new URL(href, 'https://www.amazon.com').href : null;
}

async function shSync() {
  history.replaceState(null, '', location.pathname + location.search);
  const progress = p => shSend({type: 'sync-progress', ...p});
  progress({state: 'running', message: 'Finding recent orders…'});
  try {
    const ids = new Set(await shOrderIdsOnPage(document, 5));
    // Follow the page's own "Next" link, as clicking it would
    let page = document;
    for (let n = 1; n < SH_SYNC_PAGES; n++) {
      const next = shNextPageUrl(page);
      if (!next) break;
      progress({state: 'running', message: `Finding orders: page ${n + 1} (${ids.size} so far)\u2026`});
      const resp = await shFetch(next, {credentials: 'include'});
      if (!resp.ok) throw new Error(`amazon.com returned HTTP ${resp.status} for order history page ${n + 1}`);
      page = new DOMParser().parseFromString(await resp.text(), 'text/html');
      const before = ids.size;
      shFindOrderIds(page).forEach(i => ids.add(i));
      if (ids.size === before) break;
      await shSleep(1000);
    }
    if (!ids.size) throw new Error('No orders found on the order history page (are you signed in?)');
    const states = await shSend({type: 'states', ids: [...ids]});
    if (!states || states.error) throw new Error(states ? states.error : 'No answer from the extension');
    const skipped = new Set((await shSend({type: 'skipped-list'})) || []);
    // 'needs_images': has its items, but its product pictures weren't fetched yet
    const todo = [...ids].filter(i => states.states[i] !== 'detailed' && !skipped.has(i));
    const counts = {sent: 0, queued: 0, already: 0, skipped: 0, error: 0};
    for (let i = 0; i < todo.length; i++) {
      progress({state: 'running', message: `Reading order ${i + 1} of ${todo.length} (${todo[i]}; ${ids.size} orders found, the rest already have their items)…`});
      const r = await shProcessOrder(todo[i]);
      counts[r] = (counts[r] || 0) + 1;
      await shSleep(1500);  // don't hammer amazon.com
    }
    progress({state: 'done', message: `Done. ${ids.size} orders found: ${ids.size - todo.length} already had their items, `
      + `${counts.sent} sent, ${counts.skipped} have no details page`
      + `${counts.queued ? `, ${counts.queued} queued until smart-home can be reached` : ''}, ${counts.error} couldn’t be read.`,
      error: counts.error > 0});
  } catch (e) {
    progress({state: 'done', message: `Sync failed: ${e.message}`, error: true});
  }
}

(async () => {
  const path = location.pathname;
  if (/order-details/i.test(path) || /order-details/i.test(location.search)) {
    const ids = shFindOrderIds(document);
    const id = (location.search.match(/order[Ii][Dd]=(\d{3}-\d{7}-\d{7})/) || [])[1] || ids[0];
    if (!id) return;
    if ((await shSend({type: 'is-done', id})) === true) return;  // already sent
    // Parts of the page can fill in after it loads
    for (let n = 0; n < 10 && !/Grand Total/i.test(document.body.innerText); n++) await shSleep(1000);
    await shProcessOrder(id, document, location.href);
  } else if (/thank-?you/i.test(path) || /Order placed, thanks/i.test(document.body.innerText.slice(0, 3000))) {
    const ids = await shOrderIdsOnPage(document, 10);
    if (!ids.length) {
      await shSend({type: 'unreadable', order_id: '', url: location.href,
        error: 'An order confirmation page with no order number found', html: shPageHtml(document)});
      return;
    }
    await shSleep(5000);  // let Amazon finish setting up the order
    for (const id of ids) {
      if ((await shSend({type: 'is-done', id})) === true) continue;
      await shProcessOrder(id, null, null, SH_NEW_ORDER_TRIES);
    }
  } else if (/your-orders\/orders|order-history/i.test(path) && location.hash === SH_SYNC_HASH) {
    await shSync();
  }
})();
