# Smart Home — Project Notes

## Deployment

This project runs on the **tank2** server. Access it via:

```
ssh tank2
```

The `smart-home monitor` and `smart-home serve` commands run there. When debugging issues that require checking live data (e.g. the SQLite DB, logs, or running processes), use `ssh tank2` to connect.

Also, the project is re-deployed after almost every commit. The standard deploy command is:

```
pipx install git+https://github.com/priestc/smart-home.git@master --force; sudo -n systemctl restart smart-home-api.service
```

**Do NOT restart `smart-home.service` (the monitor) on every deploy.** Restarting it drops the BLE connection to the pool monitor (YC01), which causes the device to power off and requires a manual button press to bring it back online. Only restart `smart-home.service` when changes to the BLE monitor code (`__main__.py`, `pool.py`, etc.) actually require it:

```
ssh tank2 'sudo -n systemctl restart smart-home.service'
```

## Testing workflow

When the user mentions doing a test or says they want to test something, automatically commit all pending changes, push to GitHub, and deploy to tank2 (using the standard deploy command above) before they begin the test. Do not wait to be asked separately to commit/deploy.

## Git hooks

A pre-commit hook in `hooks/pre-commit` automatically rebuilds the ESP32 firmware binaries whenever `esp32_relay.ino` is staged. After a fresh clone, install it with:

```
ln -sf ../../hooks/pre-commit .git/hooks/pre-commit
```

This requires `arduino-cli` to be on `$PATH`. On machines without it the hook warns and skips; on tank2 it always runs.

To install `arduino-cli` and all ESP32 build dependencies:

```
bash "$(smart-home firmware-dir)/setup.sh"
```

## Error handling principle

Never silently swallow errors. Whenever something goes wrong — a failed network request, an unexpected API response, a caught exception — always surface it visibly in the UI so the user knows what's happening. This applies to:

- **AJAX / fetch calls**: show an error banner or message on the page if the request fails or returns a non-2xx status. Do not let `.catch()` or `try/catch` blocks silently do nothing.
- **Backend errors**: return meaningful error responses; don't swallow exceptions and return empty or stale data.
- **UI state**: if data can't be loaded, show an error state rather than leaving the UI blank or in a loading spinner forever.
