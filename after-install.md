# kanban-warden installed

Enable and restart the same Hermes profile that will run the gateway:

```bash
hermes --profile <profile> plugins enable kanban-warden
hermes --profile <profile> gateway restart
```

For the Hairou Feishu gateway profile, use:

```bash
hermes --profile hairou-feishu plugins enable kanban-warden
hermes --profile hairou-feishu gateway restart
```

If this plugin was installed without `--profile`, reinstall it in the target
profile home so the profile-scoped gateway can discover it:

```bash
hermes --profile <profile> plugins install coderlaoma/hermes-kanban-warden --force --enable
```
