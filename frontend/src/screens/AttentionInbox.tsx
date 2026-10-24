// Everything that needs a person, oldest first (the server's order), across all projects.
import { useShared } from "../app/context";
import { Freshness, LoadFailure, Loading } from "../components/common";
import { AttentionList } from "../components/lists";
import { useNow } from "../lib/polling";

export function AttentionInbox() {
  const { attention } = useShared();
  const now = useNow();
  const items = attention.data?.items ?? [];
  const approvals = items.filter((i) => i.kind === "decision_required").length;
  const reconciliations = items.length - approvals;
  return (
    <>
      <h1>Needs your attention</h1>
      <Freshness polls={[attention]} />
      <p className="lede">
        {items.length
          ? `${approvals} approval${approvals === 1 ? "" : "s"} required, ${reconciliations} reconciliation${
              reconciliations === 1 ? "" : "s"
            } required. Oldest first.`
          : null}
      </p>
      {attention.loading ? <Loading what="the attention inbox" /> : <AttentionList items={items} now={now} />}
      <LoadFailure poll={attention} what="the attention inbox" />
      {attention.data?.next_cursor ? <p className="help">Showing the oldest {items.length} items.</p> : null}
    </>
  );
}
