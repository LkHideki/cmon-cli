# Roadmap: funcionalidades candidatas

Levantamento feito em 2026-11-11 sobre a versão 0.8.0. Nada aqui está comprometido: é uma lista
de candidatas, cada uma com o que resolve, em que parte do código atual se apoia, o esforço e o
que pode dar errado. Quando uma for escolhida, ela ganha um plano próprio em
[`docs/plans/`](docs/plans/) e sai desta lista. As dez da tabela abaixo foram implementadas na
branch `feat/roadmap-top10` e continuam aqui, cada uma com uma linha **Estado** e o texto
ajustado onde a implementação se afastou do que eu tinha descrito.

Esforço: **P** cabe em uma sessão, **M** em um ou dois dias, **G** pede plano próprio e vários
incrementos. As funções citadas estão em [`cmon.py`](cmon.py), salvo indicação contrária.

## Por onde começar

As dez de melhor relação entre valor e esforço, na ordem em que eu faria:

| ID | Funcionalidade | Esforço | Estado |
|---|---|---|---|
| [G2](#g2-tabela-de-preços-fora-do-código) | Tabela de preços fora do código, com aviso de modelo desconhecido | P | Implementada (`5cde799`) |
| [A2](#a2-hook-em-todos-os-alertas-com-dados-estruturados) | Hook em todos os alertas, com dados estruturados | P | Implementada (`0dc20c0`) |
| [A1](#a1-alertas-por-limiar-de-percentual) | Alertas por limiar de percentual | P | Implementada (`7623fb0`) |
| [A4](#a4-cmon-guard-código-de-saída-para-scripts-e-hooks) | `cmon guard`: código de saída para scripts e hooks | P | Implementada (`32bf08c`) |
| [E1](#e1-cmon-statusline-para-o-claude-code) | `cmon statusline` para o Claude Code, com o custo da sessão | M | Implementada (`9a4655f`) |
| [B1](#b1-projeção-e-hora-de-esgotamento-no-status-e-na-barra) | Projeção e hora de esgotamento no `status` e na barra | M | Implementada (`efd9c07`) |
| [B2](#b2-orçamento-diário-do-limite-semanal) | Orçamento diário do limite semanal | P | Implementada (`fced00c`) |
| [C1](#c1-relatório-de-eficiência-de-cache) | Relatório de eficiência de cache | M | Implementada (`9e56bde`) |
| [C4](#c4-placar-de-ciclos-bloqueio-e-sobra) | Placar de ciclos: tempo bloqueado e sobra | P | Implementada (`74a5dd3`) |
| [G1](#g1-cmon-doctor-no-próprio-cli) | `cmon doctor` no próprio CLI | M | Implementada (`8248d7a`) |

G2 veio primeiro porque corrigia um erro silencioso, descrito na própria seção. Depois das dez, uma
revisão encontrou defeitos em algumas delas, corrigidos em `be82763`, `e353cab`, `7828ec6` e
`4ac02e4`; cada seção diz o que mudou.

## A. Alertas e automação

### A1. Alertas por limiar de percentual

**Estado:** implementada na branch feat/roadmap-top10 (commit 7623fb0).

Hoje `collect --alert` dispara em dois casos: o ritmo vai estourar a janela antes do reset
(`_fire_rate_alerts`) ou o reset está próximo (`_fire_lead_alert`). Não há aviso do tipo "a janela
de 5h passou de 80%". `cmon wait --at 80` faz isso, mas prende um terminal e vale para uma única
espera.

Uma chave `alert_at` em `~/.cmon/config.json`, gravada com
`cmon config set alert_at "session=80,95 weekly_all=50,75,90"` e validada no `set`. Aceita os
apelidos `5h` e `weekly` e modelos com escopo, como `fable`. É avaliada a cada `collect --alert` e
deduplicada por percentual e por ciclo com o mesmo marcador de `_alert_fired`; quando uma coleta
encontra vários percentuais ultrapassados de uma vez, só o maior notifica e os menores ficam
marcados. Esforço P. O percentual é inteiro e a coleta roda a cada 20 minutos, então o aviso de 95%
pode chegar com a janela já em 100%. Eu tinha escrito que cmon Bar aberto encurtaria esse atraso,
e não encurta: a barra roda `cmon status --json --live` (`menubar/CmonBar.swift`), e `status` não
dispara alerta; só `collect --alert` dispara.

### A2. Hook em todos os alertas, com dados estruturados

**Estado:** implementada na branch feat/roadmap-top10 (commit 0dc20c0).

`CMON_HOOK` só roda no alerta de proximidade do reset: `_fire_lead_alert` chama `_run_hook`,
`_fire_rate_alerts` não. Quem liga o hook a um webhook perde justamente o alerta mais útil, o de
que o ritmo vai estourar.

O hook passou a rodar em todo alerta de `collect --alert` e recebe, além de `CMON_ALERT_MSG`, as
variáveis `CMON_ALERT_KIND`, `CMON_ALERT_KEY`, `CMON_ALERT_PERCENT` e `CMON_ALERT_RESETS_AT` (ISO
8601 em UTC), sempre definidas e vazias quando o valor é desconhecido, para não derrubar um hook
com `set -u`. Os tipos que existem são `rate` e `lead`, e `threshold` veio com A1; `reset` depende
de A3, que não foi feita, e as notificações de `cmon wait` (limiar atingido, janela liberada)
continuam sem hook. `CMON_ALERT_KEY` não se limita a `session` e `weekly_all`: os alertas de ritmo
e de limiar passam qualquer janela da API, como `weekly_scoped:Fable`, com o nome do modelo como a
API o devolve. A primeira versão do README dizia o contrário e tratava a chave como segura para
colar em JSON; a correção de revisão em `7828ec6` acertou o texto e passou a testar que as
variáveis ficam definidas mesmo vazias. O README traz receitas para ntfy e para um webhook JSON
genérico, sem que o cmon conheça nenhum desses serviços. Esforço P. Muda o comportamento de quem já
usa o hook, que passa a receber mais chamadas; está no CHANGELOG.

### A3. Aviso de janela liberada, em segundo plano

Saber que a janela de 5h resetou exige hoje deixar `cmon wait` rodando. A coleta agendada já vê a
transição: basta notificar quando uma janela que estava acima de um piso (digamos 90%) aparece
zerada ou com `resets_at` novo. Esforço P. Só interessa a quem foi bloqueado, por isso o piso; sem
ele vira uma notificação a cada cinco horas.

### A4. `cmon guard`: código de saída para scripts e hooks

**Estado:** implementada na branch feat/roadmap-top10 (commit 32bf08c).

`cmon status` sai com 0 sempre, de propósito, para não quebrar statusline. Falta o inverso: um
comando que leia o cache e saia com código diferente de zero acima de um limite.

```sh
cmon guard --window session --max 90 || exit 0   # não começa o lote se a janela está no fim
```

Serve a laços de `claude -p`, a jobs de CI e a hooks do Claude Code (`UserPromptSubmit`, `Stop`)
que avisam o agente para encerrar e salvar estado antes do bloqueio. Lê só o cache de status
(`_read_cache`), então custa milissegundos. Eu tinha escrito "como `status`", e não é: sem
`--live`, `status` cai no DuckDB quando falta o cache e chama a API quando um ciclo já resetou;
`guard` não faz nem uma coisa nem outra. O preço é que, sem `status.json` e com o banco presente,
`guard` sai com 3 em vez de ler o banco, caso raro porque `collect`, `status --live` e a barra
regravam o cache. Precisa de um terceiro código de saída para "sem dados", senão um cache vazio
passa por "liberado": ficou 0 abaixo de `--max`, 1 no limite ou acima, 3 sem dados (sem cache,
janela ausente ou cache mais velho que `--stale-min`, 60 minutos) e 2 para argumento inválido, que
é o código do argparse. Um ciclo que já resetou no cache conta como 0%. Uma data de reset ilegível
no cache derrubava o comando com saída 1, o código de "acima do limite"; desde a correção de
revisão em `be82763` o percentual decide sozinho. Esforço P.

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

**Estado:** implementada na branch feat/roadmap-top10 (commit efd9c07).

`now --advice` já calcula a projeção no reset e em quantas horas a janela chega a 100%
(`_window_tips`), mas isso não aparece no `status` nem em cmon Bar. A barra mostra onde o consumo
está, sem dizer para onde vai.

Segue o desenho de `by_client`: `collect`, `watch` e `now` calculam e gravam em
`~/.cmon/projection.json` (como `_refresh_client_share`), e `status --json` carrega, opcionais em
cada janela e sem quebrar leitores antigos, `projected_percent` (limitado a 999, como nas outras
saídas), `exhausts_at` (`null` quando o ritmo não chega a 100% antes do reset) e `projection_at`.
Os campos ficam ausentes, e não `null`, quando não há ritmo medível (menos de duas leituras no
ciclo, ou nenhuma subida), quando o reset já passou, quando a janela está em 100% e quando a
leitura já passou do percentual projetado. O caso dos 100% só passou a valer com a correção de
revisão em `e353cab`: `status` juntava a projeção do cache mesmo depois de uma leitura `--live` em
100% entre duas coletas. O da leitura acima da projeção veio na revisão seguinte, pelo mesmo
motivo: o consumo não cai dentro do ciclo, e "Projected 22% at reset" sob 70% usados se contradiz.
Na barra, os placeholders
`{5h_proj}`, `{wk_proj}` e `{5h_eta}` (no formato de `{reset_short}`, `80m`, em branco enquanto a
projeção fica abaixo de 100%) e uma linha de projeção em cada janela do menu, em laranja acima de
100% e com a idade a partir de dois minutos. Esforço M. A projeção envelhece com o cache: a 20
minutos de distância ela pode estar bem errada perto do fim da janela, e por isso o payload diz
quando foi calculada.

### B2. Orçamento diário do limite semanal

**Estado:** implementada na branch feat/roadmap-top10 (commit fced00c).

A pergunta prática do dia a dia é "quanto posso gastar hoje sem faltar na sexta". Dá para
responder com o que já existe: o que resta do limite semanal, dividido pelos dias até o reset e
ponderado pelo peso de cada dia da semana. Eu tinha apontado `_day_weights` para esse peso, mas ele
guarda um peso de recência por data; o peso por dia da semana são os multiplicadores `dow` de
`_hour_profile`, e o piso de cobertura é a fonte `learned` dele. A implementação usa também a forma
das horas (intensidade = taxa da hora × `dow`, como em `_project`), de modo que os dias inteiros
diferem só pelo dia da semana, mas um dia parcial (hoje a partir da leitura inicial, o dia do reset
até o reset) pesa pelas horas que contém. Uma linha em `cmon now`:

```text
Today: 9 pp used of ~14 pp budget (profile-weighted), 5 pp left — week at 41%, resets in 3d 22h.
```

Esforço P. Com pouco histórico os pesos por dia são ruído; abaixo do mesmo piso de cobertura do
perfil a divisão é em partes iguais (*equal split*), e um perfil sintético de `quiet_hours` conta
como divisão igual. Casos que a descrição não previa: um ciclo que começou hoje conta a partir do
reset em 0%; leituras separadas por mais de `PROFILE_PAIR_MAX_H` em volta da meia-noite, com o
percentual mudando no meio, movem a contagem para a primeira leitura do dia; um reset ainda hoje
faz de todo o restante o orçamento do dia; sem leitura anterior a linha mostra só o orçamento; o
limite esgotado tem linha própria. Sem janela semanal, sem `resets_at` ou com o reset já passado,
não há linha. `now --advice` ficou como estava.

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

**Estado:** implementada na branch feat/roadmap-top10 (commit 9e56bde).

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

Ficou como `cmon burn --cache`, que recusa `--by` e `--plan`. Candidata a `/compact` é a sessão com
pelo menos 10 turnos, contexto médio por turno entre os 10% maiores do período e pelo menos 50k
tokens, piso que impede um período só de sessões pequenas de apontar alguma. A primeira versão
chamava de 10% o 90º percentil por posição com tudo o que estava acima dele, o que dava 2 de 10
sessões e 3 de 20; desde a correção de revisão em `e353cab` são os 10% arredondados para cima, no
mínimo uma, mais as empatadas no corte. A pausa é medida sobre o histórico inteiro da sessão, antes
de `--since` e dos filtros, e linhas `<synthetic>` sem tokens não contam como turno.

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

**Estado:** implementada na branch feat/roadmap-top10 (commit 74a5dd3).

`trends` mostra o pico de cada ciclo. Dois números derivados dizem mais sobre o uso do plano:
quantas horas a janela ficou em 100%, isto é, tempo bloqueado, e quanto do limite semanal sobrou
no reset. Bloqueio frequente com sobra semanal indica rajadas mal distribuídas; sobra alta sem
bloqueio indica plano grande demais. Esforço P, só com `snapshots`. A resolução é a do intervalo
de coleta: um bloqueio de dez minutos entre duas coletas não aparece.

Ficou como `trends --scoreboard`, que substitui a saída normal e ignora `--since`, com os últimos 8
ciclos semanais. O tempo bloqueado soma os intervalos entre leituras consecutivas de um ciclo de
5h a partir da primeira em 100%, recortados ao ciclo semanal, e um intervalo maior que 5h fica de
fora porque pode esconder um reset. O último ciclo conta como em andamento, a não ser que o
`resets_at` semanal mais recente já tenha passado. Sem histórico semanal, o modo texto sai com 1 e
`--json` imprime `[]`. Os ciclos semanais vinham só de `_cycles`, que corta onde o percentual cai:
com a máquina desligada no reset e a primeira leitura do ciclo novo igual ou acima da última do
anterior, duas semanas viravam uma linha e a sobra da que terminou se perdia. Desde a correção de
revisão em `e353cab` o placar corta também onde `resets_at` muda mais que `CYCLE_JITTER_S`, como
`_plan_share` já fazia. Os ciclos de 5h do placar passaram a cortar do mesmo jeito: duas leituras
em 100% dos dois lados de um reset (máquina dormindo enquanto outro aparelho enchia a janela
seguinte) viravam um ciclo só, com o intervalo contado como bloqueado. Os outros usos de `_cycles`
não mudaram.

### C5. Mapa de calor por hora e dia da semana

A projeção aprende um perfil de 24 horas e um multiplicador por dia da semana
(`_hour_buckets_tokens` e os multiplicadores `dow` de `_hour_profile`; `_day_weights`, que eu
tinha citado aqui, é um peso de recência por data, como B2 mostrou), e nenhum dos dois é mostrado. Um painel a mais em
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

**Estado:** implementada na branch feat/roadmap-top10 (commit 9a4655f).

O Claude Code entrega ao comando de statusline um JSON no stdin com a sessão, o modelo e o
diretório e, conferido na documentação em 2026-11-11, também `cost.total_cost_usd` e, nos planos
Pro e Max depois da primeira resposta da sessão, `rate_limits.five_hour` e `rate_limits.seven_day`
(`used_percentage` de 0 a 100 e `resets_at` em segundos Unix, cada um opcional). `cmon status`
ignora esse JSON e imprime só o plano. Um subcomando próprio junta os dois lados: percentuais do
plano e, pelo `session_id`, o custo e os pontos que *esta* sessão já consumiu.

```text
5h 62% 1h40m · wk 44% · session $3.10 (5h 2pp, wk 0.4pp)
```

Esforço M. A statusline roda a cada atualização da tela, então o comando não pode varrer
transcripts nem esperar o lock do DuckDB: lê o cache de status e um cache por sessão
(`~/.cmon/sessions.json`) que `collect` mantém, e os pontos da sessão podem estar até uma coleta
atrasados. O stdin mais rico mudou duas coisas. O custo mostrado é o `cost.total_cost_usd` ao vivo
do Claude Code quando ele vem, e o do cache só na falta dele, de modo que aparece custo mesmo para
sessão que o cache ainda não tem. E o plano continua vindo primeiro do cache, mas os `rate_limits`
preenchem uma janela quando o cache falta, tem mais de 30 minutos ou guarda uma janela cujo reset
já passou; uma janela que resetou sem substituto aparece em 0%, como em `guard`, e os pontos de um
ciclo já encerrado são descartados. O comando sai sempre com 0: a correção de revisão em `be82763`
cobriu um stdin aninhado fundo demais, que estourava a recursão do parser e saía com traceback, e
`4ac02e4` isolou o teste que roda o comando em processo filho das credenciais reais.

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

**Estado:** implementada na branch feat/roadmap-top10 (commit 8248d7a).

`make doctor` vive em [`scripts/setup.sh`](scripts/setup.sh) e só existe para quem clonou o
repositório no macOS. Quem instalou com `uv tool install`, em qualquer sistema, não tem diagnóstico
nenhum. Trazer as checagens que não são do macOS para um subcomando: origem e validade do token,
alcance da API, agendamento instalado, idade do último snapshot, versão do schema, lock do banco,
`sync_dir` acessível. `--json` para assistentes. `make doctor` passa a chamar o subcomando e
acrescenta só o que é do app. Esforço M.

Além do que eu tinha listado, entraram `config.json` e `bar.json`, este conferido contra os tipos
que o decodificador de cmon Bar aceita e não só contra a sintaxe JSON, porque um tipo errado faz o
app trocar o arquivo inteiro pelos padrões. Cada checagem sai como `ok`, `warn`, `fail` ou `skip`
(este para `--offline`, `sync_dir` não definido, sistema sem suporte e checagens de banco que não
podem rodar); só `fail` muda o código de saída, e por isso agendamento ausente, snapshot com mais
de 60 minutos, lock ocupado e schema antigo são avisos. O token nunca aparece, nem mascarado: só a
origem e a validade. A chamada à API não grava snapshot nem cache, e o banco é aberto só para
leitura. Se o `cmon` instalado é anterior ao `doctor`, `make doctor` falha numa checagem própria,
`cmon command is up to date`, com a correção `make install`. Na revisão, `be82763` fez um
`bar.json` aninhado fundo demais virar `fail` em vez de traceback, e `4ac02e4` passou a testar o
`ok` do `--json` num diagnóstico com falha e a abertura do banco só para leitura.

### G2. Tabela de preços fora do código

**Estado:** implementada na branch feat/roadmap-top10 (commit 5cde799).

`PRICES` é um dicionário fixo, e `_price` devolve zero para modelo que não esteja nele. Um modelo
novo aparece em `burn` com custo US$ 0 e, como `_plan_share` reparte os pontos pelo custo, recebe
zero ponto em `burn --plan`: o que ele consumiu vai para os grupos que tiveram custo no mesmo
intervalo, ou para *other* quando ninguém mais teve, sem aviso. É o caso de Mythos, que
`_short_model` reconhece e `PRICES` não tem; continua sem preço embutido, mas agora aparece no
aviso e pode ganhar preço em `prices.json`.

Duas mudanças: `~/.cmon/prices.json` sobrepõe a tabela embutida (vence a substring mais longa, e
um arquivo inválido é ignorado com um aviso), e `burn` avisa quando encontra tokens de um modelo
sem preço em vez de somar zero calado. Esforço P. O aviso vai para o stderr só no modo texto. Em
`burn --json`, a lista `unpriced_models` não pôde ir para o topo como eu tinha pensado: sem
`--plan` a saída é uma lista solta, e uma chave no topo a transformaria em objeto e quebraria o
contrato. Nesse formato a lista vai em cada grupo que tem tokens sem preço, e a saída de referência
dos testes não muda; com `--plan` o objeto tem `unpriced_models` no topo, mesmo vazio, e os grupos
levam a mesma chave.

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
