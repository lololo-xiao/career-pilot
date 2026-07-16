import type { Application } from "./types";

const STATUS_LABELS: Record<string, string> = {
  discovered: "Tracked",
  scored: "Fit reviewed",
  approved: "Approved to tailor",
  tailoring: "Tailoring",
  ready: "Ready to apply",
  form_filled: "Form filled",
  submitted: "Applied",
  followed_up: "Followed up",
  oa: "Online assessment",
  oa_failed: "OA failed",
  interview: "Interview 1",
  interview_1: "Interview 1",
  interview_1_failed: "Interview 1 failed",
  interview_2: "Interview 2",
  interview_2_failed: "Interview 2 failed",
  final_interview: "Final interview",
  final_interview_failed: "Final interview failed",
  offer: "Offer",
  accepted: "Accepted",
  rejected: "Rejected",
  no_response: "No response",
  withdrawn: "Withdrawn",
};

const STARTED_STATUSES = new Set([
  "submitted",
  "followed_up",
  "oa",
  "oa_failed",
  "interview",
  "interview_1",
  "interview_1_failed",
  "interview_2",
  "interview_2_failed",
  "final_interview",
  "final_interview_failed",
  "offer",
  "accepted",
  "rejected",
  "no_response",
]);

const INTERVIEW_STATUSES = new Set([
  "interview",
  "interview_1",
  "interview_1_failed",
  "interview_2",
  "interview_2_failed",
  "final_interview",
  "final_interview_failed",
  "offer",
  "accepted",
]);

const OFFER_STATUSES = new Set(["offer", "accepted"]);

const NODE_COLUMNS: Record<string, number> = {
  Tracked: 0,
  Preparing: 1,
  Applied: 1,
  "Online assessment": 2,
  "OA failed": 3,
  "Interview 1": 3,
  "Interview 1 failed": 4,
  "Interview 2": 4,
  "Interview 2 failed": 5,
  "Final interview": 5,
  "Final interview failed": 6,
  Offer: 6,
  Accepted: 7,
  Rejected: 7,
  "No response": 7,
  Withdrawn: 7,
};

const NODE_COLORS: Record<string, string> = {
  Tracked: "#173d32",
  Preparing: "#9aa69e",
  Applied: "#f39a43",
  "Online assessment": "#dfbf42",
  "OA failed": "#df7970",
  "Interview 1": "#77ad73",
  "Interview 1 failed": "#d97070",
  "Interview 2": "#5a9b82",
  "Interview 2 failed": "#c96168",
  "Final interview": "#698eb4",
  "Final interview failed": "#b45464",
  Offer: "#8f70a8",
  Accepted: "#507e62",
  Rejected: "#a84d51",
  "No response": "#718397",
  Withdrawn: "#8b8178",
};

interface SankeyNode {
  id: string;
  column: number;
  value: number;
  x: number;
  y: number;
  height: number;
}

interface SankeyLink {
  source: string;
  target: string;
  value: number;
}

function allStatuses(application: Application): string[] {
  return [
    ...application.status_events.flatMap((event) => [event.from, event.to]),
    application.status,
  ];
}

function hasAnyStatus(application: Application, statuses: Set<string>): boolean {
  return allStatuses(application).some((status) => statuses.has(status));
}

function applicationPath(application: Application): string[] {
  const statuses = [
    ...application.status_events.map((event) => event.to),
    application.status,
  ];
  const started = Boolean(application.submitted_at) || statuses.some((status) => STARTED_STATUSES.has(status));
  const path = ["Tracked", started ? "Applied" : "Preparing"];

  if (started) {
    for (const status of statuses) {
      const label = STATUS_LABELS[status];
      if (!label || !Object.hasOwn(NODE_COLUMNS, label) || ["Tracked", "Applied"].includes(label)) continue;
      if (path.at(-1) !== label) path.push(label);
    }
  }
  return path;
}

function buildSankey(applications: Application[]): { nodes: SankeyNode[]; links: SankeyLink[] } {
  const linkCounts = new Map<string, SankeyLink>();
  for (const application of applications) {
    const path = applicationPath(application);
    for (let index = 0; index < path.length - 1; index += 1) {
      const source = path[index];
      const target = path[index + 1];
      if (source === target) continue;
      const key = `${source}\u0000${target}`;
      const existing = linkCounts.get(key);
      if (existing) existing.value += 1;
      else linkCounts.set(key, { source, target, value: 1 });
    }
  }

  const links = [...linkCounts.values()];
  const nodeIds = new Set(links.flatMap((link) => [link.source, link.target]));
  if (!nodeIds.size) nodeIds.add("Tracked");
  const incoming = new Map<string, number>();
  const outgoing = new Map<string, number>();
  for (const link of links) {
    outgoing.set(link.source, (outgoing.get(link.source) ?? 0) + link.value);
    incoming.set(link.target, (incoming.get(link.target) ?? 0) + link.value);
  }

  const width = 1120;
  const top = 36;
  const usableHeight = 270;
  const scale = Math.min(22, usableHeight / Math.max(applications.length, 1));
  const nodes: SankeyNode[] = [...nodeIds].map((id) => {
    const column = NODE_COLUMNS[id] ?? 7;
    const value = Math.max(incoming.get(id) ?? 0, outgoing.get(id) ?? 0, id === "Tracked" ? applications.length : 0);
    return {
      id,
      column,
      value,
      x: 32 + column * ((width - 160) / 7),
      y: 0,
      height: Math.max(14, value * scale),
    };
  });

  for (let column = 0; column <= 7; column += 1) {
    const columnNodes = nodes
      .filter((node) => node.column === column)
      .sort((a, b) => b.value - a.value || a.id.localeCompare(b.id));
    const totalHeight = columnNodes.reduce((sum, node) => sum + node.height, 0) + Math.max(0, columnNodes.length - 1) * 18;
    let y = top + Math.max(0, (usableHeight - totalHeight) / 2);
    for (const node of columnNodes) {
      node.y = y;
      y += node.height + 18;
    }
  }
  return { nodes, links };
}

function conciseSummary(applications: Application[]): string {
  if (!applications.length) {
    return "Track applications and update their statuses to see conversion and outcome trends here.";
  }
  const applied = applications.filter((item) => Boolean(item.submitted_at) || hasAnyStatus(item, STARTED_STATUSES)).length;
  const interviews = applications.filter((item) => hasAnyStatus(item, INTERVIEW_STATUSES)).length;
  const offers = applications.filter((item) => hasAnyStatus(item, OFFER_STATUSES)).length;
  const closed = applications.filter((item) => [
    "oa_failed",
    "interview_1_failed",
    "interview_2_failed",
    "final_interview_failed",
    "rejected",
    "no_response",
    "withdrawn",
    "accepted",
  ].includes(item.status)).length;
  const interviewRate = applied ? Math.round((interviews / applied) * 100) : 0;
  return `${applications.length} tracked, ${applied} applied, and ${closed} closed. ${interviews} reached an interview (${interviewRate}% of applications), with ${offers} offer${offers === 1 ? "" : "s"} recorded.`;
}

export function ApplicationSummary({ applications }: { applications: Application[] }) {
  const { nodes, links } = buildSankey(applications);
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const sourceOffsets = new Map<string, number>();
  const targetOffsets = new Map<string, number>();
  const scale = Math.min(22, 270 / Math.max(applications.length, 1));
  const applied = applications.filter((item) => Boolean(item.submitted_at) || hasAnyStatus(item, STARTED_STATUSES)).length;
  const interviews = applications.filter((item) => hasAnyStatus(item, INTERVIEW_STATUSES)).length;
  const offers = applications.filter((item) => hasAnyStatus(item, OFFER_STATUSES)).length;

  return (
    <article className="workspace-card workspace-application-summary">
      <div className="workspace-summary-heading">
        <div>
          <span className="workspace-kicker">APPLICATION FLOW</span>
          <h2>Where your applications are going</h2>
          <p>{conciseSummary(applications)}</p>
        </div>
        <div className="workspace-summary-metrics">
          <div><strong>{applied}</strong><span>Applied</span></div>
          <div><strong>{interviews}</strong><span>Interviewed</span></div>
          <div><strong>{offers}</strong><span>Offers</span></div>
        </div>
      </div>
      {applications.length ? (
        <div className="workspace-sankey" role="img" aria-label="Sankey diagram of application status transitions">
          <svg viewBox="0 0 1120 350">
            <g className="workspace-sankey-links">
              {links
                .slice()
                .sort((a, b) => (nodeById.get(a.source)?.column ?? 0) - (nodeById.get(b.source)?.column ?? 0))
                .map((link) => {
                  const source = nodeById.get(link.source);
                  const target = nodeById.get(link.target);
                  if (!source || !target) return null;
                  const linkWidth = Math.max(1.5, link.value * scale);
                  const sourceOffset = sourceOffsets.get(source.id) ?? 0;
                  const targetOffset = targetOffsets.get(target.id) ?? 0;
                  sourceOffsets.set(source.id, sourceOffset + linkWidth);
                  targetOffsets.set(target.id, targetOffset + linkWidth);
                  const startX = source.x + 12;
                  const endX = target.x;
                  const startY = source.y + sourceOffset + linkWidth / 2;
                  const endY = target.y + targetOffset + linkWidth / 2;
                  const bend = Math.max(28, (endX - startX) * 0.5);
                  return (
                    <path
                      d={`M ${startX} ${startY} C ${startX + bend} ${startY}, ${endX - bend} ${endY}, ${endX} ${endY}`}
                      key={`${link.source}-${link.target}`}
                      stroke={NODE_COLORS[target.id] ?? "#769487"}
                      strokeWidth={linkWidth}
                    >
                      <title>{link.source} to {link.target}: {link.value}</title>
                    </path>
                  );
                })}
            </g>
            <g className="workspace-sankey-nodes">
              {nodes.map((node) => {
                const rightAligned = node.column === 7;
                const labelX = rightAligned ? node.x - 8 : node.x + 20;
                return (
                  <g key={node.id}>
                    <rect fill={NODE_COLORS[node.id] ?? "#769487"} height={node.height} rx="3" width="12" x={node.x} y={node.y} />
                    <text textAnchor={rightAligned ? "end" : "start"} x={labelX} y={node.y + node.height / 2 - 2}>
                      <tspan className="workspace-sankey-count">{node.value}</tspan>
                      <tspan className="workspace-sankey-label" dy="15" x={labelX}>{node.id}</tspan>
                    </text>
                  </g>
                );
              })}
            </g>
          </svg>
        </div>
      ) : (
        <div className="workspace-summary-empty">Your Sankey diagram will appear after you track the first application.</div>
      )}
    </article>
  );
}
