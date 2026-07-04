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

Ou seja, com o Claude Code logado não precisa de nada. Sem ele, `cmon token set`
guarda o token com segurança em qualquer sistema. `.env` continua funcionando
para o passo 1 (veja `.env.example`). Rode `cmon --help` ou `cmon token --help`
para o resto.

## Uso

```bash
uv run cmon now       # uso atual + tempo até o reset + ritmo/projeção
uv run cmon collect   # grava 1 snapshot no banco (com timestamp UTC)
uv run cmon report    # resumo do consumo acumulado
uv run cmon plot      # gráficos -> usage.png
uv run cmon tips      # dicas de pacing (usar ~100% do semanal sem travar o 5h)
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
