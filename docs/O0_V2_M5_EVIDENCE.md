# O0 v2 M5 — prova final

Início:

```text
python scripts/o0_m5_final_proof.py
```

Retomada explícita de operação interrompida:

```text
python scripts/o0_runner.py --config <config-com-resume_interrupted-true>
```

Artefatos verificáveis: `O0_V2_M5_EVIDENCE.json` e `O0_V2_M5_EVIDENCE_PACKAGE/`.
O pacote contém bundle Git, relatórios, operations, evidências, estados e provas de interrupção indexados por SHA-256. Nenhum gate ou aprovação humana foi registrado.

Prova nova de interrupção e retomada:

```text
python scripts/o0_m5_final_proof.py --resume-proof-only
```

O `taskkill` 128 observado anteriormente é compatível com PID já ausente quando a interrupção foi disparada. A prova atual usa timeout de 2 segundos e exige `taskkill` código 0, todos os PIDs observados encerrados, journal `INTERRUPTED`, ausência de relatório parcial e apenas um registro aceito após retomada. O snapshot imutável do Auditor é reutilizado somente se seus bytes coincidirem com o estado canônico.
