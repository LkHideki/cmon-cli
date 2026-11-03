# Claude e Codex operando no mesmo cmon

Adaptar o `cmon` para coletar, armazenar e exibir uso de Claude e Codex pelo mesmo modelo normalizado, preservando todo o histórico e comportamento atuais do provider Claude. A feature estará pronta quando os comandos de quota, histórico, projeção e tokens aceitarem seleção de provider, os logs locais do Codex alimentarem as janelas de 5 horas e semanal sem dupla contagem, a migração do DuckDB for retrocompatível e a suíte automatizada provar os dois providers com fixtures sintéticas.

<!-- plan-meta
id: plan-260918-190354
criado: 2026-09-18 19:03:54 -03
status: em-execução
passes: 5/5 (1 cobertura, 2 decomposição, 3 dependências e ordem, 4 verificação e risco, 5 corte e fecho)
fonte: "Isso pode usar esse padrão e pode fazer um plano de ação para implementar essa feature, só que crie em outra branch a partir da main."
premissas:
  - A branch parte da main local, limpa e 17 commits à frente de origin/main.
  - Claude e Codex serão adapters independentes atrás de um contrato normalizado.
  - O Codex será lido localmente de $CODEX_HOME/sessions ou ~/.codex/sessions, sem endpoint privado adicional e sem credencial OpenAI.
  - O provider padrão será all, mostrando apenas providers detectados; seleção explícita continuará disponível.
  - Percentuais de quota do Codex são estado observado, não API pública; leituras expiradas serão sinalizadas como stale.
  - Custos do Codex serão rotulados como equivalência de API, nunca como valor faturado da assinatura ChatGPT.
  - O projeto continuará distribuído como módulo único cmon.py, conforme a convenção atual.
  - Eventos token_usage_record são cumulativos dentro do turno; a entidade faturável é um registro lógico por (provider, thread_id, turn_id), atualizado pelo snapshot coerente mais novo.
  - observed_at é o timestamp da fonte e governa séries/staleness; ingested_at registra somente quando o cmon persistiu o evento.
  - O default all e o envelope JSON multi-provider são mudança pública preparada para 0.6.0; --provider claude preservará contratos legados para scripts.
  - status preservará a política atual de sair com código 0 em indisponibilidade; os demais comandos seguem falha explícita/falha parcial por capacidade.
  - Stale Codex significa observed_at com mais de 15 minutos ou resets_at vencido; a leitura continua visível com idade, mas não alimenta projeção, advice, forecast, alerta, hook ou wait --at.
  - A revisão encontrou 22 JSONL Codex (~227 MB, maior ~206 MB), tornando obrigatório ler append em O(delta), não reprocessar o arquivo inteiro.
-->

## Fora de escopo
- Consultar endpoints privados da OpenAI ou automatizar `/status`.
- Alterar autenticação, token ou refresh OAuth da Anthropic.
- Transformar o monólito em pacote com múltiplos módulos nesta feature.
- Importar histórico remoto de ChatGPT/Codex Cloud que não esteja nos JSONL locais.
- Prometer custo faturado exato para uso incluído na assinatura ChatGPT.
- Publicar pacote, criar tag ou enviar a branch ao remoto.
- Renomear o comando `cmon`, o pacote PyPI ou o comando `token`; apenas título/descrição e ajuda passam a refletir múltiplos providers.

## Tarefas

- [ ] T1 — Fixar o contrato de providers e os comportamentos públicos
- - [ ] T1.1 — Criar em `cmon.py` os registros normalizados `UsageLimit` e `TokenUsage` com invariantes explícitos: provider/key locais separados, observed/ingested time, input não-cacheado, cache read/write, output total e reasoning informativo/subconjunto · verif: testes rejeitam NaN/Inf, percent fora de 0..100, tokens negativos/bool e provam que reasoning/cache não contam duas vezes
- - [ ] T1.2 — Definir capacidades `quota|tokens|history`, papéis de janela `5h|weekly` e resolvedor puro `all|claude|codex` por comando, com ordem estável `claude,codex` · dep: T1.1 · verif: testes cobrem credencial sem logs, logs sem quota, DB sem diretório, premium-only, provider ausente e valor inválido sem I/O
- - [ ] T1.3 — Criar harness e baseline antes das mudanças
- - - [ ] T1.3.1 — Adicionar pytest e helpers de CLI, relógio, rede, FS e DuckDB temporários · verif: suíte vazia roda sem usar `$HOME`, rede ou banco real
- - - [ ] T1.3.2 — Capturar golden baseline Claude atual de stdout, stderr, JSON e exit code para `now/status/trends/burn/backtest/tune`, mais parser/help dos demais comandos · dep: T1.3.1 · verif: fixtures executam determinísticas sem credencial real
- - [ ] T1.4 — Definir contrato `ProviderAdapter` por capacidade e erros tipados `ProviderUnavailable|ProviderError|MigrationError`; adapters não abrem DB e não chamam `sys.exit` · dep: T1.1, T1.2 · verif: provider Claude sem token retorna erro tipado e não impede execução Codex em `all`
- [ ] T2 — Migrar o armazenamento DuckDB sem perder histórico Claude
- - [ ] T2.1 — Evoluir o schema persistente
- - - [ ] T2.1.1 — Adicionar versão de schema e helpers de staging idempotentes, sem commit próprio, em `db()` · dep: T1.1, T1.3.1 · verif: criar/validar duas vezes um banco novo produz o mesmo schema
- - - [ ] T2.1.2 — Reconstruir `snapshots` com `provider`, `window_minutes`, `observed_at`, `ingested_at`, `source_event_id NOT NULL` e `UNIQUE(provider,source_event_id,key)` · dep: T2.1.1 · verif: primary/secondary do mesmo evento coexistem e replay não aumenta a contagem
- - - [ ] T2.1.3 — Reconstruir `token_log` com chave lógica do turno/provider, reasoning e cache normalizados; reconstruir `scanned` com PK `(provider,path)`, offset do último newline, identidade/fingerprint e tamanho/mtime · dep: T2.1.1 · verif: IDs iguais entre providers coexistem e append avança cursor sem reparse total
- - - [ ] T2.1.4 — Reconstruir `forecasts` com `provider NOT NULL` e identidade/joins por `(provider,key,horizon,target)` · dep: T2.1.1 · verif: forecasts Claude/Codex homônimos reconciliam somente com seus snapshots
- - - [ ] T2.1.5 — Criar estado mínimo de turnos Codex por `(thread_id,turn_id)` para contexto allowlisted e versão cumulativa de usage · dep: T2.1.1 · verif: contexto e usage separados entre dois scans convergem para uma linha lógica completa
- - - [ ] T2.1.6 — Definir migração de chaves `meta`/caches persistentes: parâmetros legados viram Claude por janela; estado global continua global; alert markers ganham provider/key · dep: T2.1.1 · verif: parâmetros Claude e intervalos globais mantêm valores anteriores
- - [ ] T2.2 — Migrar dados existentes com segurança
- - - [ ] T2.2.1 — Preparar backfill de `snapshots/forecasts` legados para Claude, usando `ts→observed_at`, tempo de migração→ingested_at e IDs determinísticos com ordinal que preservem duplicatas · dep: T2.1.2, T2.1.4 · verif: fixture 0.5 conserva 100% de contagens/agregados/forecasts
- - - [ ] T2.2.2 — Preparar backfill de `token_log/scanned/meta` legados sem revarrer logs, cobrindo schema 0.5 e schema anterior sem `entrypoint` · dep: T2.1.3, T2.1.5, T2.1.6 · verif: fixtures sem `~/.claude` preservam tokens, caminhos, PKs e parâmetros
- - - [ ] T2.2.3 — Injetar falhas em cada fase de staging/swap e provar rollback sem schema parcial · dep: T2.2.1, T2.2.2 · verif: cada falha reabre o banco no schema/contagens originais
- - - [ ] T2.2.4 — Orquestrar uma única transação v0→v1 para todas as tabelas, validar contagens/agregados/constraints, gravar `schema_version` por último e propagar busy/falha como `MigrationError` · dep: T2.2.3 · verif: sucesso é idempotente; DB ocupado e falha não são engolidos por `_snapshot`
- - [ ] T2.3 — Versionar cache v2 por provider/janela e fazer merge parcial atômico via temp no mesmo diretório, flush/fsync e `os.replace`; cache v1 vira Claude · dep: T1.1, T1.3.1 · verif: update Claude preserva Codex, leitor concorrente nunca vê JSON parcial, corrupção é rejeitada e temp é limpo
- [ ] T3 — Encapsular o fluxo Claude no novo contrato sem regressões
- - [ ] T3.1 — Implementar adapter Claude de quota contra T1.4 preservando OAuth, allowlist, retry, `Retry-After` e mensagens de erro · dep: T1.3.2, T1.4 · verif: testes mockados reproduzem 200/401/403/429/5xx sem `sys.exit`
- - [ ] T3.2 — Fazer `_parse_jsonl` emitir `TokenUsage(provider="claude")` mantendo UUID, surface, projeto, sessão e semântica de cache existente · dep: T1.1, T1.3.2, T1.4 · verif: fixture Claude produz exatamente os totais anteriores
- - [ ] T3.3 — Atualizar `_snapshot`, `collect` e scanner para consumirem o contrato normalizado sem mudar a saída Claude-only · dep: T2.2.4, T2.3, T3.1, T3.2 · verif: goldens Claude de texto/JSON/stderr/exit code permanecem idênticos
- [ ] T4 — Implementar ingestão local dos logs Codex
- - [ ] T4.1 — Resolver raiz por `CODEX_HOME`/`~/.codex` e aceitar somente arquivos regulares cujo realpath permaneça em `sessions`, sem seguir symlink externo/FIFO · dep: T1.2, T1.3.1, T1.4 · verif: testes cobrem override, fallback, ausência, symlink, FIFO, remoção/permissão durante scan
- - [ ] T4.2 — Implementar parser de turnos Codex
- - - [ ] T4.2.1 — Parsear allowlist de `session_meta/turn_context`: thread/turn/session, modelo, esforço e basename sanitizado do projeto; nunca summary, workspace roots, instruções, prompts, respostas ou tool payloads · dep: T1.1, T1.3.1, T1.4 · verif: fixture com ANSI/controles/strings longas é sanitizada e conteúdo proibido não aparece no retorno/DB
- - - [ ] T4.2.2 — Tratar `token_usage_record.turn_token_usage` como snapshot cumulativo por turno: validar invariantes, escolher versão coerente mais nova/maior e manter response_id/ordinal só como versão · dep: T1.1, T4.2.1 · verif: 96 updates, eventos fora de ordem e dois scans resultam em uma linha com o último total, nunca soma
- - - [ ] T4.2.3 — Usar o último `event_msg.token_count.info.last_token_usage` completo/coerente somente se o turno não tiver evento moderno; ignorar fallback zero/inconsistente · dep: T4.2.2 · verif: fixtures moderna, legada, mista, campos ausentes e total-only geram no máximo um registro válido por turno
- - [ ] T4.3 — Implementar parser de quotas Codex
- - - [ ] T4.3.1 — Mapear `limit_id=codex` primary/secondary para keys locais e papéis públicos `5h/weekly` usando window, percentual finito e epoch UTC · dep: T1.1, T1.3.1 · verif: fixture gera 300/10080 minutos e rejeita percentual/reset inválido sem quebrar as demais janelas
- - - [ ] T4.3.2 — Preservar `plan_type`, `limit_name`, créditos e limites sem janela como metadados, sem confundir `premium` com quota percentual · dep: T4.3.1 · verif: evento premium não cria janela falsa
- - - [ ] T4.3.3 — Gerar IDs sem path: token moderno usa `(session/thread,turn)`; quota usa `session_id+ordinal+limit_id+slot`, com fallback `session_id+timestamp+limit_id+slot` · dep: T4.2.2, T4.3.1 · verif: timestamps iguais entre sessões, ordinal ausente, replay e arquivo movido/copiado não colidem
- - [ ] T4.4 — Integrar ingestão incremental Codex
- - - [ ] T4.4.1 — Ler somente `[offset último newline,tamanho-snapshot)` em O(delta), sem avançar sobre linha parcial; detectar truncate/replace por identidade+fingerprint e limitar linha por `MAX_JSONL_LINE` · dep: T2.1.3, T4.1 · verif: mede bytes lidos em append, truncate, replace mesmo path/tamanho, append durante leitura e JSON/linha inválidos
- - - [ ] T4.4.2 — Na mesma transação, fazer UPSERT de contexto e token lógico por turno, inserir quotas, validar e só então avançar cursor `scanned`; erro não avança offset · dep: T2.2.4, T4.2.3, T4.3.3, T4.4.1 · verif: falha antes/depois de cada fase, replay e append são idempotentes; scan parcial completa contexto depois
- - - [ ] T4.4.3 — Selecionar a observação de quota mais recente globalmente, não pela ordem dos diretórios · dep: T4.4.2 · verif: arquivos fora de ordem retornam o maior timestamp
- - - [ ] T4.4.4 — Provar orçamento de performance/memória do scanner com fixture esparsa de 200 MB sem conteúdo privado · dep: T4.4.2 · verif: segundo scan sem mudança lê 0 payload; append de 4 KB lê O(4 KB), não O(200 MB)
- [ ] T5 — Orquestrar seleção, detecção e falhas parciais de providers
- - [ ] T5.1 — Criar registry estático de adapters/capacidades sem imports pesados; disponibilidade é resolvida por comando a partir de API, FS e histórico DB · dep: T3.1, T3.2, T4.1, T4.4.2 · verif: import não faz I/O e `burn` Codex funciona com DB mesmo sem sessions atuais
- - [ ] T5.2 — Agregar por capacidade: explícito falha não-zero; `all` persiste/cacheia sucessos, reporta erros em stderr/JSON e falha só sem resultado; exceção `status` permanece 0/offline salvo erro de uso · dep: T5.1 · verif: matriz de exit codes/payload para sucesso, parcial, total e status
- - [ ] T5.3 — Adicionar `--provider` após os subcomandos aplicáveis e `CMON_PROVIDER` com precedência CLI > env > all; `install` agenda explicitamente `collect --provider all` · dep: T1.2, T5.1 · verif: valor inválido falha antes de I/O; token/config/uninstall ignoram env; help documenta que Codex live apenas revarre arquivos
- - [ ] T5.4 — Fixar contratos JSON: provider Claude explícito preserva schema atual; all/Codex usam envelope versionado `providers/errors`, stdout contém só JSON e warnings vão para stderr · dep: T1.3.2, T5.2, T5.3 · verif: validação JSON estrita/goldens de trends, burn e backtest
- [ ] T6 — Adaptar quota, cache, projeções, alertas e histórico para múltiplos providers
- - [ ] T6.1 — Particionar queries, `_recent`, `_rate`, `_cycles`, forecasts e caches em memória por `(provider,key)`; escolher policies pelo papel/duração da janela, não por nomes Claude · dep: T2.2.4, T5.1 · verif: Claude session≡Codex primary e weekly_all≡secondary sem misturar delta, half-life ou perfil
- - [ ] T6.2 — Adaptar comandos de leitura/coleta
- - - [ ] T6.2.1 — Agrupar `now/status` por provider; Claude explícito preserva bytes, all usa grupos rotulados com reset/observed age próprios e cache parcial/stale por janela · dep: T2.3, T5.2, T5.3, T5.4, T6.1 · verif: goldens texto/JSON/exit code para Claude, Codex, ambos, offline e parcial
- - - [ ] T6.2.2 — Fazer `collect` persistir todos os providers selecionados, reportando resultado parcial sem perder o snapshot válido · dep: T5.2, T5.3, T6.1 · verif: banco contém o provider saudável quando o outro falha
- - - [ ] T6.2.3 — Fazer `watch` atualizar cada provider com backoff/erro independente e sem writer lock entre polls · dep: T6.2.1, T6.2.2 · verif: 403 Claude mantém painel Codex; erro local não vira retry de rede; dois ciclos fecham conexões
- - - [ ] T6.2.4 — Fazer `wait` operar em um provider e aliases públicos `5h|weekly`, mantendo `session|weekly_all`; all só auto-resolve uma fonte de quota, valida `--at 0..100`/intervalo positivo · dep: T5.3, T6.2.1 · verif: casos ambíguos falham com exemplo `wait --provider codex --window 5h`
- - - [ ] T6.2.5 — Agrupar `trends` e painéis de quota do `plot` por provider sem combinar ciclos/cores/legendas · dep: T5.4, T6.1, T6.2.2 · verif: fixture mista produz séries/JSON separados e PNG válido
- - [ ] T6.3 — Isolar inteligência de pacing
- - - [ ] T6.3.1 — Namespaciar perfis, parâmetros, forecasts, rate/profile caches e meta por provider/janela, mantendo somente settings realmente globais compartilhados · dep: T2.1.6, T6.1 · verif: tune Codex não altera Claude e migração mantém baseline Claude
- - - [ ] T6.3.2 — Executar `_project`, `backtest` e `tune` somente com histórico compatível · dep: T6.3.1 · verif: backtest misto tem métricas separadas e mesmas métricas Claude do baseline
- - - [ ] T6.3.3 — Gerar alertas/hooks rotulados e deduplicados por provider/key, nunca para leitura stale · dep: T6.2.1, T6.3.1 · verif: alertas simultâneos disparam uma vez cada; stale dispara zero
- - - [ ] T6.3.4 — Manter advice determinístico por provider; `_ai_tip` via Claude só em Claude-only ou opt-in explícito, nunca recebe métricas Codex por padrão · dep: T6.2.1, T6.3.1 · verif: Codex/all padrão não invoca subprocesso Claude nem emite dicas de modelos Anthropic
- - [ ] T6.4 — Aplicar stale Codex por janela com `CMON_CODEX_STALE_MIN=15`, resets_at vencido e clock skew; mostrar último observed_at, mas suprimir rate/projeção/advice/forecast/alert/hook/wait-at · dep: T4.4.3, T6.2.1, T6.3.3, T6.3.4 · verif: relógio congelado cobre igualdade no reset, futuro, reset ausente, primary stale/secondary fresh e reabilitação independente
- [ ] T7 — Adaptar tokens, modelos e custo equivalente para Codex
- - [ ] T7.1 — Implementar conversões: Codex `uncached=max(input-cached-cache_write,0)`, reasoning⊆output e total=input+output; Claude preserva semântica atual · dep: T1.1, T3.2, T4.2.3 · verif: invariantes e entradas inconsistentes/extremas nunca geram total/custo duplo
- - [ ] T7.2 — Separar preços por provider com fonte/data/aliases exatos; custo misto desconhecido retorna `known_cost_usd`, `cost_complete=false`, `unknown_models` e humano partial/n-a, nunca `$0` silencioso · dep: T7.1 · verif: conhecidos batem esperado e alias de assinatura sem preço fica incompleto
- - [ ] T7.3 — Rotular valores Codex como “API-equivalent estimate” e manter Claude como estimativa existente · dep: T7.2 · verif: nenhuma saída Codex usa “billed/charged” e snapshots textuais cobrem JSON/humano
- - [ ] T7.4 — Adaptar `burn` e painel burn do `plot` para `(provider,group,model)`; surface Codex ausente vira `unknown`, projeto/sessão são sanitizados · dep: T5.3, T5.4, T7.1, T7.2, T7.3 · verif: fixture mista não soma modelos homônimos e JSON declara custo parcial
- [ ] T8 — Fechar testes, documentação, versão e critérios de release
- - [ ] T8.1 — Consolidar fixtures sintéticas/sanitizadas de Claude, Codex moderno/legado e DuckDB 0.5/anterior; cobrir parser orjson/json, serial/ProcessPool e relógio injetado · dep: T3.3, T4.4.4 · verif: `rg` não encontra prompts, segredos, caminhos/IDs reais; nenhuma fixture depende do Mac do usuário
- - [ ] T8.2 — Adicionar pytest ao dev group e CI Python 3.11–3.13 com DuckDB mínimo suportado e latest, mantendo Ruff/smoke · dep: T8.1 · verif: workflow roda pytest, ruff e help; workers são picklable e fallback serial é coberto
- - [ ] T8.3 — Atualizar documentação e release
- - - [ ] T8.3.1 — Atualizar docstring/help, `README.md`, `.env.example` e `CONTRIBUTING.md`: capacidades, auth Claude-only, logs Codex, privacidade, stale, JSON v2, aliases, falha parcial, scheduler all e custos equivalentes · dep: T6.4, T7.4 · verif: comandos documentados passam e nenhum texto chama custo Codex de faturado
- - - [ ] T8.3.2 — Atualizar `MULTI_PROVIDER_PLAN.md` para registrar a arquitetura entregue e corrigir a premissa antiga sobre quota Codex · dep: T4.4.3, T6.4 · verif: documento não recomenda headers/endpoint como caminho implementado
- - - [ ] T8.3.3 — Preparar `CHANGELOG.md` e `pyproject.toml` para 0.6.0, destacando default all/JSON e recomendando `--provider claude` a scripts legados, sem tag/publish/dist · dep: T8.2, T8.3.1, T8.3.2 · verif: versão e changelog coincidem
- - [ ] T8.4 — Rodar aceite final temporário: migração atômica, coleta Claude mockada, Codex incremental/cumulativo, all/parcial, dedupe, stale, JSON/exit codes, burn, trends, backtest e plot · dep: T8.2, T8.3.3 · verif: registrar números exatos de testes/lint/smoke e bytes do scan incremental; `git status` só contém arquivos intencionais

## Log
- 2026-09-18 19:03 · main · plano → branch `feat/codex-provider` criada a partir da main local limpa
- 2026-09-18 19:18 · root · revisão → três revisões paralelas concluídas (arquitetura, testes/riscos e UX/compatibilidade)
- 2026-09-18 19:21 · root · baseline → `ruff` verde, smoke CLI verde; CodeGraph mapeou 179 nós/504 arestas
- 2026-09-18 19:22 · root · plano → congelado após pass 5; implementação ainda não iniciada
- 2026-09-18 19:31 · root · implementação → autorizada; DAG abaixo passa a rastrear commits

## Incrementos de implementação

- [x] INC-0 — Consolidar branch, plano, AGENTS/CLAUDE e ignore do CodeGraph · dono: root · verif: branch feature ativa e somente arquivos intencionais staged
- [x] INC-1 — Adicionar harness pytest, baseline Claude, fixtures sintéticas e CI · dep: INC-0 · dono: inc1_tests · verif: pytest + ruff + smoke verdes
- [x] INC-2 — Implementar contratos de provider, invariantes, schema/migração atômica e cache v2 · dep: INC-0 · dono: inc2_storage · verif: testes de schema novo/legado/rollback/cache verdes
- [ ] INC-3 — Adaptar Claude e seleção/registry/erros sem regressão pública · dep: INC-1, INC-2 · verif: goldens Claude e matriz de falha parcial verdes
- [ ] INC-4 — Implementar parser e scanner incremental Codex com UPSERT cumulativo · dep: INC-1, INC-2 · verif: parser moderno/legado, replay, append O(delta) e privacidade verdes
- [ ] INC-5 — Integrar now/status/collect/watch/wait/trends e JSON multi-provider · dep: INC-3, INC-4 · verif: contratos humano/JSON/exit code e stale verdes
- [ ] INC-6 — Isolar rate/perfil/projeção/backtest/tune/advice/alertas por provider · dep: INC-5 · verif: analytics misto não cruza providers e stale não dispara efeitos
- [ ] INC-7 — Integrar burn, preços equivalentes, custos parciais e plot multi-provider · dep: INC-4, INC-5 · verif: tokens/custos sem dupla contagem e PNG misto válido
- [ ] INC-8 — Atualizar documentação/0.6.0 e executar aceite final completo · dep: INC-6, INC-7 · verif: suíte/ruff/smoke verdes, status limpo, sem artefatos

- 2026-09-18 19:32 · root · INC-0 → [x] · commit 8817301; Ruff e smoke verdes
- 2026-09-18 19:38 · inc1_tests · INC-1 → [x] · 35 testes, Ruff e smoke verdes; DuckDB 1.1/latest cobertos na CI
- 2026-09-18 19:43 · inc2_storage · INC-2 → [x] · 59 testes, migração/rollback/cache verdes; CodeGraph revisado
