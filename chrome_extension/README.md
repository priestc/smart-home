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
- for every order that doesn't have its items yet, when you click **Sync
  orders** in its popup (the last 3 months, this year or last year; catches
  orders placed in the phone app). Orders Amazon has no details page for
  (some digital and subscription orders) are skipped and remembered.

An order is only sent when its items add up exactly to the page's
"Item(s) Subtotal". Otherwise it's reported as an error, and the page is
saved on tank2 under `~/.local/share/smart-home/amazon-extension/` so the
reader (`parse.js`) can be fixed. Errors show in the popup (red "!" badge)
and on /receipts.

When smart-home can't be reached (away from home), orders wait in a queue
(the badge shows how many) and are sent when it's reachable again.

## Install in Chrome

1. Chrome → `chrome://extensions` → turn on **Developer mode** (top right).
2. **Load unpacked** → pick this `chrome_extension` folder.
3. Pin it from the puzzle-piece menu. Its popup has the smart-home address
   (default `http://smart-home.lan`) and **Test connection**.

After pulling changes, click the reload arrow on its card in
`chrome://extensions`.

## Install in Firefox (121 or newer)

1. Go to `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on…**
   → pick the **`manifest.json` file** inside this folder (not the folder).
2. Click the extension's toolbar button. If it says it isn't allowed to use
   amazon.com yet, click **Allow access** (Firefox starts extensions with
   site access off; it can also be turned on in `about:addons` → the
   extension → Permissions).

A temporary add-on is removed when Firefox quits. To keep it installed, have
Mozilla sign it as an unlisted (private, not on the add-on store) add-on:

1. Sign in at https://addons.mozilla.org, then generate API credentials at
   https://addons.mozilla.org/developers/addon/api/key/.
2. Put them in `~/.config/web-ext/amo.env` (not in the repo):
   ```
   export WEB_EXT_API_KEY='user:...'
   export WEB_EXT_API_SECRET='...'
   ```
3. Run `tools/sign-firefox-extension.sh`. It prints the signed `.xpi`.
4. In Firefox, remove the temporary add-on in `about:debugging`, then open
   the `.xpi` (drag it onto a Firefox window, or `about:addons` → gear →
   **Install Add-on From File**). It stays installed, and the popup's **Allow
   access** button may be needed once more.

Mozilla accepts each version only once, so bump `version` in `manifest.json`
before signing a changed extension; install the new `.xpi` over the old one.

The manifest sets its own `content_security_policy`: Firefox's default for
MV3 extensions includes `upgrade-insecure-requests`, which would turn every
request to `http://smart-home.lan` into `https://` and fail.
