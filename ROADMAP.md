# Roadmap: funcionalidades candidatas

Levantamento feito em 2026-11-11 sobre a versão 0.8.0. Nada aqui está comprometido: é uma lista
de candidatas, cada uma com o que resolve, em que parte do código atual se apoia, o esforço e o
que pode dar errado. Quando uma for escolhida, ela ganha um plano próprio em
[`docs/plans/`](docs/plans/) e sai desta lista.

Esforço: **P** cabe em uma sessão, **M** em um ou dois dias, **G** pede plano próprio e vários
incrementos. As funções citadas estão em [`cmon.py`](cmon.py), salvo indicação contrária.

## Por onde começar

As dez de melhor relação entre valor e esforço, na ordem em que eu faria:

| ID | Funcionalidade | Esforço |
|---|---|---|
| [G2](#g2-tabela-de-preços-fora-do-código) | Tabela de preços fora do código, com aviso de modelo desconhecido | P |
| [A2](#a2-hook-em-todos-os-alertas-com-dados-estruturados) | Hook em todos os alertas, com dados estruturados | P |
| [A1](#a1-alertas-por-limiar-de-percentual) | Alertas por limiar de percentual | P |
| [A4](#a4-cmon-guard-código-de-saída-para-scripts-e-hooks) | `cmon guard`: código de saída para scripts e hooks | P |
| [E1](#e1-cmon-statusline-para-o-claude-code) | `cmon statusline` para o Claude Code, com o custo da sessão | M |
| [B1](#b1-projeção-e-hora-de-esgotamento-no-status-e-na-barra) | Projeção e hora de esgotamento no `status` e na barra | M |
| [B2](#b2-orçamento-diário-do-limite-semanal) | Orçamento diário do limite semanal | P |
| [C1](#c1-relatório-de-eficiência-de-cache) | Relatório de eficiência de cache | M |
| [C4](#c4-placar-de-ciclos-bloqueio-e-sobra) | Placar de ciclos: tempo bloqueado e sobra | P |
| [G1](#g1-cmon-doctor-no-próprio-cli) | `cmon doctor` no próprio CLI | M |

G2 vem primeiro porque corrige um erro silencioso de hoje, descrito na própria seção.

## A. Alertas e automação

### A1. Alertas por limiar de percentual

Hoje `collect --alert` dispara em dois casos: o ritmo vai estourar a janela antes do reset
(`_fire_rate_alerts`) ou o reset está próximo (`_fire_lead_alert`). Não há aviso do tipo "a janela
de 5h passou de 80%". `cmon wait --at 80` faz isso, mas prende um terminal e vale para uma única
espera.

Uma chave `alert_at` em `~/.cmon/config.json`, por janela (`session: 80,95`, `weekly_all: 50,75,90`),
avaliada a cada coleta e deduplicada por ciclo com o mesmo marcador de `_alert_fired`. Esforço P.
O percentual é inteiro e a coleta roda a cada 20 minutos, então o aviso de 95% pode chegar com a
janela já em 100%; com cmon Bar aberto a leitura é mais frequente e o atraso cai.

### A2. Hook em todos os alertas, com dados estruturados

`CMON_HOOK` só roda no alerta de proximidade do reset: `_fire_lead_alert` chama `_run_hook`,
`_fire_rate_alerts` não. Quem liga o hook a um webhook perde justamente o alerta mais útil, o de
que o ritmo vai estourar.

Chamar o hook em todo alerta e passar, além de `CMON_ALERT_MSG`, as variáveis `CMON_ALERT_KIND`
(`rate`, `lead`, `threshold`, `reset`), `CMON_ALERT_KEY`, `CMON_ALERT_PERCENT` e
`CMON_ALERT_RESETS_AT`. Com isso o README pode trazer receitas prontas para ntfy, Slack e Discord
sem que o cmon precise conhecer nenhum desses serviços. Esforço P. Muda o comportamento de quem já
usa o hook, que passaria a receber mais chamadas; vale anunciar no CHANGELOG.

### A3. Aviso de janela liberada, em segundo plano

Saber que a janela de 5h resetou exige hoje deixar `cmon wait` rodando. A coleta agendada já vê a
transição: basta notificar quando uma janela que estava acima de um piso (digamos 90%) aparece
zerada ou com `resets_at` novo. Esforço P. Só interessa a quem foi bloqueado, por isso o piso; sem
ele vira uma notificação a cada cinco horas.

### A4. `cmon guard`: código de saída para scripts e hooks

`cmon status` sai com 0 sempre, de propósito, para não quebrar statusline. Falta o inverso: um
comando que leia o cache e saia com código diferente de zero acima de um limite.

```sh
cmon guard --window session --max 90 || exit 0   # não começa o lote se a janela está no fim
```

Serve a laços de `claude -p`, a jobs de CI e a hooks do Claude Code (`UserPromptSubmit`, `Stop`)
que avisam o agente para encerrar e salvar estado antes do bloqueio. Lê só o cache, como `status`,
então custa milissegundos. Precisa de um terceiro código de saída para "sem dados", senão um cache
vazio passa por "liberado". Esforço P.

### A5. Detecção de consumo fora do padrão

Um subagente em laço durante a noite consome a semana sem que nenhum alerta atual perceba antes do
alerta de ritmo, que olha a janela inteira. O `token_log` tem custo por mensagem e por sessão, e
`_hour_profile` sabe quanto cada hora costuma consumir. Comparar o custo dos últimos N minutos com
o esperado para a hora e notificar a sessão e o projeto responsáveis. Esforço M. O risco é falso
positivo em dias de trabalho intenso; começar conservador, com múltiplo alto sobre o perfil, e
calibrar com `backtest`.

### A6. Abertura agendada da janela de 5h

`keepalive` só age quando a janela terminou e ficou parada (`_window_parked`). Não dá para
escolher *quando* a janela começa. Uma chave `keepalive_at` (`06:30`, dias úteis) abriria a janela
antes do expediente, para o primeiro reset cair no meio da manhã em vez do meio da tarde. Esforço
P a M. Tem as mesmas ressalvas do keepalive atual: gasta cota em nome do usuário, depende do
computador acordado e deve vir desligada.

## B. Projeção e planejamento

### B1. Projeção e hora de esgotamento no `status` e na barra

`now --advice` já calcula a projeção no reset e em quantas horas a janela chega a 100%
(`_window_tips`), mas isso não aparece no `status` nem em cmon Bar. A barra mostra onde o consumo
está, sem dizer para onde vai.

Seguir o desenho de `by_client`: `collect` calcula e grava em cache (como `_refresh_client_share`),
e `status --json` passa a carregar `projected_percent` e `exhausts_at` opcionais em cada janela,
sem quebrar leitores antigos. Na barra, placeholders `{5h_proj}`, `{wk_proj}` e `{5h_eta}`, mais
uma marca visual quando a projeção passa de 100%. Esforço M. A projeção envelhece com o cache: a
20 minutos de distância ela pode estar bem errada perto do fim da janela, e o payload deve dizer
quando foi calculada.

### B2. Orçamento diário do limite semanal

A pergunta prática do dia a dia é "quanto posso gastar hoje sem faltar na sexta". Dá para
responder com o que já existe: o que resta do limite semanal, dividido pelos dias até o reset e
ponderado pelo peso de cada dia da semana (`_day_weights`). Uma linha em `cmon now`:

```text
Hoje: 9 pp usados de ~14 pp de orçamento (semana em 41%, reset em 3d 22h)
```

Esforço P. Com pouco histórico os pesos por dia são ruído; abaixo do mesmo piso de cobertura do
perfil, dividir em partes iguais.

### B3. Faixa de erro nas projeções

`backtest` mede o erro médio por horizonte, e a projeção continua sendo exibida como um número
seco. Mostrar `69% ± 10` em `now` e `watch`, com o erro do horizonte correspondente tirado de
`_backtest_rows`. Esforço P. Sem previsões resolvidas suficientes não há faixa a mostrar, e a
saída fica como hoje.

### B4. Quanto vale um ponto percentual

`_plan_share` já cruza a subida do percentual com o custo registrado no mesmo intervalo. A razão
entre os dois, em US$ equivalentes de API por ponto, é uma medida do tamanho real do limite. Com
ela se responde quanto custa em pontos uma hora de Opus contra uma de Sonnet, e se detecta quando
a Anthropic muda o limite sem avisar: a razão dá um salto de um ciclo para o outro. Esforço M. A
estimativa só presta em intervalos em que quase todo o consumo foi local; intervalos com muito
*other* precisam ser descartados, e o resultado deve sair como faixa.

### B5. Dias de ausência

`quiet_hours` declara horas sem trabalho; não existe equivalente para dias. Em semana de feriado
ou férias a projeção semanal erra para cima. `cmon config set away 2026-12-24..2026-12-26` zeraria
a intensidade desses dias na integração de `_project`. Esforço P.

## C. Análise do consumo

O plano [usage-by-client](docs/plans/usage-by-client.md) já lista, em seu backlog, novas dimensões
para `burn --by` (subagentes, skill, servidor MCP, plugin, esforço, branch, versão, modo rápido),
alertas por cliente e um resumo semanal. Não repito essas aqui.

### C1. Relatório de eficiência de cache

O README diz que leitura e escrita de cache respondem por cerca de 80% do custo em uso agêntico, e
`burn` mostra o total por componente. Falta dizer onde esse custo é evitável. Com
`cache_read_tokens`, `cache_write_tokens` e `observed_at` por mensagem dá para medir, por sessão e
por projeto:

- a taxa de acerto de cache;
- quanto foi gasto reescrevendo cache depois de uma pausa maior que o TTL, que é o custo de voltar
  a uma sessão antiga em vez de abrir outra;
- as sessões que vivem com contexto muito grande, em que cada turno custa caro e um `/compact`
  pagaria.

Esforço M. É estimativa: o log não diz qual TTL foi aplicado a cada bloco, e o corte por pausa
depende de supor 1h, como a tabela de preços já supõe.

### C2. Linha do tempo de uma sessão

`burn --by session` dá o total de cada sessão. `cmon session <id>` mostraria a sessão por dentro:
custo por turno, modelo, crescimento do contexto e os pontos em que ele foi compactado. Ajuda a
entender por que uma sessão específica custou 30 pontos. Esforço M. Compactação e troca de modelo
são inferidas da sequência de tokens, sem marca explícita no que o cmon guarda hoje.

### C3. Comparação entre períodos

`burn --compare` colocaria lado a lado o período pedido e o anterior de mesmo tamanho, com a
variação por grupo: qual projeto ou cliente explica a semana ter sido mais cara. Esforço P. O
`token_log` guarda as linhas depois que o Claude Code apaga os transcripts, então a comparação vai
além de 30 dias em máquinas que coletam há mais tempo; em instalação recente o período anterior
vem vazio e a saída precisa dizer isso.

### C4. Placar de ciclos: bloqueio e sobra

`trends` mostra o pico de cada ciclo. Dois números derivados dizem mais sobre o uso do plano:
quantas horas a janela ficou em 100%, isto é, tempo bloqueado, e quanto do limite semanal sobrou
no reset. Bloqueio frequente com sobra semanal indica rajadas mal distribuídas; sobra alta sem
bloqueio indica plano grande demais. Esforço P, só com `snapshots`. A resolução é a do intervalo
de coleta: um bloqueio de dez minutos entre duas coletas não aparece.

### C5. Mapa de calor por hora e dia da semana

A projeção aprende um perfil de 24 horas e um multiplicador por dia da semana
(`_hour_buckets_tokens`, `_day_weights`), e nenhum dos dois é mostrado. Um painel a mais em
`cmon plot` e uma versão em texto em `trends --heatmap`, com a grade completa de hora por dia
tirada do `token_log`, deixam o usuário conferir se o que o cmon aprendeu bate com a rotina dele,
o que também ajuda a depurar projeções estranhas. Esforço P.

### C6. Exportação

`cmon export --format csv|parquet|json` para `snapshots`, `token_log` e ciclos, com `--since`.
O DuckDB já escreve parquet em `sync_devices`. Atende quem quer levar os dados para planilha ou BI
sem abrir o banco, o que o AGENTS.md desaconselha fazer enquanto `watch` ou `collect` seguram o
lock. Esforço P. O export carrega nomes de projeto e ids de sessão, como o sync; a ajuda do comando
deve dizer isso.

### C7. Valor extraído do plano

O README explica que o custo mostrado é o equivalente de API. Com uma chave `plan_price` na
configuração, `burn` fecharia a conta: equivalente de API no mês contra o preço da assinatura, e o
múltiplo entre os dois. Esforço P. Serve para decidir entre planos, junto com C4.

## D. cmon Bar (macOS)

### D1. Minigráfico no menu

O menu mostra barras de estado atual. Um gráfico pequeno por janela, com a trajetória do ciclo e a
linha de ritmo ideal, mostraria de relance se o consumo está acima ou abaixo do alvo, que hoje só
se vê em `cmon plot`. Esforço M. Pede uma série histórica que `status --json` não traz; o melhor é
um `trends --json --series` separado, lido só quando o menu abre, para não engordar o payload que
a barra consulta a cada atualização.

### D2. Seções por projeto e por modelo

A seção *By client* veio na 0.8.0. O mesmo mecanismo (`_refresh_client_share` com outra coluna)
serve para projeto e modelo, que respondem melhor "o que está consumindo agora". Esforço P a M.
Projetos são muitos: mostrar os três maiores e agrupar o resto.

### D3. Notificações nativas com ação

As notificações saem por `osascript` (`_notify`): aparecem em nome do Editor de Scripts e o clique
não leva a lugar nenhum. Emitidas pelo app, com `UNUserNotificationCenter`, teriam o ícone do
cmon, botão para abrir o `watch` e opção de silenciar até o próximo reset. Esforço M. Não
verifiquei se o app compilado com `swiftc` e assinado ad hoc consegue a permissão de notificação;
é a primeira coisa a testar.

### D4. Janela de ajustes

*Settings…* abre `bar.json` no editor de texto (`openSettings` em
[`menubar/CmonBar.swift`](menubar/CmonBar.swift)). Uma janela com os segmentos do título, as regras
de atualização e uma prévia ao vivo atenderia quem não quer editar JSON. Esforço G, e o arquivo
continuaria sendo a fonte da verdade. Prioridade baixa: o público atual edita JSON sem problema.

## E. Integrações

### E1. `cmon statusline` para o Claude Code

O Claude Code entrega ao comando de statusline um JSON no stdin com a sessão, o modelo e o
diretório. `cmon status` ignora esse JSON e imprime só o plano. Um subcomando próprio juntaria os
dois lados: percentuais do plano e, pelo `session_id`, o custo e os pontos que *esta* sessão já
consumiu.

```text
5h 62% 1h40 · wk 44% · sessão US$ 3,10 (2 pp)
```

Esforço M. A statusline roda a cada atualização da tela, então o comando não pode varrer
transcripts nem esperar o lock do DuckDB: lê o cache de status e um cache por sessão que `collect`
mantém, e o número da sessão pode estar alguns minutos atrasado. O formato exato do JSON de
entrada precisa ser conferido na documentação do Claude Code antes de implementar.

### E2. `status --format`

Os placeholders de `bar.json` (`{5h_pct}`, `{reset_short}`, `{wk_bar}`…) só existem no Swift.
`cmon status --format '{5h_pct} {reset_short}'` com os mesmos nomes daria a tmux, starship,
waybar e polybar a personalização que hoje é exclusiva do macOS. Esforço P a M. São duas
implementações do mesmo vocabulário, em Python e em Swift, que tendem a divergir; um arquivo de
casos de teste compartilhado entre `tests/` e `menubar/Tests.swift` segura isso.

### E3. Servidor MCP

`cmon mcp` exporia `status` e `burn --plan` como ferramentas, para um agente consultar a cota e
decidir sozinho que modelo usar ou quando parar. Esforço M, com dependência nova. O ganho é
pequeno onde o agente já tem shell, porque `cmon status --json` resolve; faz sentido para clientes
sem shell, como o app de desktop.

### E4. Métricas para Prometheus

`cmon export --prom` no formato de textfile do node_exporter: percentual por janela, segundos até
o reset, custo por modelo. Esforço P, sobre C6. Para quem já tem Grafana em casa ou no trabalho.

## F. Contas e equipe

### F1. Perfis para mais de uma conta

O cmon lê uma conta, a do Claude Code do terminal. Quem tem conta pessoal e de trabalho não
acompanha as duas. `cmon --profile work` separaria token e banco por perfil, e a barra mostraria um
grupo por perfil. Esforço G. Mexe em resolução de token, que é a parte mais sensível do código, e o
perfil secundário não tem login do Claude Code de onde ler: dependeria de `cmon token set` e de um
token que expira sem renovação automática.

## G. Dados, instalação e robustez

### G1. `cmon doctor` no próprio CLI

`make doctor` vive em [`scripts/setup.sh`](scripts/setup.sh) e só existe para quem clonou o
repositório no macOS. Quem instalou com `uv tool install`, em qualquer sistema, não tem diagnóstico
nenhum. Trazer as checagens que não são do macOS para um subcomando: origem e validade do token,
alcance da API, agendamento instalado, idade do último snapshot, versão do schema, lock do banco,
`sync_dir` acessível. `--json` para assistentes. `make doctor` passaria a chamar o subcomando e
acrescentar só o que é do app. Esforço M.

### G2. Tabela de preços fora do código

`PRICES` é um dicionário fixo, e `_price` devolve zero para modelo que não esteja nele. Um modelo
novo aparece em `burn` com custo US$ 0 e, como `_plan_share` reparte os pontos pelo custo, recebe
zero ponto em `burn --plan`: o que ele consumiu vai para os grupos que tiveram custo no mesmo
intervalo, ou para *other* quando ninguém mais teve, sem aviso. É o caso de Mythos hoje, que
`_short_model` reconhece e `PRICES` não tem.

Duas mudanças: `~/.cmon/prices.json` sobrepõe a tabela embutida, e `burn` avisa quando encontra
tokens de um modelo sem preço em vez de somar zero calado. Esforço P.

### G3. Cópia de segurança antes de migrar

O AGENTS.md pede que se copie `usage.duckdb` à mão antes de atualizar, porque a migração é sem
volta para versões antigas. `_ensure_schema` pode fazer a cópia sozinha, para
`usage.duckdb.v<N>.bak`, antes de abrir a transação, e `cmon db backup|restore` cobre o caso
manual. Esforço P. O banco é pequeno depois da compactação do schema v2, mas a cópia deve checar o
espaço livre e manter só a mais recente por versão.

### G4. Captura dos campos que o endpoint já devolve

`limits()` lê só `limits[]` e descarta o resto da resposta. `snapshots.metadata_json` existe e
está vazia para o Claude. Guardar ali os campos desconhecidos, e avisar uma vez quando surgir um
`kind` novo, protege contra mudança do endpoint privado e pode revelar dados úteis que hoje são
jogados fora. É hipótese minha que a resposta traga algo além de `limits[]`, como créditos de uso
extra; o primeiro passo é inspecionar uma resposta real. Esforço P para a captura.

### G5. Notificação no Windows

`_notify` cobre macOS e Linux; no Windows os alertas só vão para o stderr de uma tarefa agendada
que ninguém lê. Uma notificação via PowerShell fecha a lacuna. Esforço P, mas sem máquina Windows
para testar no CI além do smoke test.

### G6. Completar comandos no shell

`cmon completion zsh|bash|fish`, gerado a partir do argparse de `main()`. Esforço P.

### G7. Gráficos em HTML

`cmon plot` gera PNG e instala matplotlib no primeiro uso. `cmon plot --html` produziria um arquivo
único, interativo, com as mesmas três visões e as de C4 e C5. Esforço M. Se depender de biblioteca
JS por CDN, deixa de funcionar offline; embutir o script aumenta o arquivo.

### G8. Visões alternadas no `watch`

O `watch` mostra as janelas e uma linha de burn. Teclas para alternar entre janelas, clientes,
projetos e sessões ativas fariam dele o painel completo no terminal, sem sair para `burn`. Esforço
M. Ele segura o lock de escrita do DuckDB, então as visões novas precisam reaproveitar a conexão
aberta.

## Já planejado em outro lugar

| Tema | Onde | Estado |
|---|---|---|
| Codex como segundo provider | [docs/plans/codex-provider.md](docs/plans/codex-provider.md) | Em execução, parcialmente construído |
| Outros clientes: GLM, Aider, OpenCode, Gemini CLI | [docs/plans/MULTI_PROVIDER_PLAN.md](docs/plans/MULTI_PROVIDER_PLAN.md) | Plano |
| Troca automática para z.ai ao estourar o limite | [docs/plans/FALLBACK_PLAN.md](docs/plans/FALLBACK_PLAN.md) | Plano |
| Mais dimensões em `burn --by`, alertas por cliente, resumo semanal | [docs/plans/usage-by-client.md](docs/plans/usage-by-client.md), seção Backlog | Backlog |

## O que fica de fora

- **Proxy no caminho das requisições.** Daria medição exata por requisição e troca de provider no
  meio da sessão, mas transforma o cmon de observador em ponto de falha do Claude Code. O plano de
  fallback já descarta isso.
- **Renovar o token do Claude Code.** O cmon 0.5 fez isso e deslogava o usuário; a leitura é só de
  consulta desde a 0.6.
- **Servidor central para equipe.** O sync por pasta compartilhada existe justamente para não
  haver servidor, e arquivos de outra conta são ignorados de propósito.
- **Widget do macOS.** Exige projeto Xcode e extensão de app, o que acaba com a instalação por
  `swiftc` sem dependências.
- **Uso do chat em claude.ai por conversa.** Não há log local; continua aparecendo só como *other*.
