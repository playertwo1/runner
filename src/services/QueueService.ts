import { QueueRepository } from '../repositories/QueueRepository';

export class QueueService {
  private readonly queueRepo: QueueRepository;

  constructor(queueRepo: QueueRepository) {
    this.queueRepo = queueRepo;
  }

  async invoke(invocationId: string, payload: any): Promise<void> {
    // CORREÇÃO: Bloqueio de mutação antes de iniciar processamento
    const existingState = await this.queueRepo.getState(invocationId);

    if (existingState) {
      if (['ACCEPTED', 'IN_PROGRESS', 'COMPLETED'].includes(existingState.status)) {
        // Retoma com segurança (early return) ou lança erro de conflito
        console.warn(`[Queue M4] Invocação ${invocationId} ignorada: já existe com status ${existingState.status}.`);
        return; // Ou: throw new Error('INVOCATION_ALREADY_EXISTS');
      }
    }

    // Lógica original de invocação
    await this.queueRepo.createState(invocationId, {
      status: 'PENDING',
      payload,
      results: []
    });

    // ... continuação do processamento da fila
  }
}
