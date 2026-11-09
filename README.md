# K3s Cluster Telegram Admin Bot

Bot Telegram leggero e potente per gestire e monitorare il cluster Kubernetes (K3s) dell'HomeLab da remoto.

## Caratteristiche Principali

- 📊 **Stato Pod in tempo reale**: Visualizzazione con icone intuitive (`🟢 Running`, `🟡 Pending`, `🔴 CrashLoopBackOff`).
- ⚡ **Monitoraggio Risorse**: CPU e RAM consumate dal nodo e dai singoli pod tramite Kubernetes Metrics Server.
- 🌐 **Web App & Health Check**: Controllo rapido della raggiungibilità HTTP e link diretti sia su rete LAN che Tailscale.
- 🔄 **Rollout Restart con 1 tap**: Menu con pulsanti interattivi per riavviare qualsiasi applicazione (`Nextcloud`, `Paperless`, `Firefly`, `Grafana`, ecc.).
- 📜 **Lettura Log**: Visualizzazione immediata delle ultime righe di log di qualsiasi deployment.
- ⚖️ **Scaling Repliche**: Accensione e spegnimento rapido (es. `garmin-fetch-data`).
- ⚠️ **Eventi Cluster**: Diagnosi immediata di errori o avvisi nel cluster.
- 🔒 **Sicurezza totale**: Accessibile esclusivamente dai Telegram User ID autorizzati.

## Configurazione

Copia `config.example.json` in `config.json` e inserisci il token del bot ottenuto da `@BotFather`:
```bash
cp config.example.json config.json
```

Contenuto di `config.json`:
```json
{
  "bot_token": "YOUR_TELEGRAM_BOT_TOKEN_HERE",
  "allowed_user_ids": [
    123456789
  ],
  "server_lan_ip": "192.168.1.100",
  "server_tailscale_ip": "100.x.y.z",
  "default_namespace": "homelab"
}
```

## Avvio come Servizio Systemd (all'avvio del PC)

Il bot è configurato come servizio utente `cluster-bot.service`:
```bash
# Avvio manuale per test
python3 /home/francesco/Progetti/cluster-bot/bot.py

# Avvio e abilitazione con systemd
systemctl --user daemon-reload
systemctl --user enable --now cluster-bot.service

# Verifica stato
systemctl --user status cluster-bot.service
```
