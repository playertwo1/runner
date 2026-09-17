# O0-C37 — Referências verificáveis de evidência

## Contrato mínimo

- Evidência canônica: `reports/evidence/<evidence_id>.json` com `schema_version`, `evidence_id`, `run_id`, `audit_round`, `audit_target_sha` e `content`.
- Referência transmitida: somente `evidence_id` e `sha256`.
- Digest: SHA-256 dos bytes JSON canônicos em UTF-8.
- Escrita: exclusiva; um ID existente nunca é sobrescrito.
- Consumo: validar schema, existência, digest e vínculo com o estado antes de executar Builder/Auditor.
- Falha: referência ausente, caminho inseguro, substituição ou divergência rejeita sem mutar o estado.
- Findings/checks permanecem; apenas o corpo da evidência deixa de ser retransmitido.

## Testes indispensáveis

- criação e resolução válidas;
- ausência, digest divergente e substituição;
- `run_id`, rodada ou SHA alvo divergente;
- rejeição pré-ator sem mutação;
- handoff mínimo preservando referência crítica.

## Fora do escopo

O0-C38, gate/fase, armazenamento remoto, retenção, coleta de lixo e assinatura criptográfica.
