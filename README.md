# cmon — Claude Monitor

[![CI](https://github.com/LkHideki/cmon/actions/workflows/ci.yml/badge.svg)](https://github.com/LkHideki/cmon/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

CLI para rastrear o consumo do seu plano Claude ao longo do tempo. Lê o mesmo
endpoint que o app usa (`https://claude.ai/api/oauth/usage`), guarda snapshots
em DuckDB e mostra ritmo de consumo, projeções e gráficos.

## Instalação

```bash
git clone https://github.com/LkHideki/cmon && cd cmon
uv sync                  # instalação enxuta (sem libs de gráfico)
uv sync --extra plot     # opcional: habilita o comando `plot` (matplotlib/seaborn)
```

Requer Python ≥ 3.11 e [uv](https://docs.astral.sh/uv/). Sem uv, um
`pip install -e .` também funciona.

## Token

O `cmon` resolve o token nesta ordem, parando no primeiro que encontrar:

1. **`CLAUDE_OAUTH_TOKEN`** — variável de ambiente (ideal em CI / override).
2. **Cofre seguro do SO** — Keychain (macOS), Credential Manager (Windows) ou
   Secret Service (Linux). Gravado uma vez, sem ficar em texto puro:

   ```bash
   cmon token set        # cola o token (input oculto); ou:  echo $TOK | cmon token set
   cmon token status     # de onde vem o token, mascarado
   cmon token clear      # remove do cofre
   ```
3. **Credencial do Claude Code** — se você estiver logado, é lida direto do
   Keychain (macOS) ou de `~/.claude/.credentials.json` (Linux/Windows). Zero
   atrito: nada a configurar.

**Auto-refresh:** quando o access token expira, o `cmon` o renova sozinho via
`refresh_token` e guarda a nova cadeia num cofre próprio (`claude-oauth-auto`),
**sem** regravar a credencial do Claude Code. Renova de forma proativa (lê o
`expiresAt`) e reativa (se a API devolver 401 — inclusive quando um
`CLAUDE_OAUTH_TOKEN` velho está sombreando tudo). Efeito colateral: a 1ª
renovação gira o `refresh_token` do Claude Code, então **ele pode pedir login uma
vez** na próxima vez que for renovar — depois disso as duas cadeias ficam
independentes. `token status` mostra a validade; `client_id`/endpoint são
configuráveis por `CMON_OAUTH_CLIENT_ID` / `CMON_OAUTH_TOKEN_URL`.

Ou seja, com o Claude Code logado não precisa de nada — e continua funcionando
mesmo com o token vencido. Sem ele, `cmon token set` guarda o token com segurança
em qualquer sistema. `.env` continua funcionando para o passo 1 (veja
`.env.example`). Rode `cmon --help` ou `cmon token --help` para o resto.

## Uso

```bash
uv run cmon now       # uso atual + tempo até o reset + ritmo/projeção
uv run cmon status    # linha única p/ statusline/tmux/prompt
uv run cmon watch     # TUI ao vivo, atualiza sozinho (Ctrl-C sai)
uv run cmon wait      # bloqueia até a janela de 5h resetar, então notifica
uv run cmon collect   # grava 1 snapshot no banco (com timestamp UTC)
uv run cmon report    # resumo do consumo acumulado
uv run cmon trends    # consumo por ciclo (pico, delta vs anterior, anomalia)
uv run cmon burn      # tokens & US$ estimado (dos logs locais do Claude Code)
uv run cmon plot      # gráficos -> usage.png
uv run cmon tips      # dicas de pacing (usar ~100% do semanal sem travar o 5h)
uv run cmon install   # agenda a coleta de fundo no agendador do SO
```

Opção global `--db PATH` (antes do subcomando) sobrepõe `CMON_DB`:
`uv run cmon --db ~/.cmon/usage.duckdb now`.

### `cmon status` — statusline

Uma linha compacta, ideal pra barra de status / tmux / prompt. Sai com código 0
e imprime `cmon offline` se a rede falhar (não quebra a statusline):

```
5h 18% · sem 42% · reset 3h18m
```

### `cmon wait` — avisa quando liberar

Bloqueia até a janela resetar e então dispara uma notificação nativa — pra você
retomar no segundo em que o 5h libera. Ou use `--at N` p/ avisar ao *atingir* N%:

```bash
uv run cmon wait                      # espera o 5h resetar
uv run cmon wait --window weekly_all  # espera o semanal resetar
uv run cmon wait --at 80              # avisa quando o 5h chegar a 80%
```

### `cmon trends` — tendência entre ciclos

Segmenta o histórico em ciclos (corta em cada reset) e mostra o pico de cada um,
o delta em relação ao ciclo anterior e um aviso se o ciclo atual destoa da média.

### `cmon burn` — tokens & custo (dos logs)

Enquanto o resto do `cmon` lê o **% oficial** do endpoint, o `burn` minera os
transcripts locais do Claude Code (`~/.claude/projects/**/*.jsonl`) para dar o que
o endpoint não expõe: **tokens e US$ estimado por modelo, dia, projeto ou sessão**
— retroativo, offline, sem token.

```bash
uv run cmon burn                    # por modelo
uv run cmon burn --by project       # atribuição por projeto (onde seu plano foi)
uv run cmon burn --by day --since 7d
uv run cmon burn --json
```

A varredura é incremental (cacheia por `mtime`+tamanho, deduplica por `uuid`): a
primeira vez lê tudo (~dezenas de segundos em bases grandes), as seguintes levam
frações de segundo. Os mesmos números aparecem no `watch` (linha *burn 5h*) e no
`tips` (mix de modelos das últimas 5h, que aterra a dica de troca de modelo).

Cruzando as duas fontes: a **API** diz *onde está a parede* (% oficial + reset), os
**logs** dizem *como você gastou* (qual modelo/projeto drenou). Ressalvas: o custo é
**estimativa** (tabela de preços editável no topo de [`cmon.py`](cmon.py)), e os logs
cobrem **só o Claude Code CLI** — uso no claude.ai web/desktop não aparece (mas conta
no % oficial).

### `cmon watch` — TUI ao vivo

Painel que se atualiza sozinho: barras coloridas por janela (verde/amarelo/
vermelho), ritmo `%/h`, projeção no reset e alertas quando você vai bater 100%
antes do reset. Ótimo pra deixar aberto num canto do terminal.

```bash
uv run cmon watch                 # atualiza a cada 30s
uv run cmon watch -n 10           # a cada 10s
uv run cmon watch --collect       # grava cada leitura no banco enquanto observa
```

### Alertas

`_alerts` avisa quando, **no ritmo atual, a janela bate 100% antes do reset**.
Aparecem em `now` e `watch`; no `collect --alert` vão pro stderr (o cron manda
por e-mail) e disparam uma notificação nativa best-effort (macOS/Linux):

```cron
*/20 * * * * cd ~/cmon && /caminho/para/uv run cmon collect --alert
```

### `cmon report`

```bash
uv run cmon report --since 24h    # só as últimas 24h (aceita 7d ou data ISO)
uv run cmon report --json         # saída em JSON p/ script/pipe
```

### `cmon tips`

Objetivo: gastar perto de **100% do limite semanal** até o reset — sem esgotar
antes e sem estourar a **janela de 5h**, que trava o uso. Para cada janela mostra
o ritmo observado, o alvo `%/h` para zerar a folga e a projeção no reset:

- **projeção < 100%** → *upside*: sobra cota, dá pra intensificar ou usar modelo
  mais forte;
- **projeção > 100%** → *vai faltar*: em quantas horas você bate 100% antes do
  reset e para quanto frear.

O ritmo corta automaticamente no último reset, então se adapta a janelas de 5h,
7d (ou 72h — a Anthropic reseta o "semanal" num horário fixo por conta, nem
sempre em 7 dias exatos). Por fim, passa os números pro **Claude Sonnet**
(`claude -p`, barato) que devolve 3 dicas acionáveis. Use `--no-ai` para só as
projeções locais, sem gastar cota.

`cmon now` responde na hora "quanto falta pra minha janela de 5h resetar" e,
se já houver histórico, projeta se você vai bater o limite antes disso:

```
Uso atual:
  Current session  █··················   7%    reseta em   4h 25min
  All models       ████████··········  41%    reseta em 3d 22h
  Fable only       ████████··········  43%    reseta em 3d 22h  ←ativo

Janela de 5h: 7% usada — expira em 4h 25min.
Ritmo: 2.1%/h → projeção no reset: 16%.
```

## Coleta contínua

`report`/`plot`/`trends`/alertas ficam úteis com histórico. O jeito fácil é deixar
o `collect` agendado no agendador nativo do SO:

```bash
uv run cmon install            # a cada 20min (launchd/systemd/schtasks)
uv run cmon install -i 10      # a cada 10min
uv run cmon install --dry-run  # só mostra o que faria
uv run cmon uninstall          # remove
```

No background o token vem do cofre do SO ou da credencial do Claude Code — a env
`CLAUDE_OAUTH_TOKEN` do seu shell **não** é herdada, então rode `cmon token set`
se for esse o seu caso. Preferir cron na mão? Continua valendo:

```cron
*/20 * * * * cd ~/cmon && /caminho/para/uv run cmon collect --alert
```

## Como funciona

- **Fonte**: array `limits[]` do endpoint — `session` (janela de 5h),
  `weekly_all` (todos os modelos) e `weekly_scoped` (por modelo, ex. Fable).
- **Consumo**: diferença de `percent` entre snapshots; quedas = reset da janela
  (descartadas), não consumo.
- É preciso o header `User-Agent: claude-cli/...`, senão o Cloudflare do
  claude.ai responde 403.
- **Robustez**: `fetch` tenta de novo em 429/5xx/rede com backoff (respeita
  `Retry-After`); 401/403 falham na hora com mensagem clara. `collect` deduplica
  leituras muito próximas (`CMON_DEDUP_SECS`, padrão 60s; `--force` ignora) e sai
  com código ≠ 0 em falha, então o cron registra o erro em vez de silenciar.

## Desenvolvimento

Tudo vive num único arquivo, [`cmon.py`](cmon.py) — comandos são funções `now`,
`collect`, `watch`, etc., ligadas ao argparse em `main()`. Fácil de ler de cima a baixo.

```bash
uv sync --extra plot         # instala tudo, inclusive libs de gráfico
uv run ruff check .          # lint (config em pyproject.toml)
uv run ruff check --fix .    # corrige o que dá
uv run cmon <cmd>            # roda direto do fonte
```

O CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) roda ruff + um smoke
do CLI em Python 3.11–3.13. PRs bem-vindos: mantenha o `ruff` verde e o estilo
enxuto do arquivo. Variáveis de ambiente úteis: `CMON_DB` (caminho do banco),
`CMON_RETRIES`, `CMON_DEDUP_SECS`.

## Aviso

Usa um endpoint privado e não documentado da Anthropic; pode mudar sem aviso.
Só acessa a sua própria conta. Licença [MIT](LICENSE).
