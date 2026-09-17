# O0 v2 M1 — Validacao nao interativa de Antigravity CLI e Codex CLI

- **Marco:** O0 v2 M1 (Roadmap v3)
- **Status:** PASS independente no SHA `38db3f5b31d6f614b841f1133c0b474c8eb0bdc5`
- **Artefato verificavel:** [O0_V2_M1_EVIDENCE_REAUDIT.json](../O0_V2_M1_EVIDENCE_REAUDIT.json) e [pacote persistente](../O0_V2_M1_EVIDENCE_REAUDIT_PACKAGE/)
- **Registro anterior:** `O0_V2_M1_EVIDENCE.json` foi substituido para fins de aceite porque referenciava artefatos nao preservados e relatorio textual.
- **Data da execucao:** 2026-09-16
- **Ambiente:** Windows (drive fisico A:\ideias_standard)

---

## 1. Contexto e Descoberta Arquitetural

Durante a validacao inicial em repositorio descartavel sob `E:\Meu Drive\TEMPLATE\ideias_standard` (montagem virtual do Google Drive para Desktop), identificou-se que o subsistema de isolamento do Codex CLI (`--sandbox read-only`), implementado no Windows via `CreateProcessWithLogonW`, falhava com erro de acesso negado (`WinError 267 / UnauthorizedAccessException`). Isso ocorre porque drives virtuais em nuvem sao montados exclusivamente no contexto da sessao de logon interativa do usuario.

A migracao do repositorio para o disco local fisico `A:\ideias_standard` e a atualizacao dos caminhos dos servidores MCP (`git` em `mcp_config.json` e `serena` em `serena_config.yml`) resolveram o bloqueio, permitindo a execucao segura e nao interativa das duas ferramentas em sandbox nativo.

---

## 2. Antigravity CLI (Builder)

- **Binario:** `C:\Users\fael\AppData\Local\agy\bin\agy.exe`
- **Versao:** `1.2.4`
- **Autenticacao:** Sessao autenticada Antigravity / Gemini CLI
- **Modelo:** `gemini-3.7-flash-medium`
- **Comando nao interativo:**
  ```powershell
  agy.exe --add-dir="A:\ideias_standard\.tmp_o0_m1_reaudit_20260916\builder_repo" --model=gemini-3.7-flash-medium --dangerously-skip-permissions --output-format json --print="<prompt>"
  ```
- **Diretorio de trabalho:** `A:\ideias_standard\.tmp_o0_m1_reaudit_20260916\builder_repo`
- **Codigo de saida:** `0`
- **Comportamento verificado:**
  - Alterou `calc.py` adicionando `multiply(a, b) -> int`.
  - Alterou `test_calc.py` adicionando teste unitario `test_multiply`.
  - Executou `python -m unittest test_calc.py` com sucesso dentro do repositorio.
  - Criou commit `feat(calc): add multiply function with tests`.
  - Gerou novo commit SHA verificavel: `09e05b65ce8dcb35e9b83d08fb4b8e5b96662694` (distinto do `base_sha` `97accb89e13ff07455fb2c2928401ccd1c271942`). O pacote contem `builder.bundle`; `git clone` do bundle reproduz o SHA e os testes passam.
  - Retornou status estruturado `SUCCESS` em formato JSON, sem prompts interativos.

---

## 3. OpenAI Codex CLI (Auditor)

- **Binario:** `C:\Users\fael\AppData\Roaming\npm\codex.CMD`
- **Versao:** `codex-cli 0.154.0`
- **Autenticacao:** Credenciais armazenadas do ChatGPT (`~/.codex/auth.json`)
- **Comando nao interativo:**
  ```powershell
  codex.CMD exec --ephemeral --skip-git-repo-check -C "A:\ideias_standard\.tmp_o0_m1_reaudit_20260916\audit_checkout" --sandbox read-only --json --output-schema "A:\ideias_standard\.tmp_o0_m1_reaudit_20260916\audit_reports\auditor-output.schema.json" -o "A:\ideias_standard\.tmp_o0_m1_reaudit_20260916\audit_reports\codex_audit_report.json" "<prompt>"
  ```
  *(com stdin fechado via `input=""` para garantir modo headless estrito)*
- **Diretorio de trabalho (isolado):** `A:\ideias_standard\.tmp_o0_m1_reaudit_20260916\audit_checkout`
- **Codigo de saida:** `0`
- **Protecao do checkout:**
  - O checkout foi clonado do repositorio do Builder e fixado no SHA exato `09e05b65ce8dcb35e9b83d08fb4b8e5b96662694`.
  - Arquivos protegidos contra escrita (`stat.S_IREAD`).
  - Execucao sob `--sandbox read-only`.
  - Relatorio emitido estritamente fora do checkout (`audit_reports/codex_audit_report.json`).
  - Tentativa de sobrescrever `calc.py` com `Path.write_text` foi rejeitada com `PermissionError`; SHA-256 antes/depois: `91876997320d373f84e3905cdd1bb02795a4aa3f74689cd858ebc7455b854286`.
  - Verificacao pos-auditoria: `git status --porcelain` retornou limpo.
- **Resultado da auditoria:** `PASS`
  - Implementacao de `multiply` e respectivos testes unitarios verificados e validados pelo auditor independente.
  - Relatorio completo e JSON valido no pacote: `audited_sha`, `audit_result=PASS`, `summary`, `findings=[]`, `checks` nao vazio. SHA e campos sao validados antes do aceite.

---

## 4. Matriz de Criterios de Aceite (M1)

| Criterio M1 | Verificacao | Status |
| :--- | :--- | :---: |
| Versoes das CLIs registradas | Antigravity 1.2.4 / Codex 0.154.0 | PASS |
| Autenticacoes confirmadas | Sessoes locais ativas | PASS |
| Execucao nao interativa | Sem prompts interativos | PASS |
| Codigos de saida verificados | Exit code 0 em ambos | PASS |
| Diretorios de trabalho explicitos | `builder_repo` e `audit_checkout` separados | PASS |
| Builder altera arquivo | `calc.py` e `test_calc.py` modificados | PASS |
| Builder executa testes unitarios | Testes passaram localmente | PASS |
| Builder cria commit | Commit criado com mensagem semantica | PASS |
| SHA verificavel do Builder | Bundle commitado reproduz `09e05b65...` | PASS |
| Auditor em checkout separado | Checkout isolado no SHA exato | PASS |
| Codigo auditado protegido contra escrita | `PermissionError` em escrita real + `--sandbox read-only` | PASS |
| Relatorios fora do checkout | Salvo em `audit_reports/` | PASS |
| Saida estruturada capturada | JSON do Builder, JSONL Codex e relatorio JSON completo | PASS |
| Checkout inalterado pos-auditoria | `git status` limpo | PASS |

---

## 5. Limites e Nao Escopo

- Este marco comprova exclusivamente o funcionamento das CLIs (M1).
- O marco M2 (adapters para o runner `o0_runner.py`) nao foi iniciado.
- Nenhuma aprovacao humana ou avanco de gate de produto foi registrado.
- O Gate S1 permanece `NOT_RUN` e a fase S2 permanece `NOT_STARTED`.
- Reexecucao com `--work-root` ou `--output` ja existente falha sem apagar a evidencia anterior. Cada nova execucao requer caminhos novos.
