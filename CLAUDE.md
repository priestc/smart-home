# Smart Home — Project Notes

## Deployment

This project runs on the **tank2** server. Access it via:

```
ssh tank2
```

The `smart-home monitor` and `smart-home serve` commands run there. When debugging issues that require checking live data (e.g. the SQLite DB, logs, or running processes), use `ssh tank2` to connect.

Also, the project is re-deployed after almost every commit using the commands: `pipx install git+https://github.com/priestc/smart-home.git@master --force; sudo -n systemctl restart smart-home-api.service; sudo -n systemctl restart smart-home.service`

## Error handling principle

Never silently swallow errors. Whenever something goes wrong — a failed network request, an unexpected API response, a caught exception — always surface it visibly in the UI so the user knows what's happening. This applies to:

- **AJAX / fetch calls**: show an error banner or message on the page if the request fails or returns a non-2xx status. Do not let `.catch()` or `try/catch` blocks silently do nothing.
- **Backend errors**: return meaningful error responses; don't swallow exceptions and return empty or stale data.
- **UI state**: if data can't be loaded, show an error state rather than leaving the UI blank or in a loading spinner forever.
