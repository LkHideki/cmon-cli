# Contribuindo com o cmon

Obrigado pelo interesse em contribuir! Este guia cobre o essencial para começar.

## Ambiente de desenvolvimento

O projeto usa [uv](https://docs.astral.sh/uv/) para gerenciar dependências e ambientes.

```bash
# Clone e entre no diretório
git clone https://github.com/LkHideki/cmon.git
cd cmon

# Instala as dependências (ambiente enxuto)
uv sync

# Para trabalhar com os gráficos (comando `plot`), inclua o extra opcional
uv sync --extra plot

# Rode a CLI
uv run cmon --help
```

Requer **Python 3.11+**. A CI valida em 3.11, 3.12 e 3.13.

## Antes de abrir um PR

Rode o mesmo que a CI roda:

```bash
# Lint (obrigatório — a CI falha se houver violação)
uv run ruff check .

# Formatação automática das correções triviais
uv run ruff check --fix .

# Smoke test (importa o módulo e valida o CLI, sem rede)
uv run cmon --help
```

A configuração do ruff vive no `pyproject.toml` (`line-length = 120`, regras `E/F/I/UP/B`).

## Mensagens de commit

Seguimos [Conventional Commits](https://www.conventionalcommits.org/pt-br/):

```
<tipo>: <resumo no imperativo, minúsculo>

<corpo opcional explicando o PORQUÊ, não o o quê>
```

Tipos usados: `feat`, `fix`, `docs`, `perf`, `refactor`, `style`, `chore`, `ci`, `test`.

Diretrizes:

- **Escreva em português com acentuação correta.** Nada de `nao`, `voce`, `e` no lugar de `não`, `você`, `é`.
- Prefira **um tipo por commit**. Evite tipos compostos como `perf+feat:` — separe em dois commits ou escolha o predominante.
- O corpo é opcional, mas quando o commit muda comportamento, **explique o porquê** e traga números quando fizer sentido (ex.: `72s -> 1.3s`).

## Escopo e estilo

- O código-fonte é um único módulo: [`cmon.py`](cmon.py). Mantenha as mudanças coesas com o estilo existente.
- Novas dependências pesadas devem entrar como **extra opcional** (como `plot`), não no core.
- Nunca versione segredos. `.env`, `*.duckdb` e `*.png` já estão no `.gitignore`.

## Reportando bugs e ideias

Abra uma [issue](https://github.com/LkHideki/cmon/issues) descrevendo o que esperava, o que aconteceu e como reproduzir. Para a CLI, inclua o comando exato e a saída relevante (mascare tokens).
