# Plano: fallback automático de provider ao estourar limite

Baseado em pesquisa de 2026-07-08. Objetivo: quando o limite do plano Anthropic
(janela 5h ou weekly) chega em **99%**, `cmon watch` arma automaticamente um
fallback pro **z.ai (GLM)** — que é Anthropic-compatible — reescrevendo o
`~/.claude/settings.json` pra próxima sessão do Claude Code apontar pro GLM.

## Premissa arquitetural (o nó)

`cmon` **observa, não executa**. Ele lê `claude.ai/api/oauth/usage`, guarda em
DuckDB e mostra `burn`/`watch`/`trends` ([cmon.py:414](cmon.py#L414) `fetch`,
[cmon.py:450](cmon.py#L450) `limits`). Ele **não está no caminho da request** do
Claude Code — quem escolhe provider/modelo é o `claude` CLI, via env vars lidas
**no start** do processo.

Consequência confirmada por pesquisa: **Claude Code lê `~/.claude/settings.json`
só ao iniciar; sessão já aberta NÃO relê mid-run.** Logo, não existe hot-swap na
sessão viva sem um proxy. O fallback é *"na próxima sessão"*, não *"na sessão
atual"*:

```
watch detecta 99% → grava fallback no settings.json → avisa "reinicie o claude"
                  → nova janela do claude sobe no GLM
```

Como o usuário já roda `watch` justamente quando está perto do limite, o
"reinicie agora" é aceitável — o watch é o ponto de visibilidade e vira o
executor.

**Por que não o `fallbackModel` nativo do Claude Code?** Ele existe
(`--fallback-model` / setting `fallbackModel`), mas é **same-provider** e dispara
em **erro/overload de request** (ex. Fable→Opus quando um request é recusado) —
não em **quota de plano estourada**, e não troca de endpoint/provider. Não cobre
o caso "5h em 99% → ir pro GLM". Por isso o cmon-fallback não reinventa nada
nativo.

## Mecanismo de troca (Claude Code → z.ai)

Bloco `env` em `~/.claude/settings.json` (drop-in Anthropic-compat, sem proxy/
tradutor):

```json
{"env": {
  "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
  "ANTHROPIC_AUTH_TOKEN": "<z.ai key>",
  "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.2",
  "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.2",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-4.7",
  "CLAUDE_CODE_SUBAGENT_MODEL": "glm-5.2"
}}
```

Valores confirmados na doc oficial da z.ai (`docs.z.ai/devpack/tool/claude`):
`OPUS/SONNET→glm-5.2`, `HAIKU→glm-4.7`. `CLAUDE_CODE_SUBAGENT_MODEL` é
**essencial** — sem ela, subagents do Claude Code continuam resolvendo pro Opus
da Anthropic e furam o fallback justamente quando a quota estourou (a comunidade
— `claude-code-switch`/`ccm` — seta exatamente esse conjunto de 7 env vars).

- Endpoints Anthropic-compat: `https://api.z.ai/api/anthropic` (padrão) ou
  `https://api.z.ai/api/coding/paas/v4` (GLM Coding Plan). Configurável, mas
  **host-allowlisted** (ver Segurança) — config/`.env` não pode redirecionar a
  key pra host arbitrário.
- z.ai tem endpoint OpenAI-compat `.../api/openai/v1/models` → dá pra listar
  modelos em runtime (ver "Resolução dinâmica").

> ⚠️ **A key sai do vault e vai a cleartext no `settings.json` enquanto
> armado.** É o custo de usar o mecanismo nativo do Claude Code. Mitigações:
> 0600 no arquivo; garantir remoção no restore (o backup é do estado *sem* key,
> então reverter apaga a key); avisar o usuário. **Hardening opcional**: usar
> `apiKeyHelper` do Claude Code (script que devolve a key em runtime) em vez de
> `ANTHROPIC_AUTH_TOKEN` literal — o cmon grava o caminho do helper, a key nunca
> toca o disco em claro. Se armado nunca for revertido (crash), a key fica no
> settings — `fallback status` deve sinalizar isso.

## Estado da z.ai (pesquisa 2026-07-08)

- Flagship: **GLM-5.2** (jun 2026, benchmarks ~par com Opus 4.7–4.8). Também
  **GLM-5-Turbo** e **GLM-4.7**. Variante 1M context: `glm-5.2[1m]`.
- Nomes de modelo mudam rápido → **não hardcodar**; resolver dinâmico com pin
  manual e fallback estático (ver abaixo). O `cmon` deve rodar a busca/list de
  modelos em vez de fixar a versão.

## UX / CLI

- **`cmon watch -f` / `--fallback`** — ao cruzar 99% (5h **ou** weekly, ativo),
  grava o bloco z.ai no `settings.json` **automaticamente** + alerta destacado
  "z.ai armado — reinicie o Claude Code". No **reset** da janela, **reverte**
  sozinho pro estado original.
- **`cmon watch -f -k` / `--keep`** — igual, mas **não reverte** no reset: fica
  no z.ai até `cmon fallback off`.
- **`cmon fallback set-key`** — guarda a z.ai key no vault (keyring), mesmo
  padrão do `token set`; sem cleartext. Override por env `Z_AI_API_KEY`.
- **`cmon fallback status`** — mostra se está armado, qual provider/modelo, e a
  origem da key (mascarada).
- **`cmon fallback off`** — restaura o estado original (escape manual;
  idempotente = no-op se nada armado).

Gatilho fixo em 99% (configurável via `fallback.threshold`). Armar é automático,
sem confirmação — zero fricção no momento crítico.

## Requisito central: restauração byte-idêntica

Ao reverter, o `settings.json` deve voltar **exatamente** ao que era antes, sem
perda nem alteração de estado.

- **Backup = cópia literal dos bytes** do arquivo original (não
  `json.load`→`json.dump`, que reordena chaves, reindenta, mexe no newline
  final). Restaurar = copiar os bytes de volta, idêntico.
- **Se o arquivo não existia** antes de armar → restaurar **apaga** o arquivo
  (volta ao estado "inexistente"), não deixa um `{}` órfão.
- **Escrita atômica** (tmp no **mesmo dir** + `os.replace`) ao armar e ao
  restaurar — crash no meio nunca corrompe. Resolver symlinks antes (não seguir
  um `settings.json` que virou symlink pra fora).
- **Permissões restritas (0600)** no `settings.json` armado **e** no backup — o
  backup copia o settings original, que pode já conter segredos do usuário;
  gravá-lo com permissão frouxa espalha esses segredos. Dir `~/.cmon` em 0700.
- **Proteção contra edição externa**: ao armar, guardar o hash do que o `cmon`
  escreveu. Na restauração, se o `settings.json` atual ≠ esse hash, o usuário
  editou à mão enquanto o fallback estava ativo → **não** sobrescrever cego;
  avisar e apontar o backup pra merge manual.
- **Lockfile** (`~/.cmon/fallback.lock`) — impede dois `cmon watch -f`
  simultâneos de armar/restaurar em corrida (TOCTOU sobre o mesmo settings).
- **Idempotência**: `fallback off` sem nada armado é no-op.

Efeito prático: o estado "armado" (z.ai) pode ficar reformatado (é temporário);
o estado "restaurado" é bit-idêntico ao original — incluindo ausência de
arquivo, ordem de chaves e whitespace.

## Config (`~/.cmon/config.json`, infra `cmon config` já existe)

```json
{"fallback": {
  "provider": "zai",
  "threshold": 99,
  "trigger_windows": ["session", "weekly_all"],
  "base_url": "https://api.z.ai/api/anthropic",
  "model_map": {"opus": null, "sonnet": null, "haiku": null},
  "model_cache_ttl_h": 24
}}
```

`model_map` `null` = auto-resolve. Preencher com um id (ex. `"glm-5.2"`) = pin.

## Resolução dinâmica do modelo

Ao armar, nesta ordem:

1. **Pin manual vence** — se `fallback.model_map.<slot>` está preenchido, usa
   exatamente esse id (reprodutibilidade sob demanda).
2. **Auto (default)** — `GET .../api/openai/v1/models` (auth c/ z.ai key), filtra
   família GLM, ordena por versão, escolhe o topo: `opus`/`sonnet`/`subagent` →
   maior flagship (hoje `glm-5.2`); `haiku` → tier leve estável (hoje `glm-4.7`,
   valor da doc oficial da z.ai).
   **Cache 24h** em disco (`model_cache_ttl_h`) pra não bater na rede a cada
   ciclo nem a cada execução do watch. **Timeout curto (≤3s)** — é o momento
   crítico; rede lenta não pode travar o armar → cai pro estático.
   **Sanitizar cada id** antes de gravar no settings: aceitar só
   `^glm-[0-9a-z.\-\[\]]+$`, descartar o resto. A resposta da API vira valor
   escrito em `~/.claude/settings.json` — sem validação, uma resposta adulterada
   injeta string arbitrária ali.
3. **Fallback estático** — se a listagem falhar (offline / schema mudou /
   timeout), usa default embutido (hoje `opus/sonnet/subagent=glm-5.2`,
   `haiku=glm-4.7`) + avisa no watch que não confirmou a versão viva.

## Conformidade (docs oficiais + comunidade)

- **z.ai** (`docs.z.ai/devpack/tool/claude`, 2026-07-08) — o bloco `env` acima é
  o setup oficial, valor por valor.
- **Claude Code** (`code.claude.com/docs/en/model-config`) — `ANTHROPIC_DEFAULT_*_MODEL`
  é o mecanismo oficial de alias (v2.1.176+); com `ANTHROPIC_BASE_URL` custom o
  Claude Code repassa qualquer string de modelo sem validar retirement (ids GLM
  passam limpos); settings/org lidos **só no startup** (confirma "reinicie");
  `apiKeyHelper` é setting oficial (embasa o hardening da key).
- **Comunidade** (`claude-code-switch`/`ccm`, ZenMux, etc.) — fazem o switch via
  os mesmos 7 env vars, escrevendo em `~/.claude/settings.json` (user-level, que
  **tem precedência sobre env vars soltas** → garante que o fallback vença
  configs prévias do usuário). Diferença a nosso favor: essas ferramentas
  **não fazem backup** antes de escrever; nosso plano faz backup byte-idêntico +
  restauração fiel — mais seguro que a prática corrente.

## Mudanças no código (`cmon.py`)

1. **Config** — adicionar chaves `fallback.*` (reusa `CONFIG_KEYS` e o mecanismo
   de `~/.cmon/config.json` da seção Config do README).
2. **Vault** — z.ai key no keyring (service próprio, ex. `cmon-zai`), mesmo
   padrão de `token set`/[README Token]; override `Z_AI_API_KEY`; sem cleartext.
3. **settings.json I/O** — `_arm_fallback()` (valida host-allowlist do
   `base_url` → backup literal 0600 → merge cirúrgico só do `env`, preservando o
   resto → escrita atômica 0600) e `_restore_fallback()` (restaura bytes / apaga
   se não existia / recusa se editado à mão). Sob lockfile.
4. **Resolução de modelo** — `_resolve_glm_models()` (pin → list API + cache →
   estático).
5. **watch loop** ([cmon.py:1117](cmon.py#L1117)) — no ciclo: `percent ≥
   threshold` → arma (uma vez por janela); `resets_at` passou + percent caiu →
   restaura, salvo `--keep`. Restaura também no Ctrl-C (exceto `--keep`).
   Reusar `_fire_lead_alert`/hook pro alerta.
6. **CLI** — flags `-f/--fallback` e `-k/--keep` no parser do `watch`; novo
   subcomando `fallback {set-key,status,off}`.
7. **Docs** — README (nova seção "Fallback"), CHANGELOG, `.env.example`
   (`Z_AI_API_KEY`).

## Segurança

Ordenado por severidade.

1. **Exfiltração via `base_url` (alto)** — `fallback.base_url` vem do config/env
   do usuário e é pra onde a z.ai key e todo o tráfego do Claude Code vão. Um
   `.env`/config adulterado apontando pra host atacante vaza a key. **Mitigação:
   host-allowlist a `*.z.ai`** (mesma técnica que o README já usa pro
   `CMON_OAUTH_TOKEN_URL`, travado em `*.anthropic.com`). Recusar armar se o host
   não casar.
2. **Key em cleartext no `settings.json` (alto)** — ver aviso na seção
   "Mecanismo". Mitigar com 0600 + remoção garantida no restore + `apiKeyHelper`
   opcional + `fallback status` sinalizando "armado" (key exposta).
3. **Backup espalha segredos existentes (médio)** — o backup literal copia o
   settings original, que pode já ter tokens do usuário. Backup em 0600, dir
   `~/.cmon` 0700.
4. **Injeção via resposta da list de modelos (médio)** — o id retornado pela API
   é escrito no settings. Sanitizar (`^glm-[0-9a-z.\-\[\]]+$`); descartar o resto.
5. **Segredos em log/erro (médio)** — nunca logar a z.ai key nem headers de auth
   ao chamar a list de modelos / em traceback de falha do armar.
6. **TLS + timeout na list de modelos (baixo)** — HTTPS obrigatório (mesma
   allowlist de host), timeout ≤3s, sem downgrade.
7. **Symlink no settings/backup (baixo)** — resolver e recusar caminhos que
   viraram symlink pra fora de `~/.claude` / `~/.cmon`.

## Performance

- **Não tocar disco/rede a cada tick** — a checagem por ciclo do watch é só
  comparar `percent ≥ threshold` (barato). Armar é **uma vez por janela**
  (guardar estado em memória + hash); nada de stat/hash/reescrita a cada
  intervalo.
- **List de modelos: cache 24h em disco** — sobrevive entre execuções do watch;
  só consulta a rede no primeiro armar do dia. Timeout ≤3s protege o momento
  crítico; falha → estático, sem travar.
- **Restore no Ctrl-C / signal** — handler rápido, I/O local apenas; nunca
  bloquear em rede na saída.
- **Reset detection barata** — reusa o `resets_at`/`percent` que o watch já
  busca por ciclo ([cmon.py:450](cmon.py#L450)); sem chamada extra.

## Riscos residuais

- **Escrever em `~/.claude/settings.json` é território novo** — hoje `cmon` só
  **lê** de lá. Mitigado por restauração byte-idêntica + backup + escrita
  atômica + proteção contra edição externa + lockfile.
- **Nomenclatura de modelo z.ai instável** — mitigado por resolução dinâmica +
  pin + fallback estático.
- **Endpoint de list de modelos não-contratado** — se mudar, cai no fallback
  estático sem quebrar o armar.
- **Reset falso-positivo revertendo cedo** — exigir `resets_at` ultrapassado
  **e** queda de `percent` antes de restaurar, não só um dos dois.

## Fora de escopo (por ora)

- **OpenRouter** como destino — é OpenAI-shaped, exigiria proxy tradutor no
  caminho da request. Só cabe num modelo de proxy, não no launcher/watch atual.
- **Hot-swap na sessão viva** — impossível sem proxy (Claude Code não relê
  settings mid-run). Reavaliar só se entrar um modo proxy no futuro.
- **Quota % do próprio z.ai** — fora daqui; ver Fase 5 do
  [MULTI_PROVIDER_PLAN.md](MULTI_PROVIDER_PLAN.md).

## Ordem sugerida

1. settings.json I/O (arm/restore byte-idêntico) + testes de restauração — é o
   núcleo de risco.
2. Config + vault (key z.ai).
3. Resolução dinâmica de modelo (com fallback estático primeiro, list API
   depois).
4. Integração no watch loop + flags CLI `-f`/`-k`.
5. Subcomando `fallback` + docs.
