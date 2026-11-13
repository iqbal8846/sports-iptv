# Sports IPTV — Auto-Updating Football & Cricket M3U

[![Auto Update Sports M3U](https://github.com/iqbal8846/sports-iptv/actions/workflows/update_playlist.yml/badge.svg)](https://github.com/iqbal8846/sports-iptv/actions/workflows/update_playlist.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Automated, zero-dependency M3U playlist aggregator and live stream health checker for **Football/Soccer** and **Cricket** channels from open-source GitHub playlists. Refreshes automatically **every 6 hours** via GitHub Actions.

---

## Live M3U Streaming Links

Copy and paste any link below into **VLC**, **Televizo**, **TiviMate**, **OTT Navigator**, or **Kodi**:

| Category | Permanent Auto-Updated URL |
| :--- | :--- |
| **Android App Matches API (JSON)** | `https://iqbal8846.github.io/sports-iptv/output/matches.json` |
| **Android App Raw Backup (JSON)** | `https://raw.githubusercontent.com/iqbal8846/sports-iptv/main/output/matches.json` |
| **All Sports (Combined M3U)** | `https://raw.githubusercontent.com/iqbal8846/sports-iptv/main/output/sports_all.m3u` |
| **Football / Soccer (M3U)** | `https://raw.githubusercontent.com/iqbal8846/sports-iptv/main/output/football.m3u` |
| **Cricket Live (M3U)** | `https://raw.githubusercontent.com/iqbal8846/sports-iptv/main/output/cricket.m3u` |
| **Web Dashboard (Pages)** | `https://iqbal8846.github.io/sports-iptv/output/` |

---

## Features

- **Every 6 Hours Auto-Update:** Scheduled GitHub Actions workflow removes dead streams and ranks the fastest working links first.
- **Smart Categorization:** Separates dedicated **Cricket**, **Football**, and **Multi-Sport** networks (*T Sports, Star Sports, Sony Sports, Sky Sports, TNT Sports, beIN Sports, Eurosport, Setanta*).
- **BDIX Cloud Protection:** Preserves Bangladesh local ISP (BDIX) streams during cloud builds while verifying them live when run locally.
- **GitHub Auto-Discovery:** Automatically scans newly updated public M3U repositories on GitHub.
- **Zero Dependencies:** Built 100% with Python Standard Library — no `pip install` required.

---

## Adding Custom Sources Later

Edit [`config.json`](config.json) and add either a direct `.m3u`/`.m3u8` URL or any GitHub repository link inside `"sources"`:

```json
{
  "name": "New Sports Source",
  "url": "https://github.com/owner/repo-name",
  "is_bdix": false,
  "default_sports_group": false,
  "enabled": true
}
```

As soon as you commit changes to `config.json`, GitHub Actions automatically rebuilds all playlists.

---

## Disclaimer

This repository does **not** host, rebroadcast, or transmit any video/audio streams. It is an automated parser and health-checker script that aggregates publicly available, non-DRM Free-To-Air (FTA) M3U links indexed in public open-source repositories.
