# Smart Home — Project Notes

## Deployment

This project runs on the **tank2** server. Access it via:

```
ssh tank2
```

The `smart-home monitor` and `smart-home serve` commands run there. When debugging issues that require checking live data (e.g. the SQLite DB, logs, or running processes), use `ssh tank2` to connect.

Also, the project is re-deployed after almost every commit using the commands: `pipx install git+https://github.com/priestc/smart-home.git@master --force; sudo -n systemctl restart smart-home-api.service; sudo -n systemctl restart smart-home.service`
