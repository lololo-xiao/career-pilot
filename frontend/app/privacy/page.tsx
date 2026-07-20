import Link from "next/link";


export default function PrivacyPage() {
  return (
    <main className="privacy-page">
      <header className="site-header">
        <Link className="brand" href="/" aria-label="CareerPilot home">
          <span className="brand-mark" aria-hidden="true">CP</span>
          <span>CareerPilot</span>
        </Link>
        <div className="account-control">
          <Link className="header-action" href="/settings">Settings</Link>
          <a
            className="header-action"
            href="https://github.com/lololo-xiao/career-pilot/issues"
            rel="noreferrer"
            target="_blank"
          >
            Support
          </a>
        </div>
      </header>

      <article className="privacy-document">
        <span className="eyebrow">Privacy notice</span>
        <h1>Your career evidence stays inside the runtime you choose.</h1>
        <p className="privacy-updated">Last updated July 20, 2026</p>

        <section>
          <h2>How CareerPilot is operated</h2>
          <p>
            CareerPilot is currently a single-user, self-hosted application. The iOS
            client connects only to the private CareerPilot server URL you enter. The
            project does not currently provide a shared CareerPilot cloud account or a
            public multi-user hosting service.
          </p>
        </section>

        <section>
          <h2>Information the app handles</h2>
          <p>
            Depending on the features you use, your runtime may store contact details,
            CVs and extracted text, career evidence, job descriptions, application
            status, conversation messages, agent memories and skills, approvals,
            generated documents, and AI-provider connection details. The iOS client
            stores the server URL on the device and may place an exported file in a
            temporary app cache so iOS can present the share sheet.
          </p>
        </section>

        <section>
          <h2>Where information is sent</h2>
          <p>
            CareerPilot sends requests to the private runtime you select. When you use
            an AI feature, relevant content is sent by that runtime to the OpenAI
            connection you configured. Public job discovery contacts the Greenhouse or
            Lever board you request. Optional web search, MCP servers, and observability
            send only the data disclosed in their settings and approval flows. Those
            services apply their own privacy terms.
          </p>
        </section>

        <section>
          <h2>Tracking and advertising</h2>
          <p>
            The iOS client contains no advertising SDK and does not use data for
            cross-app tracking. Optional Langfuse tracing is disabled unless the runtime
            operator explicitly configures it.
          </p>
        </section>

        <section>
          <h2>Retention, deletion, and security</h2>
          <p>
            Workspace data remains on the selected runtime until you delete it, restore
            a replacement backup, or remove that installation. Conversation sessions,
            agent resources, provider connections, and other records can be removed in
            the product. Provider credentials are encrypted by the runtime. Exported
            files in the iOS temporary cache are managed by iOS and may be cleared by
            the system. Never expose the current server directly to the public internet;
            use private network access or a trusted authentication gateway.
          </p>
        </section>

        <section>
          <h2>Your choices and support</h2>
          <p>
            You choose the runtime, AI provider, optional integrations, and every
            approval-gated action. To request project support, open a GitHub issue
            without including CVs, credentials, or other sensitive personal data. For a
            hosted distribution, the organization operating that server must publish
            its own contact, retention, deletion, and legal terms in addition to this
            technical notice.
          </p>
        </section>
      </article>
    </main>
  );
}
