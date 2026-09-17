# O0 First Block — Validation Evidence

- Scope: `O0-C01`–`O0-C13`
- Implementation SHA: `9e013b2f32aad6aaa2febea07f33c6e792efb230`
- Conformance run: `34857599652`
- Matrix: Python 3.11 / 3.12 / 3.13
- Result: PASS

## Covered behavior

- state load and deterministic `next_actor`;
- configured Builder/Auditor command launch without implicit shell;
- separate Builder and immutable Auditor workspaces;
- Builder report requires `result_sha`;
- `result_sha` becomes `audit_target_sha`;
- Auditor report is validated against the frozen SHA;
- regression tests for runner configuration, workspace separation, command dispatch and frozen read-only audit checkout.

## Boundary

No real provider adapter or full FAIL → fix → PASS cycle was executed in this first block. The later O0-C38 evidence is recorded below.

## O0-C38 — controlled real E2E cycle

- Command: `python3 scripts/o0_e2e.py --work-root <empty-directory> --output <evidence-file>`
- Evidence: `O0_C38_E2E_EVIDENCE.json`
- Flow: `READY_FOR_AUDIT → FIX_REQUIRED → READY_FOR_AUDIT → WAITING_PRODUCT_AUTHORITY`
- Audit rounds: `0 → 1 → 1 → 2`
- Initial SHA: `77ead7d1a0b587a45c028654c900906df4244c7d`
- Corrected SHA: `082bdd575665bcd70ee8e260842a2ed9b90a5328`
- Boundary: local provider-neutral subprocess actors; no external provider adapter or product gate.

## O0-C39 — process concurrency

- Commands: `python -m unittest scripts.test_o0_concurrency` and `wsl python3 -m unittest scripts.test_o0_concurrency`
- Result: one actor and one transition under contention; the concurrent runner exits deterministically.
- Stale handling: kernel ownership releases the lock after forced process termination; a leftover lock file does not block recovery.
- Failure handling: actor failure preserves canonical state bytes and releases the lock.

## O0-C40 — idempotent operation identity

- Command: `python -m unittest scripts.test_o0_idempotency` (também executado em WSL).
- Identity: SHA-256 canônico de `run_id`, estado de origem, ator, rodada e SHAs relevantes.
- Replay: novo processo retorna o resultado persistido sem executar novamente o ator.
- Conflict: mesmo `operation_id` com relatório divergente é rejeitado sem mutação ou duplicação.
- Independent audit: PASS no SHA `ca347c0aade5ebc9ee5c2568c08cf49fab6d2328`.

## O0-C14/O0-C15 — formal regularization

- Commands: `python -m unittest scripts.test_orchestrate_handoffs scripts.test_o0_runner` em ambiente POSIX/WSL.
- O0-C14: FAIL válido produz `FIX_REQUIRED`, incrementa uma rodada, preserva SHAs/gate/aprovação e direciona ao Builder; relatórios inválidos não alteram o estado.
- O0-C15: `builder-findings` contém somente alvo, rodada e findings canônicos com evidências referenciadas; o digest do relatório aceito fica no estado e impede adulteração conjunta de relatório/snapshot. No Linux, o Builder recebe um descritor de memória selado, não o caminho mutável; troca durante preparação é rejeitada antes do spawn.
- Round limit: o terceiro FAIL produz `BLOCKED`, sem avanço indevido.
- Boundary: entrega imutável depende de `memfd`/seals do Linux; outros sistemas recusam a execução Builder em FIX_REQUIRED.

## O0-C41 — interruption recovery journal

- Command: `python -m unittest scripts.test_o0_recovery` (também executado em WSL).
- Real processes: o runner é encerrado à força após o relatório, após a transição e após o snapshot do relatório.
- Recovery: a retomada conclui estado e `operation-record` sem uma segunda execução do Builder.
- Integrity: journal, relatório, estado resultante e snapshot são vinculados à identidade e a digests canônicos.
- Boundary: timeout e cancelamento explícitos permanecem em O0-C42.

## O0-C42 — timeout and cancellation

- Tests: `python -m unittest scripts.test_o0_timeout_cancel scripts.test_o0_recovery scripts.test_o0_idempotency` em processos reais.
- Timeout/cancelamento: journal `INTERRUPTED` com motivo estruturado; estado canônico e rodada não avançam.
- Regressão: processos reais escrevem relatório parcial antes de timeout/cancelamento; o arquivo está ausente antes da retomada explícita e o journal permanece `INTERRUPTED`.
- Resume: requer `resume_interrupted=true`, estado de origem intacto e pedido de cancelamento removido; C41 continua recuperando o período após relatório/transição.
- Boundary: encerramento de descendentes no Windows e falhas estruturadas do runner ficam fora de O0-C42.

## O0-C43 — structured runner failures

- Tests: `python -m unittest scripts.test_o0_failures scripts.test_o0_recovery scripts.test_o0_timeout_cancel scripts.test_o0_idempotency` com processos reais.
- Falhas de ator e relatórios inválidos persistem evidência canônica de categoria, exit code e vínculo ao estado, sem saída textual, traceback ou payload sensível.
- Journal de operação com JSON malformado persiste evidência estruturada de INVALID_JSON, exit code 2 e saída sem secrets, sem executar o ator ou alterar o estado canônico.
- Processo reiniciado preserva evidência; se o ator saiu com código não zero após gerar relatório válido, C41 valida e recupera sem reexecutá-lo.
- Configuração inválida persiste evidência mínima ao lado da configuração; falha de escrita da evidência retorna erro seguro.

## O0-C44 — limited retries and duplicate rejection

- Tests: `python -m unittest scripts.test_o0_retry scripts.test_o0_failures scripts.test_o0_recovery scripts.test_o0_timeout_cancel scripts.test_o0_idempotency` em processos reais.
- Limite de retries: `max_retries` (padrão 3) bloqueia novas tentativas após falhas sucessivas sem executar o ator e sem alterar o estado canônico (exit code 2).
- Operação já concluída: reexecução sem configuração explícita de replay (`operation_id` repetido) é rejeitada de forma segura (`Operation has already been completed`).
- Rejeição de duplicatas: o runner e a persistência de operações rejeitam qualquer relatório cujo digest já tenha sido aceito em outra operação (`Report has already been accepted`).
- Idempotência e integridade: retomada bem-sucedida dentro do limite avança o estado; replay idempotente com o mesmo `operation_id` e mesmo payload continua preservado.

## O0-C45 — adversarial e2e cycle

- Commands: `python -m unittest scripts.test_o0_adversarial_e2e` e `python scripts/o0_adversarial_e2e.py --work-root <dir> --output O0_C45_E2E_EVIDENCE.json`.
- Concorrência: tentativa concorrente sob o lock de estado `<state>.lock` é rejeitada com exit code 2 e erro estruturado (`Runner state lock is busy`) sem alterar o estado canônico.
- Retry: falha transitória do ator gera evidência estruturada de falha (`ACTOR_EXIT_NONZERO`), exit code 2, e retry seguinte avança o estado com segurança dentro de `max_retries`.
- Timeout: ator grava relatório parcial antes de travar; expiração de `actor_timeout_seconds` encerra o ator, registra journal `INTERRUPTED` (`TIMEOUT`), descarta o relatório parcial antes da retomada e preserva o estado canônico byte a byte.
- Interrupção real (cancelamento): interrupção real forçada via requisição de cancelamento (`cancel_path`), encerra o ator com razão `CANCELLED`, descarta relatório parcial, preserva o estado canônico sem avanço indevido e comprova bloqueio enquanto houver pedido de cancelamento pendente ou falta de flag explícita `resume_interrupted` antes de retomar com sucesso.
- Rejeição de duplicatas: operações já aceitas ou relatórios com digest idêntico ao já aceito em outra operação são rejeitados de forma segura (`Operation has already been completed` e `Report has already been accepted`).
- Referências verificáveis: artefato de evidência registra referências canônicas (`canonical_reports`, `evidence_references`, `failure_references`, `journal_references`) verificáveis por digest sha256 em disco.
- Final state: ciclo adversarial conclui em `WAITING_PRODUCT_AUTHORITY` com `approval: null`, `human_gate_required: true`, sem aprovação de gate e sem início de S2.

## O0 v2 M1 — CLI validation

- Status: PASS (auditoria independente no SHA `38db3f5b31d6f614b841f1133c0b474c8eb0bdc5`)
- Evidência canônica de aceite: `O0_V2_M1_EVIDENCE_REAUDIT.json` e pacote persistente `O0_V2_M1_EVIDENCE_REAUDIT_PACKAGE/`
- Registro histórico: `O0_V2_M1_EVIDENCE.json` (mantido como registro substituído)
- Builder: Antigravity CLI v1.2.4 headless (`gemini-3.7-flash-medium`), commit `09e05b65ce8dcb35e9b83d08fb4b8e5b96662694` reproduzido via `builder.bundle` e testes validados.
- Auditor: OpenAI Codex CLI v0.154.0 headless, `--sandbox read-only`, checkout isolado e protegido contra escrita (`stat.S_IREAD`), tentativa de escrita real rejeitada (`PermissionError`), checkout 100% inalterado (`git status` limpo), relatório validado em schema JSON com `audit_result: PASS`.
- Limites: M2 e S2 não iniciados; nenhum gate de produto registrado; Gate S1 = NOT_RUN.

## O0 v2 M2 — Runner Adapters Integration

## O0 v2 M2 — Runner Adapters Integration

- Status: PASS (auditoria independente no SHA `c76317dfd0c3dd37adbc414455f8ad707b7dc88f`)
- Evidência canônica de aceite: `O0_V2_M2_EVIDENCE_PROCESS_PROOF_FINAL.json` e pacote persistente `evev2m2/`
- Registros históricos: `O0_V2_M2_EVIDENCE.json`, `O0_V2_M2_EVIDENCE_REAUDIT.json` e seus respectivos pacotes (mantidos como registros históricos)
- Documentação detalhada: `docs/O0_V2_M2_EVIDENCE.md`
- Adapters: `scripts/o0_antigravity_adapter.py` (Builder via Antigravity CLI) e `scripts/o0_codex_adapter.py` (Auditor via OpenAI Codex CLI).
- Orquestrador e validação: `scripts/o0_m2_runner_integration.py` e testes automatizados em `scripts/test_o0_m2_runner_integration.py`.
- Bundle autossuficiente: `builder.bundle` gerado a partir de `HEAD` completo (`records a complete history`), validado com `git bundle verify` sem pré-requisitos e clonado em diretório isolado com suite de testes verde.
- Comprovação de interrupção (CLIs reais no Windows):
  - Timeout e cancelamento encerram toda a árvore de processos filhos (`taskkill /F /T /PID`) para Antigravity (`agy.exe`) e Codex (`codex.CMD` / `node.exe`).
  - Nenhum relatório parcial é aceito (`report_accepted: false`).
  - Estado canônico permanece inalterado byte a byte (`canonical_state_preserved: true`).
  - Journal de operação registra `phase: INTERRUPTED` com `interruption_reason: TIMEOUT` ou `CANCELLED`.
- Execução real pelo runner:
  - Step 1 (Builder): Antigravity CLI gerou commit, validou testes unitários e produziu `builder-report.json`. O runner validou o schema, verificou o commit no Git, canonicizou evidências e transicionou para `READY_FOR_AUDIT`.
  - Step 2 (Auditor): Codex CLI executou em worktree desacoplado e protegido contra escrita sob `--sandbox read-only`, auditou o commit, produziu `audit-report.json` com `audit_result: PASS`. O runner validou integridade do checkout (`git status --porcelain` vazio), schema de auditoria e canonicizou evidências.
- Parada e governança: Estado final em `WAITING_PRODUCT_AUTHORITY` com `approval: null` e `human_gate_required: true`. Nenhum gate de produto foi registrado.
- Limites: M3 executado; M4 e S2 não iniciados; Gate S1 permanece `NOT_RUN` e S2 `NOT_STARTED`.

## O0 v2 M3 — Correction Loop (Antigravity ↔ Codex)

- Status: IMPLEMENTADO — aguardando auditoria independente
- Evidência canônica: `O0_V2_M3_EVIDENCE.json` e pacote persistente `O0_V2_M3_EVIDENCE_PACKAGE/`
- Documentação detalhada: `docs/O0_V2_M3_EVIDENCE.md`
- Script de execução: `scripts/o0_m3_correction_loop.py`
- Testes automatizados: `scripts/test_o0_m3_correction_loop.py` (4 testes passando, 4.96s)
- Fluxo de execução (iniciado em chamada única `run_loop`):
  1. Builder (Antigravity CLI) produz SHA A (`09b34e...`) com defeito intencional (adição em vez de multiplicação).
  2. Auditor (Codex CLI) audita SHA A em checkout congelado, detecta a não conformidade e retorna `FAIL` com finding estruturado bloqueante (`CODEX-FINDING-001`).
  3. Runner encaminha findings via `reports/builder-findings.json` e `IDEAS_STANDARD_FINDINGS`, transicionando para `FIX_REQUIRED`.
  4. Builder (Antigravity CLI) consome os findings, corrige a implementação (`a * b`), atualiza os testes para verificar o caso corrigido, valida os testes unitários e comita o SHA B distinto (`d360c3...`).
  5. Runner valida distinção de SHA (`SHA B != SHA A`), prepara `reports/reaudit-handoff.json` (modo DELTA) e transiciona para `READY_FOR_AUDIT`.
  6. Auditor (Codex CLI) executa reauditoria de SHA B em checkout congelado, confirma a resolução dos findings e testes passando, retornando `PASS`.
  7. Runner transiciona para `WAITING_PRODUCT_AUTHORITY` com `approval: null`, `human_gate_required: true` e encerra o loop de handoff.
- Invariantes verificados:
  - Invocação única: sem passos manuais ou copiar/colar entre etapas.
  - Preservação de `run_id`, SHAs de auditoria e referências de evidências canônicas por SHA-256.
  - Limite de rodadas: ciclo finalizado em 2 rodadas (respeitando `max_audit_rounds = 3`).
  - Bundle autossuficiente: `builder.bundle` registra histórico completo e pode ser clonado isoladamente com testes passando.
- Limites: M4 e S2 não iniciados; Gate S1 permanece `NOT_RUN` e S2 `NOT_STARTED`.
