export interface QueueState {
  status: string;
  payload?: any;
  results: any[];
  [key: string]: any;
}

export class QueueRepository {
  private states: Map<string, QueueState> = new Map();

  async getState(invocationId: string): Promise<QueueState | null> {
    const s = this.states.get(invocationId);
    return s ? JSON.parse(JSON.stringify(s)) : null;
  }

  async createState(invocationId: string, state: QueueState): Promise<void> {
    this.states.set(invocationId, JSON.parse(JSON.stringify(state)));
  }

  async updateState(invocationId: string, updates: Partial<QueueState>): Promise<void> {
    const existing = this.states.get(invocationId) || { status: 'PENDING', results: [] };
    this.states.set(invocationId, { ...existing, ...JSON.parse(JSON.stringify(updates)) });
  }
}
