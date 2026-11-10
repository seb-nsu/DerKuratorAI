// Markdown text from the API (answers, interpretations, rule-based papers) as React elements – never as raw HTML.
// Covers what the API writes: headings, paragraphs, bullet and numbered lists, tables, **bold**, *italic*, `code`.
import { type ReactNode } from "react";

function inline(text: string, key = ""): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|_[^_\s][^_]*_)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;
  while ((match = pattern.exec(text))) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const token = match[0];
    const k = `${key}-${i++}`;
    if (token.startsWith("**")) out.push(<strong key={k}>{inline(token.slice(2, -2), k)}</strong>);
    else if (token.startsWith("`")) out.push(<code key={k}>{token.slice(1, -1)}</code>);
    else out.push(<em key={k}>{inline(token.slice(1, -1), k)}</em>);
    last = match.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const cells = (line: string) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

export function Markdown({ text }: { text: string }) {
  const lines = (text ?? "").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = `b${i}`;
    if (!line.trim()) {
      i++;
    } else if (/^#{1,6}\s/.test(line)) {
      // headings inside a box stay small: they never compete with the page's own headings
      blocks.push(<p key={key} className="md-heading">{inline(line.replace(/^#+\s*/, ""), key)}</p>);
      i++;
    } else if (line.trim().startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        if (!/^\s*\|[\s:|-]+\|\s*$/.test(lines[i])) rows.push(cells(lines[i]));
        i++;
      }
      const [head, ...body] = rows;
      blocks.push(
        <div key={key} className="table-wrap md-table">
          <table>
            <thead><tr>{head.map((c, j) => <th key={j}>{inline(c, `${key}h${j}`)}</th>)}</tr></thead>
            <tbody>{body.map((r, j) => <tr key={j}>{r.map((c, n) => <td key={n}>{inline(c, `${key}${j}${n}`)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      );
    } else if (/^\s*([-*•]|\d+\.)\s/.test(line)) {
      const ordered = /^\s*\d+\.\s/.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*•]|\d+\.)\s/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*•]|\d+\.)\s+/, ""));
        i++;
      }
      const List = ordered ? "ol" : "ul";
      blocks.push(<List key={key}>{items.map((it, j) => <li key={j}>{inline(it, `${key}${j}`)}</li>)}</List>);
    } else {
      const para: string[] = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*\||\s*([-*•]|\d+\.)\s)/.test(lines[i])) {
        para.push(lines[i].trim());
        i++;
      }
      blocks.push(<p key={key}>{inline(para.join(" "), key)}</p>);
    }
  }
  return <div className="md">{blocks}</div>;
}
