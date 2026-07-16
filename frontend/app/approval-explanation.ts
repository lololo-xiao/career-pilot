export interface ApprovalRequest {
  command: string;
  description: string;
  tool?: string;
}

export interface ApprovalExplanation {
  summary: string;
  tool: string;
  behavior: string;
  scope: string;
  reason: string;
}

export function explainApproval(approval: ApprovalRequest): ApprovalExplanation {
  const command = approval.command;
  const normalized = command.toLowerCase();
  const declaredTool = approval.tool?.replace(/^career_/, "").replaceAll("_", " ");
  const isCodeRunner = normalized.includes("execute_code") || declaredTool === "execute code";
  const usesPython = /python|<<['"]?py['"]?|\bimport\s+[a-z_]/i.test(command);
  const usesNetwork = /https?:\/\/|urlopen\s*\(|requests\.|httpx\.|fetch\s*\(|\bcurl\b/i.test(command);
  const searchesJobBoards = /boards-api\.greenhouse\.io|api\.lever\.co|greenhouse|lever/i.test(command)
    && /\bjobs?\b|postings?/i.test(command);
  const startsProcesses = /\bsubprocess\b|os\.system\s*\(|os\.popen\s*\(|spawn\s*\(/i.test(command);
  const changesFiles = /write_(?:text|bytes)\s*\(|\.unlink\s*\(|\b(?:remove|rename|mkdir|makedirs)\s*\(|shutil\.(?:copy|move|rmtree)\s*\(|open\s*\([^\n,]+,\s*["'][wax+]/i.test(command);
  const sendsData = /requests\.(?:post|put|patch|delete)\s*\(|method\s*=\s*["'](?:post|put|patch|delete)/i.test(command);

  const tool = usesPython
    ? "Python code runner"
    : isCodeRunner
      ? "Code runner"
      : declaredTool
        ? declaredTool.replace(/\b\w/g, (letter) => letter.toUpperCase())
        : "Local tool";

  if (searchesJobBoards && usesNetwork) {
    return {
      summary: "Search selected company career pages for open roles.",
      tool,
      behavior: sendsData
        ? "Connects to Greenhouse and Lever job-board APIs and may send data to them."
        : "Reads public listings from Greenhouse and Lever, then returns the results to this chat.",
      scope: "One run only",
      reason: changesFiles || startsProcesses
        ? "This script also contains behavior that can affect your device, so CareerPilot pauses before running it."
        : "This script appears read-only. CareerPilot still asks because the code runner can start processes or change files.",
    };
  }

  if (usesNetwork) {
    return {
      summary: sendsData
        ? "Run code that exchanges data with an external service."
        : "Read information from websites or APIs.",
      tool,
      behavior: sendsData
        ? "Makes network requests that may send or change data outside CareerPilot."
        : "Makes network requests and brings the returned information into this task.",
      scope: "One run only",
      reason: "CareerPilot pauses because local code can do more than the visible network request.",
    };
  }

  if (changesFiles) {
    return {
      summary: "Run a local script that may update files.",
      tool,
      behavior: startsProcesses
        ? "May change local files and start other commands on your device."
        : "May create, edit, move, or remove local files.",
      scope: "One run only",
      reason: "CareerPilot asks before allowing code to change data on your device.",
    };
  }

  if (startsProcesses) {
    return {
      summary: "Run a local script that may start other commands.",
      tool,
      behavior: "May launch subprocesses with the same access as the local CareerPilot runtime.",
      scope: "One run only",
      reason: "CareerPilot asks before allowing code to start additional processes.",
    };
  }

  return {
    summary: "Run a local action to continue this task.",
    tool,
    behavior: normalized.includes("execute_code")
      ? "Runs the shown code locally and returns its output to this chat."
      : "Runs the technical command shown below.",
    scope: "One run only",
    reason: "CareerPilot could not classify every effect confidently, so it asks before continuing.",
  };
}
