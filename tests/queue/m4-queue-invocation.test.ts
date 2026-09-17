import { QueueService } from '../../src/services/QueueService';
import { QueueRepository } from '../../src/repositories/QueueRepository';

describe('O0 v2 M4: Queue Invocation Idempotency', () => {
  it('deve rejeitar/retomar nova invocação da mesma fila sem apagar resultados aceitos', async () => {
    const queueRepo = new QueueRepository();
    const queueService = new QueueService(queueRepo);
    const invocationId = 'inv-req-12345';
    const payload = { data: 'test-payload' };

    // 1ª Invocação - Aceita e processada (parcial ou total)
    await queueService.invoke(invocationId, payload);
    await queueRepo.updateState(invocationId, { status: 'ACCEPTED', results: ['item1'] });

    // 2ª Invocação - Tentativa duplicada
    try {
      await queueService.invoke(invocationId, payload);
    } catch (error: any) {
      expect(error.message).toContain('ALREADY_EXISTS'); // Comportamento aceito (Rejeição)
    }

    // Validação principal: o estado anterior NÃO pode ter sido apagado
    const state = await queueRepo.getState(invocationId);
    expect(state.status).toBe('ACCEPTED');
    expect(state.results).toEqual(['item1']); // Falharia no SHA b13bf55... (retornaria null ou array vazio)
  });
});
