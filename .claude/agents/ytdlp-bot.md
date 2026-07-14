---
name: ytdlp-bot
description: Reglas y contexto para trabajar en el bot de descarga de videos de Telegram (yt-dlp).
---

# ytdlp-bot — Reglas del proyecto

## Regla principal: no cambies lo que no te pidieron

Implementa **exactamente** lo pedido. Si el usuario pide cambiar A, solo cambias A. No toques B aunque creas que mejoraría el resultado. No "mejores" el código de paso, no reordenes lo que no está en el pedido, no unifiques, no consolides.

Si quieres sugerir un cambio adicional, dilo en texto — no lo implementes sin pedirlo.

## Reglas de caption (mensajes de video)

El caption tiene 3 bloques separados por línea vacía:

**Bloque 1 — Contenido**
- Descripción cuando existe (hasta 300 chars), precedida de título en negrita si también existe
- Si no hay descripción, solo el título en negrita
- Títulos genéricos de yt-dlp (ej. "TikTok video #123") → omitir

**Bloque 2 — Cuenta y plataforma**
- `👤 <b>nombre</b>` como link clicable al perfil del uploader
- ID del video (`info["id"]`) como texto clicable a `webpage_url` — no usar "Ver en X" (no es generalizable)

**Bloque 3 — Métricas**
- Una métrica por línea, en este orden: 👁 vistas · ❤️ likes · 💬 comentarios · 🔁 reposts
- No mostrar duración (⏱) — no es de interés
- No unificar métricas en una sola línea

## Plataformas con comportamiento especial

- **TikTok** (`tiktok.com`, `vm.tiktok.com`): descarga directa a 1080p sin mostrar botones de calidad

## Infraestructura

- VPS2: `170.9.4.149` (Ubuntu 24.04, 954MB RAM, 4GB swap)
- Stack: `docker-compose` en `/home/ubuntu/ytdlp-bot/`
- Servicios: `tg-api` (local Telegram Bot API, límite 1.9GB), `ytdlp-bot`, `dozzle` (logs en :8080)
- SSH: `ssh -i ~/.ssh/tmp_vps/oracle_vps_key ubuntu@170.9.4.149`
- Deploy via CI/CD: GitHub Actions build en CI → push a GHCR → CD hace pull en VPS (no build en VPS)
- Puertos OCI: abrir en Security List del VCN, no solo en Docker
- Repo: https://github.com/seminarioA/ytdlp2tg

## Commits y GitHub

- Nunca agregar `Co-Authored-By: Claude` ni ninguna forma de co-autoría de IA en commits
- Nunca agregar el footer de Claude Code en PRs, issues o comentarios
- Nunca usar emojis en commits, PRs o issues
- Workflows separados: `ci.yml` (build + push a GHCR) y `cd.yml` (deploy al VPS via SSH)

## Lo que NO hacer

- No usar cookies personales del usuario en el servidor
- No asumir que un puerto expuesto en Docker es accesible — OCI tiene su propio Security List
- No unificar/reformatear/limpiar código que no está en el pedido
