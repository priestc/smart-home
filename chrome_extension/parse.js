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

// A product picture's full-size address: Amazon serves thumbnails such as
// https://m.media-amazon.com/images/W/<test>/images/I/619zN9UfOXL._SS284_.jpg
// of https://m.media-amazon.com/images/I/619zN9UfOXL.jpg
function shImageUrl(src) {
  if (!/^https:\/\/[^/]*(media-amazon|images-amazon)\.com\//.test(src || '')) return null;
  const m = src.match(/\/images\/I\/([^/.]+)[^/]*?\.(jpg|jpeg|png|webp)(?:[?#]|$)/i);
  return m ? `https://m.media-amazon.com/images/I/${m[1]}.${m[2].toLowerCase()}` : null;
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
  // On an order history page fetched in the background each order card's
  // contents are encrypted (Amazon's scripts decode them in a real tab), but
  // the card still names its order: data-csa-c-slot-id="...order-card.112-..."
  body.querySelectorAll('[data-csa-c-slot-id*="order-card"]').forEach(el => {
    const m = (el.getAttribute('data-csa-c-slot-id') || '').match(/(\d{3}-\d{7}-\d{7})/);
    if (m) ids.add(m[1]);
  });
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
    // Wording only an ordered item has next to it
    const ordered = (/Sold by/i.test(wideText) ? 1 : 0)
      + (/Return or replace|Return items|Eligible through|Return window|Get product support|Track package/i.test(wideText) ? 1 : 0);
    // The product's picture: an <img> whose alt text is the product's name,
    // else the first product picture near it
    const pics = [...body.querySelectorAll('img')].map(i => ({alt: shText(i.getAttribute('alt')),
      src: i.getAttribute('data-a-hires') || i.getAttribute('src') || ''})).filter(i => shImageUrl(i.src));
    const t = title.replace(/(\.\.\.|\u2026)$/, '').toLowerCase();
    let pic = pics.find(i => i.alt && (i.alt.toLowerCase().startsWith(t) || t.startsWith(i.alt.toLowerCase())));
    if (!pic) {
      const near = [...wide.querySelectorAll('img')].map(i => i.getAttribute('data-a-hires') || i.getAttribute('src') || '');
      const src = near.find(shImageUrl);
      pic = src ? {src} : null;
    }
    const image_url = pic ? shImageUrl(pic.src) : null;
    items.push({name: title, asin, quantity: Math.max(qty, 1), unit_price: price, ordered, image_url});
  }
  if (!items.length) throw new Error('No products found on the page');

  // The page also shows suggested products ("Buy it again", "Related to
  // items you've viewed"). The order's items are the ones that add up exactly
  // to the Item(s) Subtotal; quantity badges can be misread, so each item may
  // also count as 1. Of several combinations that fit, prefer the one using
  // the quantities as read, then the one whose items have the most ordered-
  // item wording ("Sold by", "Return or replace items"), then items nearer
  // the top of the page.
  const cands = items.slice(0, 30);
  const target = Math.round(subtotal * 100);
  const solutions = [];
  const pick = [];
  (function search(i, sum) {
    if (solutions.length >= 200) return;
    if (sum === target && pick.length) { solutions.push(pick.slice()); return; }
    if (i === cands.length || sum > target) return;
    const it = cands[i], each = Math.round(it.unit_price * 100);
    for (const q of it.quantity > 1 ? [it.quantity, 1] : [1]) {
      pick.push({i, quantity: q});
      search(i + 1, sum + each * q);
      pick.pop();
    }
    search(i + 1, sum);
  })(0, 0);
  if (solutions.length) {
    const score = sol => [sol.filter(p => p.quantity !== cands[p.i].quantity).length,
                          -sol.reduce((a, p) => a + cands[p.i].ordered, 0),
                          Math.max(...sol.map(p => p.i))];
    const better = (a, b) => { for (let k = 0; k < a.length; k++) if (a[k] !== b[k]) return a[k] < b[k]; return false; };
    let best = solutions[0];
    for (const sol of solutions) if (better(score(sol), score(best))) best = sol;
    return {
      order_id: orderId,
      order_date: dateMatch ? shIsoDate(dateMatch[1]) : null,
      items: best.map(p => ({name: cands[p.i].name, asin: cands[p.i].asin, quantity: p.quantity,
                             unit_price: cands[p.i].unit_price, image_url: cands[p.i].image_url})),
      subtotal, total_before_tax: beforeTax, tax, total,
      url: url || null,
    };
  }
  const sum = list => Math.round(list.reduce((a, it) => a + it.unit_price * it.quantity, 0) * 100) / 100;
  throw new Error(`No combination of the products found adds up to the page's Item(s) Subtotal (all of them: $${sum(items).toFixed(2)}, subtotal ` +
    `$${subtotal.toFixed(2)}; found: ${items.map(it => `${it.quantity} x ${it.name.slice(0, 40)} $${it.unit_price}`).join('; ')})`);
}
