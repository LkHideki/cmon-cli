# Plano: suporte multi-cliente (além de Claude Code)

Baseado em pesquisa de 2026-07-05. Cobre GLM (Z.ai), Aider, OpenCode, Codex CLI.
Gemini CLI fica em backlog (schema de log não documentado). Cursor CLI fica de
fora (sem token count local nem quota API individual).

## Arquitetura (Fase 0)

Hoje `cmon.py` assume 1 fonte: `LOGS_ROOT` fixo ([cmon.py:1094](cmon.py#L1094)) +
`_parse_jsonl` fixo pro schema do Claude Code ([cmon.py:1129](cmon.py#L1129)).
Generalizar pra registry de providers:

```python
PROVIDERS = {
    "claude": Provider(log_root=..., parser=_parse_jsonl_claude, prices=PRICES),
    "glm":    Provider(log_root=..., parser=_parse_jsonl_claude, prices=PRICES_GLM),  # mesmo parser
    "aider":  Provider(log_root=..., parser=_parse_jsonl_aider,  prices=PRICES_GENERIC),
    "opencode": Provider(log_root=..., parser=_parse_sqlite_opencode, prices=PRICES_GENERIC),
    "codex":  Provider(log_root=..., parser=_parse_jsonl_codex,  prices=PRICES_OPENAI),
}
```

Mudanças de schema:
- `token_log` ganha coluna `provider TEXT` (mesma técnica de migração já usada
  pra `entrypoint`, [cmon.py:1180](cmon.py#L1180): dropa e recria se coluna faltar).
- `scan_logs` ([cmon.py:1170](cmon.py#L1170)) passa a iterar `PROVIDERS.items()`
  em vez de 1 `glob` fixo; cada provider decide seu próprio jeito de descobrir
  arquivos/linhas novas (glob+mtime pra JSONL, `SELECT max(rowid)`/mtime do
  `.db` pra SQLite).
- `burn()` ([cmon.py:1277](cmon.py#L1277)) e `_burn_summary` ganham groupby
  extra por `provider`, além do `model` atual.
- CLI: `pb = sub.add_parser("burn", ...)` ([cmon.py:1593](cmon.py#L1593)) ganha
  `--provider {all,claude,glm,aider,opencode,codex}` (default `all`).

## Fase 1 — GLM (Z.ai), esforço baixo

Proxy Anthropic-compatible: Claude Code grava no MESMO
`~/.claude/projects/**/*.jsonl`, `message.usage` com os mesmos campos. Não
precisa parser novo nem log root novo.

Único trabalho: `message.model` vem tipo `"glm-4.6"` — hoje `_short_model`
([cmon.py:1113](cmon.py#L1113)) só reconhece `opus/sonnet/haiku` por substring
e cai no fallback `model or "?"`; `_price` ([cmon.py:1105](cmon.py#L1105)) cai
em `(0,0,0,0)` pra qualquer chave não listada em `PRICES`
([cmon.py:1097](cmon.py#L1097)) — hoje GLM já aparece no burn com custo **zero**,
sem quebrar nada.

- Adicionar entradas GLM em `PRICES` (checar tabela de preço atual em
  z.ai/pricing) e em `_short_model`.
- Distinguir de Claude puro: como o log é idêntico, só dá pra saber que foi
  GLM olhando o próprio `model` (não tem client marker separado) — ou seja,
  na prática GLM cai automaticamente sob o "provider" claude/jsonl, só o
  `model`/preço muda. Não precisa de `provider` distinto pra esse caso — é
  suficiente resolver por `model` como já é feito hoje.

## Fase 2 — Aider, esforço baixo

Log documentado: `--analytics-log arquivo.jsonl`
(`AIDER_ANALYTICS_LOG`), evento `message_send` com tokens+custo por modelo já
prontos (não precisa nem tabela de preço própria — usar o `cost` do próprio
evento). Schema de exemplo: `sample-analytics.jsonl` no repo do aider.

- Novo parser `_parse_jsonl_aider(path)`: 1 tipo de evento (`message_send`),
  campos diferentes do Claude (`tokens_sent`/`tokens_received`/`cost` — conferir
  nomes exatos no sample antes de implementar).
- Log root: não tem path fixo tipo `~/.claude/projects` — é por-projeto,
  configurável. Precisa descobrir arquivos via config do usuário (env var ou
  flag `cmon config aider-log <path>`), não dá pra fazer glob cego.

## Fase 3 — OpenCode, esforço baixo

Storage é SQLite (`~/.local/share/opencode/opencode.db`, WAL,
override `OPENCODE_DB`/`OPENCODE_DATA_DIR`), tabela `message` já com
`cost, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read,
tokens_cache_write`, `model:{id,providerID,variant}` por sessão.

- Novo coletor `_read_opencode_db(path)`: `sqlite3` read-only (seguro ler com
  WAL ativo, sem lock), 1 query em `session`+`message`, sem precisar de parser
  linha-a-linha.
- Preço: `cost` já vem calculado pelo próprio OpenCode — não precisa duplicar
  tabela de preços, só ler a coluna.
- Risco baixo de quebra: schema Drizzle é público, mas não há garantia de
  estabilidade — travar a query numa versão testada e falhar graciosamente
  (log + skip) se colunas mudarem.

## Fase 4 — Codex CLI (OpenAI), esforço médio

JSONL documentado por engenharia reversa da comunidade:
`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (`CODEX_HOME`), evento
`event_msg` tipo `token_count` com totais cumulativos (não incrementais —
cuidado ao acumular, evitar dupla-contagem: pegar delta entre snapshots, não
somar todos os `token_count` da sessão).

- Novo parser `_parse_jsonl_codex(path)`: extrair `turn_context.model` +
  último `token_count` de cada turno (ou delta entre consecutivos).
- Preço: usar tabela pública de preço da OpenAI por modelo (`PRICES_OPENAI`),
  mesmo formato `(input, output, cache_read, cache_write)`.
- Quota real (janela 5h/semanal) fica de fora nesta fase: só existe via
  headers HTTP não-documentados (`x-codex-*-used-percent`) ou endpoint reverse
  do backend ChatGPT — ver Fase 5.

## Backlog — Gemini CLI

Log existe (`~/.gemini/tmp/<hash>/chats/`, tem token usage) mas schema não é
documentado publicamente. Antes de implementar: inspecionar um arquivo real
gerado localmente pra mapear os campos. Sem isso, parser é chute.

## Fora de escopo — Cursor CLI

Sem token count em log local (feature pedida no fórum deles, não implementada)
e sem API de quota individual (só Admin API enterprise). Não dá pra fazer
burn nem quota confiável hoje. Reavaliar se lançarem isso.

## Fase 5 (opcional, alto risco) — quota real (%) pra GLM/Codex

Hoje `fetch()` ([cmon.py:301](cmon.py#L301)) é hardcoded pro endpoint OAuth
da Anthropic ([cmon.py:29](cmon.py#L29)). Pra GLM e Codex, "quota %" só existe
via endpoint/headers **não-oficiais** (`api.z.ai/api/monitor/usage/*` pro GLM,
`x-codex-*-used-percent` headers pro Codex) — sem contrato estável, pode
quebrar sem aviso a qualquer atualização do provider.

Só entrar nessa fase se o usuário topar manutenção contínua de API não-oficial.
Se sim: generalizar `fetch()`/`limits()` pra registry de "quota providers"
separado do registry de "cost providers" da Fase 0 (são conceitos diferentes:
nem todo provider de custo tem quota, ex. Aider/OpenCode são BYOK puro).

## Ordem sugerida

1. Fase 0 (arquitetura) + Fase 1 (GLM, é grátis) juntas — menor esforço, maior
   valor imediato.
2. Fase 2 (Aider) e Fase 3 (OpenCode) — schemas documentados/públicos, baixo
   risco.
3. Fase 4 (Codex) — schema só documentado pela comunidade, testar contra
   sessões reais antes de confiar.
4. Backlog (Gemini) só depois de inspecionar arquivo real.
5. Fase 5 (quota real não-oficial) só sob demanda explícita — é a única parte
   arriscada/instável do plano.
