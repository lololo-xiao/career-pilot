import type {
  AdjacentSkill,
  MatchedSkill,
  MatchResponse,
  MissingSkill,
  RequirementImportance,
} from "./types";

function scoreLabel(score: number): string {
  if (score >= 9) return "Exceptional alignment";
  if (score >= 7) return "Strong alignment";
  if (score >= 4) return "Partial alignment";
  return "Significant gaps";
}

function ImportanceBadge({ value }: { value: RequirementImportance }) {
  return <span className={`importance importance-${value}`}>{value}</span>;
}

function sourceLabel(sourceId: string): string {
  if (sourceId.startsWith("candidate:")) {
    return `Candidate evidence ${Number.parseInt(sourceId.split(":")[1], 10)}`;
  }
  return sourceId.replaceAll("_", " ");
}

function Evidence({ items }: { items: MatchedSkill["evidence"] }) {
  return (
    <div className="evidence-list">
      {items.map((item, index) => (
        <blockquote key={`${item.source_id}-${index}`}>
          “{item.quote}”
          <cite>{sourceLabel(item.source_id)}</cite>
        </blockquote>
      ))}
    </div>
  );
}

function SupportedSkillCard({
  item,
  kind,
}: {
  item: MatchedSkill | AdjacentSkill;
  kind: "matched" | "adjacent";
}) {
  return (
    <article className={`skill-card skill-card-${kind}`}>
      <div className="skill-card-heading">
        <span className="status-dot" aria-hidden="true" />
        <h4>{item.skill}</h4>
      </div>
      <p>{item.explanation}</p>
      <Evidence items={item.evidence} />
    </article>
  );
}

function MissingSkillCard({ item }: { item: MissingSkill }) {
  return (
    <article className="skill-card skill-card-missing">
      <div className="skill-card-heading">
        <span className="status-dot" aria-hidden="true" />
        <h4>{item.skill}</h4>
        <ImportanceBadge value={item.importance} />
      </div>
      <p>{item.explanation}</p>
    </article>
  );
}

function EmptyCategory({ children }: { children: React.ReactNode }) {
  return <p className="empty-category">{children}</p>;
}

export function ResultView({
  report,
  titleId = "results-title",
}: {
  report: MatchResponse;
  titleId?: string;
}) {
  const scorePercent = `${Math.max(0, Math.min(100, report.score * 10))}%`;

  return (
    <section className="results" aria-labelledby={titleId}>
      <div className="results-heading">
        <div>
          <span className="eyebrow">Grounded match report</span>
          <h2 id={titleId}>Your evidence, mapped to the role</h2>
        </div>
        <span className="verified-pill">
          <span aria-hidden="true">✓</span> Claims checked
        </span>
      </div>

      <article className="score-panel">
        <div
          className="score-dial"
          style={{ "--score": scorePercent } as React.CSSProperties}
          aria-label={`Match score ${report.score} out of 10`}
        >
          <div className="score-dial-inner">
            <strong>{report.score}</strong>
            <span>/ 10</span>
          </div>
        </div>
        <div className="score-copy">
          <span className="score-label">{scoreLabel(report.score)}</span>
          <p>{report.summary}</p>
        </div>
      </article>

      <div className="classification-grid">
        <section className="classification-column">
          <div className="classification-title classification-title-matched">
            <span>Demonstrated</span>
            <strong>{report.matched_skills.length}</strong>
          </div>
          {report.matched_skills.length ? (
            report.matched_skills.map((item) => (
              <SupportedSkillCard key={item.skill} item={item} kind="matched" />
            ))
          ) : (
            <EmptyCategory>No direct matches identified.</EmptyCategory>
          )}
        </section>

        <section className="classification-column">
          <div className="classification-title classification-title-adjacent">
            <span>Adjacent</span>
            <strong>{report.adjacent_skills.length}</strong>
          </div>
          {report.adjacent_skills.length ? (
            report.adjacent_skills.map((item) => (
              <SupportedSkillCard key={item.skill} item={item} kind="adjacent" />
            ))
          ) : (
            <EmptyCategory>No adjacent skills identified.</EmptyCategory>
          )}
        </section>

        <section className="classification-column">
          <div className="classification-title classification-title-missing">
            <span>Missing</span>
            <strong>{report.missing_skills.length}</strong>
          </div>
          {report.missing_skills.length ? (
            report.missing_skills.map((item) => (
              <MissingSkillCard key={item.skill} item={item} />
            ))
          ) : (
            <EmptyCategory>No material gaps identified.</EmptyCategory>
          )}
        </section>
      </div>

      <div className="detail-grid">
        <section className="detail-panel">
          <div className="panel-title">
            <span className="panel-number">01</span>
            <div>
              <span className="eyebrow">Role signal</span>
              <h3>Important requirements</h3>
            </div>
          </div>
          <div className="requirement-list">
            {report.important_requirements.length ? (
              report.important_requirements.map((item) => (
                <article key={`${item.requirement}-${item.evidence_quote}`}>
                  <div>
                    <h4>{item.requirement}</h4>
                    <ImportanceBadge value={item.importance} />
                  </div>
                  <p>“{item.evidence_quote}”</p>
                </article>
              ))
            ) : (
              <EmptyCategory>No explicit job requirements identified.</EmptyCategory>
            )}
          </div>
        </section>

        <section className="detail-panel">
          <div className="panel-title">
            <span className="panel-number">02</span>
            <div>
              <span className="eyebrow">Next best move</span>
              <h3>Preparation plan</h3>
            </div>
          </div>
          {report.preparation_actions.length ? (
            <ol className="action-list">
              {[...report.preparation_actions]
                .sort((a, b) => a.priority - b.priority)
                .map((item) => (
                  <li key={`${item.priority}-${item.action}`}>
                    <span>{String(item.priority).padStart(2, "0")}</span>
                    <div>
                      <h4>{item.action}</h4>
                      <p>{item.rationale}</p>
                      <div className="address-list">
                        {item.addresses.map((address) => (
                          <small key={address}>{address}</small>
                        ))}
                      </div>
                    </div>
                  </li>
                ))}
            </ol>
          ) : (
            <EmptyCategory>No preparation actions were generated.</EmptyCategory>
          )}
        </section>
      </div>

      <section className="warning-panel">
        <div className="warning-icon" aria-hidden="true">
          !
        </div>
        <div>
          <span className="eyebrow">Honesty guardrail</span>
          <h3>Claims not supported by your evidence</h3>
          {report.unsupported_claim_warnings.length ? (
            <ul>
              {report.unsupported_claim_warnings.map((warning) => (
                <li key={warning.claim}>
                  <strong>{warning.claim}</strong>
                  <span>{warning.reason}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p>No unsupported-claim risks were identified in this analysis.</p>
          )}
        </div>
      </section>
    </section>
  );
}
