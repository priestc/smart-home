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

A temporary add-on is removed when Firefox quits. To keep it installed,
either use Firefox Developer Edition or Nightly with
`xpinstall.signatures.required` set to false in `about:config` and install
a zip of this folder from `about:addons` → gear → **Install Add-on From
File**, or have Mozilla sign it for free as an unlisted add-on with
`npx web-ext sign --channel=unlisted` (needs AMO API keys) and install the
.xpi it gives you.

The manifest sets its own `content_security_policy`: Firefox's default for
MV3 extensions includes `upgrade-insecure-requests`, which would turn every
request to `http://smart-home.lan` into `https://` and fail.
