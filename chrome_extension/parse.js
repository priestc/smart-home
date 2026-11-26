// Reads an Amazon order's details page (live, or fetched and parsed with
// DOMParser) into {order_id, order_date, items, subtotal, total_before_tax,
// tax, total}. Works from the visible text and product links rather than
// Amazon's class names, which change often. Throws an Error when the result
// doesn't check out (the items must add up to the page's Item(s) Subtotal),
// so a misread order is never sent.

const SH_ORDER_RE = /\b(\d{3}-\d{7}-\d{7})\b/;
const SH_MONEY_RE = /(-?)\s*\$\s?([\d,]+\.\d{2})(?!\d)/;
const SH_ASIN_RE = /\/(?:dp|gp\/product|gp\/aw\/d)\/([A-Z0-9]{10})(?:[/?#]|$)/;

function shText(s) {
  return (s || '').replace(/[‎‏‪-‮]/g, '').replace(/\s+/g, ' ').trim();
}

// The page without scripts and styles (their text would get in the way)
function shCleanBody(doc) {
  const body = doc.body.cloneNode(true);
  body.querySelectorAll('script, style, noscript, template').forEach(e => e.remove());
  return body;
}

function shMoney(s) {
  const m = (s || '').match(SH_MONEY_RE);
  return m ? (m[1] ? -1 : 1) * parseFloat(m[2].replace(/,/g, '')) : null;
}

// The amount printed after a label, e.g. "Grand Total: $69.18"
function shAmountAfter(text, label) {
  const m = text.match(new RegExp(label + '\\s*:?\\s*(-?)\\s*\\$\\s?([\\d,]+\\.\\d{2})(?!\\d)', 'i'));
  return m ? (m[1] ? -1 : 1) * parseFloat(m[2].replace(/,/g, '')) : null;
}

function shIsoDate(s) {
  const d = new Date(s);
  if (isNaN(d)) return null;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

// Order numbers on a page (thank-you page, order history), from its text and links
function shFindOrderIds(doc) {
  const body = shCleanBody(doc);
  const ids = new Set();
  for (const m of shText(body.textContent).matchAll(/\b(\d{3}-\d{7}-\d{7})\b/g)) ids.add(m[1]);
  body.querySelectorAll('a[href*="rderI"]').forEach(a => {
    const m = (a.getAttribute('href') || '').match(/order[Ii][Dd]=(\d{3}-\d{7}-\d{7})/);
    if (m) ids.add(m[1]);
  });
  return [...ids];
}

function shParseOrderDetails(doc, url) {
  const body = shCleanBody(doc);
  const text = shText(body.textContent);

  const idMatch = (url || '').match(/order[Ii][Dd]=(\d{3}-\d{7}-\d{7})/) || text.match(/Order\s*#\s*(\d{3}-\d{7}-\d{7})/);
  if (!idMatch) throw new Error('No order number on the page');
  const orderId = idMatch[1];

  const dateMatch = text.match(/Order placed\s*:?\s*([A-Z][a-z]+\.? \d{1,2},? \d{4})/);
  const subtotal = shAmountAfter(text, 'Item\\(s\\) Subtotal');
  const total = shAmountAfter(text, 'Grand Total');
  let tax = shAmountAfter(text, '(?:Estimated tax to be collected|Tax collected|Estimated tax|Sales tax)');
  let beforeTax = shAmountAfter(text, 'Total before tax');
  if (subtotal == null) throw new Error('No "Item(s) Subtotal" on the page');
  if (total == null) throw new Error('No "Grand Total" on the page');
  if (tax == null && beforeTax != null) tax = Math.round((total - beforeTax) * 100) / 100;
  if (tax == null) throw new Error('No tax on the page');
  if (beforeTax == null) beforeTax = Math.round((total - tax) * 100) / 100;

  // Product links, the longest-text one per ASIN being its title
  const titles = new Map();
  body.querySelectorAll('a[href]').forEach(a => {
    const m = (a.getAttribute('href') || '').match(SH_ASIN_RE);
    const t = shText(a.textContent);
    if (!m || t.length < 3) return;
    if (!titles.has(m[1]) || t.length > titles.get(m[1]).title.length) titles.set(m[1], {a, title: t});
  });
  const containsOtherTitle = (el, asin) => [...el.querySelectorAll('a[href]')].some(a => {
    const m = (a.getAttribute('href') || '').match(SH_ASIN_RE);
    return m && m[1] !== asin && titles.has(m[1]) && shText(a.textContent).length >= 3;
  });

  const items = [];
  for (const [asin, {a, title}] of titles) {
    // The smallest block holding the title and a price after it
    let el = a.parentElement, price = null;
    while (el && el !== body) {
      const t = shText(el.textContent);
      const i = t.indexOf(title);
      const rest = i >= 0 ? t.slice(i + title.length) : '';
      if (SH_MONEY_RE.test(rest)) {
        // Another product's title before the price: it isn't this item's
        const cut = rest.slice(0, rest.search(SH_MONEY_RE));
        const other = [...titles.values()].some(o => o.title !== title && cut.includes(o.title));
        if (!other) price = shMoney(rest);
        break;
      }
      el = el.parentElement;
    }
    if (price == null || !el) continue;
    // A few levels up (the image, with its quantity badge), staying within this item
    let wide = el;
    for (let k = 0; k < 4 && wide.parentElement && wide.parentElement !== body && !containsOtherTitle(wide.parentElement, asin); k++) {
      wide = wide.parentElement;
    }
    const wideText = shText(wide.textContent);
    let qty = 1;
    const qm = wideText.match(/\b(?:Qty|Quantity)\s*:?\s*(\d{1,3})\b/i);
    if (qm) qty = parseInt(qm[1], 10);
    else {
      const badge = [...wide.querySelectorAll('[class*="qty" i], [class*="quantity" i]')]
        .map(e => shText(e.textContent)).find(t => /^\d{1,3}$/.test(t));
      if (badge) qty = parseInt(badge, 10);
    }
    items.push({name: title, asin, quantity: Math.max(qty, 1), unit_price: price, soldBy: /Sold by/i.test(wideText)});
  }
  if (!items.length) throw new Error('No products found on the page');

  // Ordered items say who sold them; suggested products usually don't
  const sum = list => Math.round(list.reduce((a, it) => a + it.unit_price * it.quantity, 0) * 100) / 100;
  for (const list of [items.filter(it => it.soldBy), items]) {
    if (list.length && Math.abs(sum(list) - subtotal) < 0.005) {
      return {
        order_id: orderId,
        order_date: dateMatch ? shIsoDate(dateMatch[1]) : null,
        items: list.map(({name, asin, quantity, unit_price}) => ({name, asin, quantity, unit_price})),
        subtotal, total_before_tax: beforeTax, tax, total,
        url: url || null,
      };
    }
  }
  throw new Error(`The products found add up to $${sum(items).toFixed(2)}, not the page's Item(s) Subtotal ` +
    `$${subtotal.toFixed(2)} (found: ${items.map(it => `${it.quantity} x ${it.name.slice(0, 40)} $${it.unit_price}`).join('; ')})`);
}
