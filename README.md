# cmon — Claude Monitor

CLI para rastrear o consumo do seu plano Claude ao longo do tempo. Lê o mesmo
endpoint que o app usa (`https://claude.ai/api/oauth/usage`), guarda snapshots
em DuckDB e mostra ritmo de consumo, projeções e gráficos.

## Instalação

```bash
git clone <seu-fork> cmon && cd cmon
uv sync
```

## Token

No **macOS** nada é preciso: o `cmon` lê a credencial que o Claude Code guarda
no Keychain (e mantém válida sozinho).

Em **Linux/CI**, exporte o token (ou use um `.env`, veja `.env.example`):

```bash
export CLAUDE_OAUTH_TOKEN=sk-ant-oat01-...
```

## Uso

```bash
uv run cmon now       # uso atual + tempo até o reset + ritmo/projeção
uv run cmon collect   # grava 1 snapshot no banco
uv run cmon report    # resumo do consumo acumulado
uv run cmon plot      # gráficos -> usage.png
```

`cmon now` responde na hora "quanto falta pra minha janela de 5h resetar" e,
se já houver histórico, projeta se você vai bater o limite antes disso:

```
Uso atual:
  Current session  █·················· 7.0%    reseta em   4h 25min
  All models       ████████·········· 41.0%    reseta em 3d 22h
  Fable only       ████████·········· 43.0%    reseta em 3d 22h  ←ativo

Janela de 5h: 7.0% usada — expira em 4h 25min.
Ritmo: 2.1%/h → projeção no reset: 16%.
```

## Coleta contínua

O `report`/`plot` ficam úteis com histórico. Agende o `collect` no cron:

```cron
*/20 * * * * cd ~/cmon && /caminho/para/uv run cmon collect
```

## Como funciona

- **Fonte**: array `limits[]` do endpoint — `session` (janela de 5h),
  `weekly_all` (todos os modelos) e `weekly_scoped` (por modelo, ex. Fable).
- **Consumo**: diferença de `percent` entre snapshots; quedas = reset da janela
  (descartadas), não consumo.
- É preciso o header `User-Agent: claude-cli/...`, senão o Cloudflare do
  claude.ai responde 403.

## Aviso

Usa um endpoint privado e não documentado da Anthropic; pode mudar sem aviso.
Só acessa a sua própria conta. MIT.
