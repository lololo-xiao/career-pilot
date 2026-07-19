export interface MCPProbeTicket {
  readonly generation: number;
  readonly serverName: string;
}

export interface MCPProbeCompletionGuard {
  begin: (serverName: string) => MCPProbeTicket;
  commit: (ticket: MCPProbeTicket, effect: () => void) => boolean;
  invalidate: () => void;
}

export function createMCPProbeCompletionGuard(): MCPProbeCompletionGuard {
  let generation = 0;
  return {
    begin(serverName) {
      generation += 1;
      return { generation, serverName };
    },
    commit(ticket, effect) {
      if (ticket.generation !== generation) return false;
      effect();
      return true;
    },
    invalidate() {
      generation += 1;
    },
  };
}

function exactToolNames(value: string): string[] {
  return [...new Set(value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean))];
}

export function reconcileMCPToolDraft(
  current: string,
  toolName: string,
  include: boolean,
): string {
  const names = exactToolNames(current).filter((name) => name !== toolName);
  if (include) names.push(toolName);
  return names.join("\n");
}
