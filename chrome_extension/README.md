# Smart Home: Amazon orders (Chrome extension)

Amazon's order emails no longer list what was bought. This extension reads
the order's page on amazon.com, in the browser you're already signed in to,
and sends every item (full name, ASIN, quantity, price) plus the tax and
total to smart-home, where it fills in that order's receipt
(`smart_home/amazon.py`, `apply_page_order`).

It sends an order:
- right after you check out (from the "Order placed, thanks!" page it opens
  each new order's details page in the background),
- whenever you open an order's details page,
- for every order of the last 3 months that doesn't have its items yet, when
  you click **Sync recent orders** in its popup (catches orders placed in the
  phone app).

An order is only sent when its items add up exactly to the page's
"Item(s) Subtotal". Otherwise it's reported as an error, and the page is
saved on tank2 under `~/.local/share/smart-home/amazon-extension/` so the
reader (`parse.js`) can be fixed. Errors show in the popup (red "!" badge)
and on /receipts.

When smart-home can't be reached (away from home), orders wait in a queue
(the badge shows how many) and are sent when it's reachable again.

## Install

1. Chrome → `chrome://extensions` → turn on **Developer mode** (top right).
2. **Load unpacked** → pick this `chrome_extension` folder.
3. Pin it from the puzzle-piece menu. Its popup has the smart-home address
   (default `http://smart-home.lan`) and **Test connection**.

After pulling changes, click the reload arrow on its card in
`chrome://extensions`.
