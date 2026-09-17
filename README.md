# Runner

Orquestrador local do ciclo Builder → Auditor do Ideias Standard. Este repositório
contém a máquina de estados, adapters de CLI, contratos próprios, testes e
evidências históricas do O0.

O código foi extraído de [`playertwo1/ideias_standard`](https://github.com/playertwo1/ideias_standard)
no commit `baf5c393e9e391dd0ddfc65551d37f4bfc7b2c58`. O histórico anterior
continua acessível naquele repositório; este repositório inicia uma linha de
desenvolvimento independente.

## Executar testes centrais

```powershell
python -m pip install -r requirements-dev.txt
$env:PYTHONDONTWRITEBYTECODE='1'
python -m unittest scripts.test_orchestrate_handoffs scripts.test_o0_runner
```

## Executar o runner

Crie uma configuração local a partir de `orchestration/o0-runner.example.json`.
Os caminhos de `repository`, `builder_workspace`, relatórios e workspaces de
auditoria devem apontar para locais explícitos. Configure os comandos dos
adapters conforme as CLIs instaladas no host.

```powershell
python scripts/o0_runner.py --config <config.json> --loop
```

O runner é uma ferramenta separada. Nenhum resultado técnico aprova gate ou
decisão de produto automaticamente.
