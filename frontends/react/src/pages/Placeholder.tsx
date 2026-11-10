import { PageHeader, Empty } from "../components/ui";
import { useWorkspace } from "../components/Shell";

export function Placeholder({ pageId }: { pageId: string }) {
  const { info } = useWorkspace();
  return (
    <>
      <PageHeader title={info.pages.find((p) => p.id === pageId)?.title ?? pageId} />
      <Empty message="This page is not available in this front end yet." />
    </>
  );
}
