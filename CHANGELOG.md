# Changelog

Todas as mudanças relevantes deste projeto são documentadas aqui.

O formato é baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/)
e o projeto adere ao [Versionamento Semântico](https://semver.org/lang/pt-BR/).

## [Não lançado]

Primeira linha de desenvolvimento rumo ao `0.1.0`. Ainda sem release publicado.

### Adicionado

- **CLI `cmon`** para rastrear o consumo do plano Claude ao longo do tempo:
  comandos `now`, `collect`, `report` e `plot`, lendo `limits[]` de
  `claude.ai/api/oauth/usage` e gravando snapshots em DuckDB.
- **Cofre de token cross-platform** via keyring (Keychain / Credential Manager /
  Secret Service), com resolução `env -> cofre do SO -> credencial do Claude Code`
  e os comandos `cmon token set/status/clear`.
- **Auto-refresh do token OAuth**: renovação proativa (lê `expiresAt`, 60s de
  folga) e reativa (no 401), com a cadeia guardada em cofre próprio, separada da
  credencial do Claude Code. `client_id`/endpoint configuráveis por env.
- **`cmon tips`**: projeção por janela, alvo de %/h e dicas geradas via
  `claude -p` (Sonnet), aterradas no mix real de modelos das últimas 5h.
- **`cmon watch`**: TUI ao vivo (rich) com barras coloridas, ritmo, projeção,
  alertas e a linha `burn 5h (logs)`; `--collect` grava enquanto observa.
- **`cmon status`**: linha única para statusline/tmux/prompt, degradando de forma
  graciosa quando offline.
- **`cmon wait`**: bloqueia até a janela resetar (ou `--at N%`) e dispara
  notificação nativa.
- **`cmon trends`**: segmenta o histórico por reset, com pico por ciclo, delta
  vs. o anterior e detecção de anomalia.
- **`cmon install/uninstall`**: coleta de fundo via launchd (macOS) /
  systemd-user com fallback cron (Linux) / schtasks (Windows); `--dry-run`.
- **`cmon burn`**: minera os logs locais do Claude Code
  (`~/.claude/projects/**/*.jsonl`) para estimar tokens e US$, com breakdown por
  componente (input/output/cache read/cache write), rótulo honesto
  ("equivalente na API", não fatura) e agrupamento por `model`, `day`, `project`,
  `session` ou `surface` (entrypoint: terminal/vscode/app/sdk).
- **Alertas**: aviso quando, no ritmo atual, a janela bate 100% antes do reset
  (`_notify` best-effort via osascript/notify-send).
- **Robustez**: retry com backoff exponencial respeitando `Retry-After` para
  429/5xx/rede; 401/403 falham com mensagem legível; `collect` com dedup por
  janela e falha não-silenciosa (exit code != 0).
- **Empacotamento open-source**: metadados PyPI, `LICENSE` (MIT), workflow de CI
  (ruff + smoke em 3.11–3.13), gráficos como extra opcional (`plot`).

### Performance

- **`burn` first-scan ~55x mais rápido**: insert vetorizado via DataFrame +
  `drop_duplicates` no lugar de `executemany`+`ON CONFLICT`; scan com orjson,
  leitura binária, pré-filtro por `"usage"` e parse paralelo (ProcessPool).
  Varredura de 520MB/1939 arquivos: 72s -> 1.3s; incremental 1.1s -> 0.28s.
- **`status` ~12x mais rápido**: lê cache local (`~/.cmon/status.json`) antes de
  banco e API, tirando a rede do caminho quente da statusline (~50ms vs. ~590ms).

### Alterado

- `requires-python` relaxado de `>=3.14` para `>=3.11` (o código só precisa de
  3.11); `.python-version` em 3.12.
- Nome de distribuição no PyPI passa a ser `cmon-cli` (o comando de terminal
  continua `cmon`), pois `cmon` já estava tomado.
- `burn` passa a usar janela padrão de 30 dias (o Claude Code apaga transcripts
  com mais de 30d); `--since all` varre todo o histórico disponível.
