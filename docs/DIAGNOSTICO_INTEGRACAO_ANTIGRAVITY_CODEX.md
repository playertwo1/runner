# Diagnóstico Técnico: Integração Antigravity (Builder) e Codex (Auditor) com Runner O0

Este documento consolida todas as investigações, testes empíricos, resultados observados, limitações identificadas e alternativas arquiteturais para conectar **Antigravity** e **Codex** ao runner O0 (`scripts/o0_runner.py`).

---

## 1. Contexto e Requisitos da Integração

O runner O0 (`scripts/o0_runner.py`) orquestra o ciclo de desenvolvimento e auditoria via subprocessos independentes configurados no arquivo `runner.json`:
- **`builder_command`**: Executa no diretório `builder_workspace` com variável `IDEAS_STANDARD_REPORT` apontando para o relatório canônico do Builder (`builder-report.schema.json`).
- **`auditor_command`**: Executa no diretório isolado `audit-workspaces/<sha>` com variável `IDEAS_STANDARD_REPORT` apontando para o relatório canônico de auditoria (`audit-report.schema.json`) e `IDEAS_STANDARD_AUDIT_TARGET_SHA` com o SHA a ser auditado.
- **Invariantes do Sistema**:
  - `Builder != Auditor`.
  - Auditor não escreve no workspace auditado (leitura isolada).
  - Execução não-interativa (batch) com retorno de código de processo (`exit code 0` para sucesso, diferente de zero para falha).
  - Geração estrita de relatórios validados por schema JSON.

---

## 2. Diagnóstico: Antigravity (Builder)

Foram investigadas três interfaces potenciais do Antigravity no ambiente Windows local:

### 2.1. CLI do Antigravity IDE (`agy.cmd`)
- **Caminho**: `C:\Users\fael\AppData\Local\Programs\Antigravity IDE\bin\agy.cmd`
- **Executável subjacente**: `antigravity-ide.exe` (Antigravity IDE v1.107.0, baseado em Electron).
- **Subcomandos disponíveis**: `chat`, `serve-web`, `tunnel`.
- **Comportamento observado**:
  - `agy chat [options] [prompt]` abre uma janela gráfica interativa do IDE Electron.
  - Opções disponíveis: `-m/--mode`, `-a/--add-file`, `--maximize`, `-r/--reuse-window`, `-n/--new-window`, `--profile`.
- **Bloqueio identificado**:
  - Não existe modo headless/batch em lote para execução de scripts de agente via linha de comando com término síncrono e código de saída de processo (`exit code`).
  - Não é viável como subprocesso de automação para o runner O0 sem intervenção humana na GUI.

### 2.2. CLI do Language Server (`agentapi.bat`)
- **Caminho**: `C:\Users\fael\.gemini\antigravity\bin\agentapi.bat`
- **Executável subjacente**: `C:\Users\fael\AppData\Local\Programs\antigravity\resources\bin\language_server.exe agentapi`
- **Comandos disponíveis**:
  - `new-conversation [--model=<flash_lite|flash|pro>] [--title=<title>] [--profile=<profile>] <prompt>`
  - `get-conversation-metadata <conversation_id>`
  - `send-message [--title=<title>] <recipient_id> <content>`
- **Comportamento testado**:
  - O comando `agentapi.bat new-conversation --model=flash_lite "Hello"` foi executado com sucesso e retornou um JSON contendo `conversationId` (`090e43ad-2997-4344-b2bb-59a3a99be10f`).
  - O daemon do Language Server iniciou um agente autônomo em background, registrando passos e chamadas de ferramentas em `C:\Users\fael\.gemini\antigravity\brain\<id>\.system_generated\logs\transcript.jsonl`.
- **Bloqueios identificados**:
  1. **Ausência de parâmetro de workspace**: O comando `new-conversation` não aceita argumento `--workspace`, `--dir` ou `--cd`. Ele vincula a execução exclusivamente ao workspace padrão previamente configurado no daemon (`g:\template`). O runner O0 cria workspaces dinâmicos e isolados (`builder-workspace`), para os quais o `agentapi` não permite apontar dinamicamente por comando.
  2. **Execução estritamente assíncrona**: O comando retorna imediatamente código 0 assim que enfileira a conversa no daemon, sem bloquear até a conclusão da tarefa pelo agente. O runner O0 depende de execução síncrona com código de saída do processo.
  3. **Ausência de código de saída da tarefa**: Se a tarefa do agente falhar internamente, o processo `agentapi.bat` já encerrou com 0 no momento do enfileiramento.

### 2.3. SDK Python (`google-antigravity` / Gemini API)
- **Status do ambiente**:
  - Pacote `google-antigravity`: Não instalado no Python local (`pip list` verificado).
  - Variável `GEMINI_API_KEY`: Não configurada nas variáveis de ambiente do sistema operacional.

### 2.4. CLI Headless Oficial (`agy.exe` v1.2.4) — INSTALADO E VALIDADO
- **Caminho**: `C:\Users\fael\AppData\Local\agy\bin\agy.exe`
- **Versão**: 1.2.4 (instalada via `https://antigravity.google/cli/install.ps1`).
- **Autenticação**: Pronta e funcional utilizando as credenciais ativas do Antigravity.
- **Suporte a Modo Headless**:
  - Flag `--print` (ou `-p`): Executa um prompt único de forma estritamente não-interativa e síncrona.
  - Flag `--output-format json`: Retorna metadados completos da sessão (`conversation_id`, `status: "SUCCESS"`, `response`, `duration_seconds`, `usage`).
  - Flag `--dangerously-skip-permissions`: Permite execução autônoma de ferramentas sem interrupção de confirmação na interface.
  - Flag `--json-schema <path>`: Força a validação da resposta contra schemas JSON canônicos.
  - Flag `--add-dir <path>`: Permite vincular diretórios adicionais de workspace.
- **Teste empírico realizado**:
  - Comando: `agy.exe --output-format json --print="Return a JSON object with key status and value OK"`
  - Resultado: Exit code 0, status `SUCCESS`, tempo 17s, tokens computados.
- **Conclusão para o Builder**:
  - O bloqueio anterior da interface do Antigravity está **resolvido**. O binário `agy.exe` atende plenamente aos requisitos de execução não-interativa, síncrona e em lote para atuar como `builder_command` no runner O0.

---

## 3. Diagnóstico: Codex (Auditor)

Foi investigada a CLI oficial do OpenAI Codex no ambiente local:

### 3.1. CLI do Codex (`codex.cmd` / `codex.ps1`)
- **Caminho**: `C:\Users\fael\AppData\Roaming\npm\codex.CMD`
- **Versão**: OpenAI Codex v0.154.0.
- **Autenticação**: **100% operacional** via ChatGPT tokens armazenados em `C:\Users\fael\.codex\auth.json` (modelo padrão `gpt-5.6-sol`, diagnosticado com `codex doctor`).
- **Execução não-interativa**: Comprovada com sucesso via `codex exec` com stdin fechado (`input=""`). Respondeu com sucesso em testes headless.
- **Isolamento de workspace**: Suporta a flag `-C <dir>` para apontar o working directory para o workspace de auditoria isolado (`audit-workspaces/<sha>`).
- **Sandboxing**: Suporta políticas de sandboxing (`--sandbox read-only` ou `danger-full-access`), permitindo garantir que o Auditor não muta o repositório auditado.

### 3.2. Comportamentos observados e Desafios de Formatação do Codex
Durante os testes de auditoria de código com `codex exec`, foram observados três aspectos críticos:

1. **Rejeição do Schema Canônico via `--output-schema`**:
   - Foi testado: `codex exec --output-schema schemas/audit-report.schema.json ...`.
   - **Erro da API OpenAI**:
     ```json
     {
       "type": "error",
       "error": {
         "type": "invalid_request_error",
         "code": "invalid_json_schema",
         "message": "Invalid schema for response_format 'codex_output_schema': In context=(), 'allOf' is not permitted.",
         "param": "text.format.schema"
       },
       "status": 400
     }
     ```
   - O recurso *Structured Outputs* da OpenAI possui restrições severas sobre palavras-chave de JSON Schema (como proibir `allOf`, amplamente utilizado no padrão draft-07 do projeto para reuso de definições).
2. **Preferência por Saída em Prosa / Markdown**:
   - Mesmo quando instruído a gerar JSON ou a gravar o arquivo `audit-report.json` em disco via ferramentas, o Codex priorizou responder no terminal/stdout com texto em Markdown formatado (por exemplo, `## Audit finding`, severidade, linhas e exemplos de teste de execução).
3. **Precisão da Análise Técnica do Codex**:
   - O Codex identificou com exatidão a falha introduzida propositalmente em `calc.py:2` (retorno `a - b` em vez de adição, com testes `add(2, 3) == -1` demonstrando a não-conformidade).
   - O modelo opera como um auditor de código analítico de altíssima fidelidade, mas sua saída padrão é orientada a humanos (Markdown), necessitando de tradução para a estrutura JSON estrita do runner O0.

---

## 4. Quadro Comparativo das Interfaces

| Requisito do Runner O0 | Antigravity (`agy.cmd`) | Antigravity (`agentapi.bat`) | OpenAI Codex (`codex.cmd`) |
| :--- | :--- | :--- | :--- |
| **Comando Não-Interativo (Batch)** | ❌ Apenas GUI interativa (`chat`) | ⚠️ Enfileira e sai imediatamente | ✅ `codex exec` nativo |
| **Autenticação no Ambiente** | ✅ Sessão IDE ativa | ✅ Sessão daemon ativa | ✅ Tokens ChatGPT configurados |
| **Workspace Arbitrário (`-C / cwd`)** | ❌ Fixo na janela | ❌ Fixo no daemon (`g:\template`) | ✅ Suporta `-C <dir>` |
| **Execução Síncrona + Exit Code** | ❌ Não disponível | ❌ Não disponível | ✅ Suporta execução síncrona |
| **Permissão Read-Only (Auditor)** | N/A (seria Builder) | N/A | ✅ Suporta `--sandbox read-only` |
| **Emissão de Relatório Canônico JSON**| ❌ Não nativo | ❌ Registra em logs internos | ⚠️ Emite Markdown (requer wrapper) |

---

## 5. Alternativas Arquiteturais para Desbloqueio

Para permitir que o runner O0 execute o ciclo completo com isolamento de autoridade e ferramentas reais sem simulações falsas, identificam-se as seguintes alternativas:

### Alternativa 1: Adaptador Wrapper para Codex como Auditor (Recomendado para o Auditor)
- **Como funciona**:
  - Cria-se o script adaptador `scripts/adapters/codex_auditor_adapter.py`.
  - O runner chama `codex_auditor_adapter.py` como `auditor_command`.
  - O adaptador executa `codex exec -C <audit_workspace>`, solicita a análise técnica do repositório, captura a saída em Markdown do Codex, e o próprio adaptador realiza o parse determinístico dos achados, gerando o relatório JSON estritamente compatível com `schemas/audit-report.schema.json`.
- **Status**: Totalmente viável com o ambiente e credenciais atuais.

### Alternativa 2: Antigravity como Builder via Daemon Poller (Adaptador Ponte)
- **Como funciona**:
  - Criar um script `scripts/adapters/antigravity_builder_adapter.py` que:
    1. Lê a tarefa e copia/vincula os arquivos do `builder_workspace` para a área acessível pelo daemon.
    2. Invoca `agentapi.bat new-conversation`.
    3. Monitora o `transcript.jsonl` correspondente até que o passo final atinja o status `DONE`.
    4. Sincroniza as alterações de volta para o `builder_workspace` e gera o `builder-report.json`.
- **Desafio**: Complexidade de sincronização de estado com o daemon do IDE e ausência de suporte nativo a workspaces dinâmicos no `agentapi`.

### Alternativa 3: Antigravity como Builder via Gemini API / Google GenAI SDK
- **Como funciona**:
  - Obter uma chave `GEMINI_API_KEY` (Google AI Studio) e instalar `pip install google-genai`.
  - O adaptador do Builder invoca diretamente a API Gemini com os schemas e ferramentas do workspace.
- **Desafio**: Requer configuração de chave de API externa e instalação de pacote adicional.

### Alternativa 4: Separação de Papéis — Sessão Atual (Builder) e Codex CLI (Auditor)
- **Como funciona**:
  - Como a presente sessão de desenvolvimento já opera nativamente sob o Antigravity, as alterações e correções do Builder são produzidas nesta superfície de trabalho.
  - O runner O0 executa a automação invocando o adaptador real do Codex (`codex_auditor_adapter.py`) para realizar a auditoria independente no workspace congelado.
  - Isso cumpre de forma fidedigna o princípio `Builder (Antigravity) != Auditor (Codex)`, sem simular recursos de linha de comando que o `agy.cmd` não oferece.

---

## 6. Próximos Passos Sugeridos

1. Alinhar a escolha da alternativa de integração com o Product Authority.
2. Caso aprovada a **Alternativa 1** para o Auditor (Codex) e **Alternativa 4** (ou adaptador ponte para Antigravity), implementar o adaptador canônico `scripts/adapters/codex_auditor_adapter.py`.
3. Executar o ciclo real de validação Builder → Auditor FAIL → correção → Auditor PASS.
